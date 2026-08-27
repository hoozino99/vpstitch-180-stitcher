from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import cv2
import numpy as np

from .blend import weighted_blend
from .color import ColorPipeline
from .compute import RemapBackendDecision, opencl_available, select_remap_backend
from .config import Camera, RigConfig
from .geometry import Tile, camera_map, seam_weights
from .imageio import read_image


def _fit_dimensions(
    width: int,
    height: int,
    max_width: int,
    max_height: int,
) -> tuple[int, int]:
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, round(width * scale)), max(1, round(height * scale))


def _camera_geometry_key(camera: Camera) -> tuple[object, ...]:
    return (
        camera.width,
        camera.height,
        camera.yaw_deg,
        camera.pitch_deg,
        camera.roll_deg,
        camera.scale,
        camera.crop_left,
        camera.crop_right,
        camera.crop_top,
        camera.crop_bottom,
        camera.lens,
    )


class InteractivePreviewRenderer:
    """Low-resolution, memory-resident renderer for inspector feedback.

    Source stills and unchanged warped camera layers stay in memory. Geometry
    changes therefore remap only the edited camera; authoritative video renders
    continue to use the normal tiled pipeline.
    """

    def __init__(self, *, max_width: int = 1280, max_height: int = 720) -> None:
        self.max_width = max_width
        self.max_height = max_height
        self.clear()

    def clear(self) -> None:
        self.opencl_available = opencl_available()
        self.hardware_accelerated = False
        self.backend_decision = RemapBackendDecision(
            "cpu", "waiting for representative preview frame"
        )
        self._backend_profile: tuple[object, ...] | None = None
        self._streaming_input = False
        self._stream_frame_token: object | None = None
        self._source_key: tuple[tuple[str, int, int], ...] | None = None
        self._sources: list[np.ndarray] = []
        self._working_key: tuple[object, ...] | None = None
        self._working_sources: list[np.ndarray] = []
        self._color_key: tuple[object, ...] | None = None
        self._color_pipeline: ColorPipeline | None = None
        self._gpu_sources: list[cv2.UMat] = []
        self._output_key: object | None = None
        self._geometry_keys: list[tuple[object, ...]] = []
        self._maps_x: list[np.ndarray] = []
        self._maps_y: list[np.ndarray] = []
        self._gpu_maps: list[tuple[cv2.UMat, cv2.UMat] | None] = []
        self._warped: list[np.ndarray] = []
        self._valid_masks: list[np.ndarray] = []
        self._longitude: np.ndarray | None = None
        self._frame_revision = 0

    @staticmethod
    def _paths_key(paths: list[str | Path]) -> tuple[tuple[str, int, int], ...]:
        result = []
        for path_value in paths:
            path = Path(path_value)
            stat = path.stat()
            result.append((str(path.resolve()), stat.st_mtime_ns, stat.st_size))
        return tuple(result)

    def render(self, config: RigConfig, paths: list[str | Path]) -> np.ndarray:
        if len(paths) != len(config.cameras):
            raise ValueError(
                f"expected {len(config.cameras)} preview plates, got {len(paths)}"
            )

        self._streaming_input = False
        self._stream_frame_token = None
        source_key = self._paths_key(paths)
        if source_key != self._source_key:
            self._sources = [read_image(path) for path in paths]
            self._source_key = source_key
            self._working_key = None

        return self._render_sources(config)

    def render_frames(
        self,
        config: RigConfig,
        frames: list[np.ndarray] | tuple[np.ndarray, ...],
        *,
        frame_token: object | None = None,
    ) -> np.ndarray:
        """Render one synchronized decoded frame bundle without touching disk."""
        if len(frames) != len(config.cameras):
            raise ValueError(
                f"expected {len(config.cameras)} preview frames, got {len(frames)}"
            )
        if frame_token is None:
            self._frame_revision += 1
            frame_token = self._frame_revision
        frame_changed = frame_token != self._stream_frame_token
        if frame_changed:
            self._sources = list(frames)
            self._working_key = None
            self._stream_frame_token = frame_token
        self._streaming_input = True
        self._source_key = (("<frames>", hash(repr(frame_token)), len(frames)),)
        return self._render_sources(config)

    def _render_sources(self, config: RigConfig) -> np.ndarray:
        width, height = _fit_dimensions(
            config.output.width,
            config.output.height,
            self.max_width,
            self.max_height,
        )
        output = replace(
            config.output,
            width=width,
            height=height,
            tile_width=width,
            tile_height=height,
        )

        for camera, source in zip(config.cameras, self._sources, strict=True):
            if source.shape[:2] != (camera.height, camera.width):
                raise ValueError(
                    f"{camera.name} expects {camera.width}x{camera.height}, "
                    f"got {source.shape[1]}x{source.shape[0]}"
                )

        camera_spaces = tuple(camera.colorspace for camera in config.cameras)
        camera_gains = tuple(camera.color_gain for camera in config.cameras)
        color_key = (config.color, camera_spaces, camera_gains)
        if color_key != self._color_key or self._color_pipeline is None:
            self._color_pipeline = ColorPipeline(
                config.color,
                list(camera_spaces),
                list(camera_gains),
            )
            self._color_key = color_key
        color = self._color_pipeline
        working_key = (
            config.color.mode,
            config.color.ocio_config,
            config.color.working_space,
            config.color.match_enabled,
            config.color.match_strength,
            config.color.preserve_luminance,
            tuple(camera.colorspace for camera in config.cameras),
            tuple(camera.color_gain for camera in config.cameras),
        )
        working_changed = working_key != self._working_key
        if working_changed:
            self._working_sources = [
                color.input_to_working(index, source)
                for index, source in enumerate(self._sources)
            ]
            self._gpu_sources = []
            self._working_key = working_key

        output_key = output
        geometry_keys = [_camera_geometry_key(camera) for camera in config.cameras]
        if output_key != self._output_key or len(self._warped) != len(config.cameras):
            self._warped = [np.empty((0, 0, 3), dtype=np.float32)] * len(
                config.cameras
            )
            self._valid_masks = [np.empty((0, 0), dtype=bool)] * len(config.cameras)
            self._geometry_keys = [()] * len(config.cameras)
            self._maps_x = [np.empty((0, 0), dtype=np.float32)] * len(config.cameras)
            self._maps_y = [np.empty((0, 0), dtype=np.float32)] * len(config.cameras)
            self._gpu_maps = [None] * len(config.cameras)
            self._longitude = None
            self._output_key = output_key

        tile = Tile(0, 0, width, height)
        for index, (camera, source) in enumerate(
            zip(config.cameras, self._working_sources, strict=True)
        ):
            geometry_changed = geometry_keys[index] != self._geometry_keys[index]
            if geometry_changed:
                map_x, map_y, valid, longitude = camera_map(camera, tile, output)
                self._maps_x[index] = map_x
                self._maps_y[index] = map_y
                self._gpu_maps[index] = None
                self._valid_masks[index] = valid
                self._longitude = longitude
            backend_profile = (
                self._streaming_input,
                width,
                height,
                source.shape,
                source.dtype.str,
            )
            if index == 0 and backend_profile != self._backend_profile:
                self.backend_decision = select_remap_backend(
                    source,
                    self._maps_x[index],
                    self._maps_y[index],
                    interpolation=cv2.INTER_LINEAR,
                    source_reuse_count=1 if self._streaming_input else 8,
                    maps_reused=True,
                )
                self.hardware_accelerated = (
                    self.backend_decision.backend == "opencl"
                )
                self._backend_profile = backend_profile
                self._gpu_sources = []
                self._gpu_maps = [None] * len(config.cameras)
                self._warped = [np.empty((0, 0, 3), dtype=np.float32)] * len(
                    config.cameras
                )
                working_changed = True
            if self.hardware_accelerated and not self._gpu_sources:
                self._gpu_sources = [
                    cv2.UMat(working_source)
                    for working_source in self._working_sources
                ]
            if working_changed or geometry_changed:
                if self.hardware_accelerated:
                    gpu_maps = self._gpu_maps[index]
                    if gpu_maps is None:
                        gpu_maps = (
                            cv2.UMat(self._maps_x[index]),
                            cv2.UMat(self._maps_y[index]),
                        )
                        self._gpu_maps[index] = gpu_maps
                    self._warped[index] = cv2.remap(
                        self._gpu_sources[index],
                        gpu_maps[0],
                        gpu_maps[1],
                        interpolation=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=0,
                    ).get()
                else:
                    self._warped[index] = cv2.remap(
                        source,
                        self._maps_x[index],
                        self._maps_y[index],
                        interpolation=cv2.INTER_LINEAR,
                        borderMode=cv2.BORDER_CONSTANT,
                        borderValue=0,
                    )
            self._geometry_keys[index] = geometry_keys[index]

        if self._longitude is None:
            _, _, _, self._longitude = camera_map(config.cameras[0], tile, output)
        weights = seam_weights(
            config.cameras,
            self._longitude,
            self._valid_masks,
            output.seam_feather_deg,
        )
        blended = weighted_blend(self._warped, weights)
        rendered = color.working_to_output(blended)
        return np.rint(np.clip(rendered, 0.0, 1.0) * 65535.0).astype(np.uint16)
