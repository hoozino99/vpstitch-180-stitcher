from __future__ import annotations

from pathlib import Path
from typing import Callable

import numpy as np

from .blend import weighted_blend
from .color import ColorPipeline, quantize_u16
from .config import RigConfig
from .geometry import (
    camera_map,
    expand_tile,
    iter_tiles,
    remap_camera,
    seam_weights,
    tile_count,
)
from .imageio import create_tiff_memmap, read_image
from .flow import refine_adjacent_overlaps
from .mapcache import MapCache


Progress = Callable[[int, int], None]


class Stitcher:
    def __init__(self, config: RigConfig, map_cache: MapCache | None = None):
        self.config = config
        self.map_cache = map_cache
        self.color = ColorPipeline(
            config.color, [camera.colorspace for camera in config.cameras]
        )

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

        # In OCIO mode, convert the camera plates to the scene-linear working
        # space before any Lanczos resampling. This costs more memory than a
        # post-warp transform but avoids filtering log/gamma encoded values.
        if self.config.color.mode == "ocio":
            working_sources = [
                self.color.input_to_working(index, source)
                for index, source in enumerate(sources)
            ]
        else:
            working_sources = sources

        total = tile_count(self.config.output)
        for number, tile in enumerate(iter_tiles(self.config.output), start=1):
            # DIS needs surrounding context to avoid a discontinuity at every
            # processing-tile boundary. Render a halo and retain only the core.
            margin = (
                int(np.ceil(self.config.flow.max_displacement_px)) + 16
                if self.config.flow.enabled
                else 0
            )
            render_tile = expand_tile(tile, self.config.output, margin)
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
            warped: list[np.ndarray] = []
            for index, (source, mapping) in enumerate(
                zip(working_sources, maps, strict=True)
            ):
                mapped = remap_camera(source, mapping[0], mapping[1])
                warped.append(
                    mapped
                    if self.config.color.mode == "ocio"
                    else self.color.input_to_working(index, mapped)
                )
            warped = refine_adjacent_overlaps(warped, weights, self.config.flow)
            blended = weighted_blend(warped, weights)
            output_tile = self.color.working_to_output(blended)
            crop_y = tile.y - render_tile.y
            crop_x = tile.x - render_tile.x
            output_core = output_tile[
                crop_y : crop_y + tile.height,
                crop_x : crop_x + tile.width,
            ]
            destination_slice = (
                slice(tile.y, tile.y + tile.height),
                slice(tile.x, tile.x + tile.width),
            )
            if destination.dtype == np.uint16:
                destination[destination_slice] = quantize_u16(
                    output_core,
                    self.config.color.integer_dither,
                    self.config.color.dither_seed,
                    frame_index,
                    tile.x,
                    tile.y,
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
        if Path(output).suffix.lower() == ".png":
            import cv2

            destination = np.empty(
                (self.config.output.height, self.config.output.width, 3),
                dtype=np.uint16,
            )
            self.stitch_arrays(sources, destination, progress=progress)
            bgr = cv2.cvtColor(destination, cv2.COLOR_RGB2BGR)
            if not cv2.imwrite(str(output), bgr):
                raise OSError(f"unable to write PNG output: {output}")
            return
        destination = create_tiff_memmap(
            output,
            (self.config.output.height, self.config.output.width, 3),
        )
        self.stitch_arrays(sources, destination, progress=progress)
        destination.flush()
