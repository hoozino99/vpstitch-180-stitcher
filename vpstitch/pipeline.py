from __future__ import annotations

import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Callable

import cv2
import numpy as np

from .blend import weighted_blend
from .color import ColorPipeline, quantize_u16
from .compute import RemapBackendDecision, select_remap_backend
from .config import RigConfig
from .geometry import (
    camera_map,
    expand_tile,
    iter_tiles,
    remap_camera,
    seam_weights,
)
from .imageio import read_image, write_png
from .flow import refine_adjacent_overlaps
from .mapcache import MapCache
from .metal import (
    METAL_MIN_OUTPUT_PIXELS,
    MetalBackendError,
    MetalStitchBackend,
    create_metal_backend,
)


Progress = Callable[[int, int], None]
DEFAULT_OPENCL_SOURCE_BUDGET = 512 * 1024 * 1024


def _physical_memory_bytes() -> int | None:
    try:
        pages = int(os.sysconf("SC_PHYS_PAGES"))
        page_size = int(os.sysconf("SC_PAGE_SIZE"))
    except (AttributeError, OSError, TypeError, ValueError):
        return None
    total = pages * page_size
    return total if total > 0 else None


def _opencl_source_budget() -> int:
    physical_memory = _physical_memory_bytes()
    default_bytes = DEFAULT_OPENCL_SOURCE_BUDGET
    if physical_memory is not None:
        # Keep the conservative 512 MiB floor on small systems, while allowing
        # 6K multi-camera plates to use measured OpenCL remapping on machines
        # with enough unified/device memory. The environment override remains
        # authoritative for production tuning.
        default_bytes = min(
            2 * 1024 * 1024 * 1024,
            max(DEFAULT_OPENCL_SOURCE_BUDGET, physical_memory // 8),
        )
    default_megabytes = default_bytes // (1024 * 1024)
    try:
        megabytes = int(
            os.environ.get(
                "VPSTITCH_OPENCL_SOURCE_BUDGET_MB", str(default_megabytes)
            )
        )
    except ValueError:
        megabytes = default_megabytes
    return max(64, megabytes) * 1024 * 1024


def optimized_render_config(config: RigConfig) -> RigConfig:
    """Use larger execution tiles when memory allows without changing the canvas."""
    if config.flow.enabled or os.environ.get("VPSTITCH_DISABLE_LARGE_TILES"):
        return config
    physical_memory = _physical_memory_bytes()
    if physical_memory is not None and physical_memory < 16 * 1024**3:
        return config
    output = config.output
    tile_width = (
        output.tile_width * 2 if output.tile_width <= 1024 else output.tile_width
    )
    tile_height = (
        output.tile_height * 2 if output.tile_height <= 512 else output.tile_height
    )
    tile_width = min(output.width, tile_width)
    tile_height = min(output.height, tile_height)
    if (tile_width, tile_height) == (output.tile_width, output.tile_height):
        return config
    return replace(
        config,
        output=replace(output, tile_width=tile_width, tile_height=tile_height),
    )


class Stitcher:
    def __init__(
        self,
        config: RigConfig,
        map_cache: MapCache | None = None,
        *,
        quantization_tile_size: tuple[int, int] | None = None,
    ):
        self.config = config
        self.map_cache = map_cache
        self.quantization_tile_size = quantization_tile_size or (
            config.output.tile_width,
            config.output.tile_height,
        )
        if min(self.quantization_tile_size) < 1:
            raise ValueError("quantization tile dimensions must be positive")
        self.color = ColorPipeline(
            config.color,
            [camera.colorspace for camera in config.cameras],
            [camera.color_gain for camera in config.cameras],
        )
        self.hardware_accelerated = False
        self.backend_decision = RemapBackendDecision(
            "cpu", "waiting for representative final-render tile"
        )
        self._backend_profile: tuple[object, ...] | None = None
        self._camera_backend_decisions: dict[int, RemapBackendDecision] = {}
        self._backend_rejected = False
        self._metal_backend: MetalStitchBackend | None = (
            create_metal_backend(
                config.color,
                [camera.colorspace for camera in config.cameras],
                [camera.color_gain for camera in config.cameras],
            )
            if config.output.width * config.output.height >= METAL_MIN_OUTPUT_PIXELS
            else None
        )
        self._metal_tile_cache: dict[tuple[int, int, int, int], int | None] = {}

    def stitch_arrays(
        self,
        sources: list[np.ndarray],
        destination: np.ndarray,
        frame_index: int = 0,
        progress: Progress | None = None,
    ) -> None:
        if len(sources) != len(self.config.cameras):
            raise ValueError(
                f"expected {len(self.config.cameras)} sources, got {len(sources)}"
            )
        for camera, image in zip(self.config.cameras, sources, strict=True):
            if image.shape[:2] != (camera.height, camera.width):
                raise ValueError(
                    f"{camera.name} expects {camera.width}x{camera.height}, "
                    f"got {image.shape[1]}x{image.shape[0]}"
                )
        expected = (self.config.output.height, self.config.output.width, 3)
        supported_dtypes = {np.dtype("uint16"), np.dtype("float16"), np.dtype("float32")}
        if destination.shape != expected or destination.dtype not in supported_dtypes:
            raise ValueError(f"destination must be uint16/float16/float32 with shape {expected}")

        metal_backend = (
            self._metal_backend
            if destination.dtype == np.uint16
            and not self.config.flow.enabled
            and self.config.color.mode == "ocio"
            else None
        )
        working_sources: list[np.ndarray] | None = (
            sources if self.config.color.mode != "ocio" else None
        )

        def cpu_working_sources() -> list[np.ndarray]:
            nonlocal working_sources
            if working_sources is not None:
                return working_sources
            # CPU fallback remains fully functional when Metal is unavailable
            # or fails. Camera transforms are independent, so parallelize them.
            with ThreadPoolExecutor(
                max_workers=min(len(sources), 5),
                thread_name_prefix="vpstitch-input-color",
            ) as executor:
                futures = [
                    executor.submit(
                        self.color.input_to_working,
                        index,
                        source,
                        worker_count=2,
                    )
                    for index, source in enumerate(sources)
                ]
                working_sources = [future.result() for future in futures]
            return working_sources

        if metal_backend is not None:
            try:
                # Decode stays uint16. OCIO input transforms and camera color
                # match are applied directly into private GPU working buffers,
                # avoiding five full-resolution CPU float conversions/copies.
                metal_backend.transform_sources(sources)
                self.hardware_accelerated = True
                self.backend_decision = RemapBackendDecision(
                    "metal",
                    "Metal OCIO input/color match plus fused "
                    f"{metal_backend.filter_name} remap, feather blend, "
                    "OCIO output, and 16-bit quantization",
                )
            except (MetalBackendError, MemoryError, ValueError) as error:
                self._metal_backend = None
                metal_backend = None
                self.hardware_accelerated = False
                self._backend_rejected = True
                self.backend_decision = RemapBackendDecision(
                    "cpu", f"Metal source upload failed; CPU fallback: {error}"
                )

        frame_tiles = list(iter_tiles(self.config.output))
        total = len(frame_tiles)
        if metal_backend is not None:
            cached_tiles: list[tuple[int, int, int]] = []
            cache_complete = True
            for tile in frame_tiles:
                tile_key = (tile.x, tile.y, tile.width, tile.height)
                if tile_key not in self._metal_tile_cache:
                    cache_complete = False
                    break
                prepared_id = self._metal_tile_cache[tile_key]
                if prepared_id is not None:
                    cached_tiles.append((prepared_id, tile.x, tile.y))
            if cache_complete:
                try:
                    # All fixed maps already live on the GPU. Encode every tile
                    # into one command buffer, wait once, then copy one frame.
                    metal_backend.render_prepared_frame(
                        cached_tiles,
                        destination,
                        frame_index=frame_index,
                        dither=self.config.color.integer_dither,
                        seed=self.config.color.dither_seed,
                    )
                    if progress:
                        progress(total, total)
                    return
                except (MetalBackendError, MemoryError, ValueError) as error:
                    self._metal_backend = None
                    metal_backend = None
                    self.hardware_accelerated = False
                    self._backend_rejected = True
                    self.backend_decision = RemapBackendDecision(
                        "cpu", f"Metal frame batch failed; CPU fallback: {error}"
                    )
        gpu_sources: list[cv2.UMat] = []
        if self.hardware_accelerated and metal_backend is None:
            try:
                gpu_sources = [cv2.UMat(source) for source in cpu_working_sources()]
            except (cv2.error, MemoryError) as error:
                self.hardware_accelerated = False
                self._backend_rejected = True
                self.backend_decision = RemapBackendDecision(
                    "cpu", f"OpenCL source upload failed; CPU fallback: {error}"
                )
        for number, tile in enumerate(frame_tiles, start=1):
            # DIS needs surrounding context to avoid a discontinuity at every
            # processing-tile boundary. Render a halo and retain only the core.
            margin = (
                int(np.ceil(self.config.flow.max_displacement_px)) + 16
                if self.config.flow.enabled
                else 0
            )
            render_tile = expand_tile(tile, self.config.output, margin)
            destination_slice = (
                slice(tile.y, tile.y + tile.height),
                slice(tile.x, tile.x + tile.width),
            )
            tile_key = (
                render_tile.x,
                render_tile.y,
                render_tile.width,
                render_tile.height,
            )
            has_prepared = (
                metal_backend is not None and tile_key in self._metal_tile_cache
            )
            if has_prepared:
                prepared_id = self._metal_tile_cache[tile_key]
                try:
                    if prepared_id is not None:
                        destination[destination_slice] = (
                            metal_backend.render_prepared_tile(
                                prepared_id,
                                tile.width,
                                tile.height,
                                tile_x=tile.x,
                                tile_y=tile.y,
                                frame_index=frame_index,
                                dither=self.config.color.integer_dither,
                                seed=self.config.color.dither_seed,
                            )
                        )
                    else:
                        destination[destination_slice] = 0
                    if progress:
                        progress(number, total)
                    continue
                except (MetalBackendError, MemoryError, ValueError) as error:
                    self._metal_backend = None
                    metal_backend = None
                    self.hardware_accelerated = False
                    self._backend_rejected = True
                    self.backend_decision = RemapBackendDecision(
                        "cpu", f"Metal cached tile failed; CPU fallback: {error}"
                    )
            maps = (
                self.map_cache.tile_maps(render_tile)
                if self.map_cache is not None
                else [
                    camera_map(camera, render_tile, self.config.output)
                    for camera in self.config.cameras
                ]
            )
            valid_masks = [entry[2] for entry in maps]
            longitude = maps[0][3]
            weights = seam_weights(
                self.config.cameras,
                longitude,
                valid_masks,
                self.config.output.seam_feather_deg,
            )
            render_indices = (
                list(range(len(weights)))
                if self.config.flow.enabled
                else [index for index, weight in enumerate(weights) if np.any(weight)]
            )
            if metal_backend is not None:
                if not render_indices:
                    self._metal_tile_cache[tile_key] = None
                    destination[destination_slice] = 0
                    if progress:
                        progress(number, total)
                    continue
                try:
                    prepared_id = number
                    metal_backend.prepare_tile(
                        prepared_id,
                        render_indices,
                        [(maps[index][0], maps[index][1]) for index in render_indices],
                        [weights[index] for index in render_indices],
                    )
                    self._metal_tile_cache[tile_key] = prepared_id
                    destination[destination_slice] = (
                        metal_backend.render_prepared_tile(
                            prepared_id,
                            tile.width,
                            tile.height,
                            tile_x=tile.x,
                            tile_y=tile.y,
                            frame_index=frame_index,
                            dither=self.config.color.integer_dither,
                            seed=self.config.color.dither_seed,
                        )
                    )
                    if progress:
                        progress(number, total)
                    continue
                except (MetalBackendError, MemoryError, ValueError) as error:
                    self._metal_backend = None
                    metal_backend = None
                    self.hardware_accelerated = False
                    self._backend_rejected = True
                    self.backend_decision = RemapBackendDecision(
                        "cpu", f"Metal tile failed; CPU fallback: {error}"
                    )
            warped: list[np.ndarray] = []
            render_weights: list[np.ndarray] = []
            working_sources = cpu_working_sources()
            for index in render_indices:
                source = working_sources[index]
                mapping = maps[index]
                backend_profile = (
                    source.shape,
                    source.dtype.str,
                    render_tile.width,
                    render_tile.height,
                    len(self.config.cameras),
                )
                if (
                    not self.hardware_accelerated
                    and not self._backend_rejected
                    and index not in self._camera_backend_decisions
                    and np.count_nonzero(mapping[2]) >= 1024
                ):
                    source_bytes = sum(item.nbytes for item in working_sources)
                    source_budget = _opencl_source_budget()
                    if source_bytes > source_budget:
                        self.backend_decision = RemapBackendDecision(
                            "cpu",
                            "aggregate OpenCL source uploads exceed "
                            f"the {source_budget // (1024 * 1024)} MiB budget",
                        )
                        self._backend_rejected = True
                        self._backend_profile = backend_profile
                    else:
                        decision = select_remap_backend(
                            source,
                            mapping[0],
                            mapping[1],
                            interpolation=cv2.INTER_LANCZOS4,
                            source_reuse_count=max(1, total),
                            maps_reused=False,
                            max_probe_source_bytes=source_budget,
                            min_speedup=0.10,
                            max_normalized_error=2.0 / 65535.0,
                            mean_normalized_error=0.25 / 65535.0,
                            max_absolute_error=2.0 / 65535.0,
                        )
                        self._camera_backend_decisions[index] = decision
                        if decision.backend != "opencl":
                            self.backend_decision = RemapBackendDecision(
                                "cpu",
                                f"camera {index + 1}: {decision.reason}",
                                cpu_seconds=decision.cpu_seconds,
                                opencl_seconds=decision.opencl_seconds,
                                max_normalized_error=decision.max_normalized_error,
                                mean_normalized_error=decision.mean_normalized_error,
                                max_absolute_error=decision.max_absolute_error,
                            )
                            self._backend_rejected = True
                            self._backend_profile = backend_profile
                        elif len(self._camera_backend_decisions) == len(
                            working_sources
                        ):
                            try:
                                gpu_sources = [
                                    cv2.UMat(working_source)
                                    for working_source in working_sources
                                ]
                                self.hardware_accelerated = True
                                self.backend_decision = RemapBackendDecision(
                                    "opencl",
                                    "every camera passed measured speed and quality checks",
                                )
                                self._backend_profile = backend_profile
                            except (cv2.error, MemoryError) as error:
                                self.hardware_accelerated = False
                                self._backend_rejected = True
                                self.backend_decision = RemapBackendDecision(
                                    "cpu",
                                    f"OpenCL source upload failed; CPU fallback: {error}",
                                )
                if self.hardware_accelerated:
                    try:
                        mapped = cv2.remap(
                            gpu_sources[index],
                            cv2.UMat(mapping[0]),
                            cv2.UMat(mapping[1]),
                            interpolation=cv2.INTER_LANCZOS4,
                            borderMode=cv2.BORDER_CONSTANT,
                            borderValue=0,
                        ).get()
                    except (cv2.error, MemoryError) as error:
                        # Preserve the authoritative render: permanently demote
                        # this Stitcher and rerun the current map on CPU.
                        self.hardware_accelerated = False
                        self._backend_rejected = True
                        gpu_sources.clear()
                        self.backend_decision = RemapBackendDecision(
                            "cpu", f"OpenCL remap failed; CPU fallback: {error}"
                        )
                        mapped = remap_camera(source, mapping[0], mapping[1])
                else:
                    mapped = remap_camera(source, mapping[0], mapping[1])
                warped.append(
                    mapped
                    if self.config.color.mode == "ocio"
                    else self.color.input_to_working(index, mapped)
                )
                render_weights.append(weights[index])
            if warped:
                warped = refine_adjacent_overlaps(
                    warped, render_weights, self.config.flow
                )
                blended = weighted_blend(warped, render_weights)
            else:
                blended = np.zeros(
                    (render_tile.height, render_tile.width, 3), dtype=np.float32
                )
            output_tile = self.color.working_to_output(blended)
            crop_y = tile.y - render_tile.y
            crop_x = tile.x - render_tile.x
            output_core = output_tile[
                crop_y : crop_y + tile.height,
                crop_x : crop_x + tile.width,
            ]
            if destination.dtype == np.uint16:
                quantize_width, quantize_height = self.quantization_tile_size
                for local_y in range(0, tile.height, quantize_height):
                    height = min(quantize_height, tile.height - local_y)
                    for local_x in range(0, tile.width, quantize_width):
                        width = min(quantize_width, tile.width - local_x)
                        absolute_x = tile.x + local_x
                        absolute_y = tile.y + local_y
                        destination[
                            absolute_y : absolute_y + height,
                            absolute_x : absolute_x + width,
                        ] = quantize_u16(
                            output_core[
                                local_y : local_y + height,
                                local_x : local_x + width,
                            ],
                            self.config.color.integer_dither,
                            self.config.color.dither_seed,
                            frame_index,
                            absolute_x,
                            absolute_y,
                        )
            else:
                destination[destination_slice] = output_core.astype(
                    destination.dtype, copy=False
                )
            if progress:
                progress(number, total)

    def stitch_images(
        self,
        inputs: list[str | Path],
        output: str | Path,
        progress: Progress | None = None,
    ) -> None:
        sources = [read_image(path) for path in inputs]
        if Path(output).suffix.lower() != ".png":
            raise ValueError("still-frame output must use the .png extension")
        destination = np.empty(
            (self.config.output.height, self.config.output.width, 3),
            dtype=np.uint16,
        )
        self.stitch_arrays(sources, destination, progress=progress)
        write_png(output, destination)
