from __future__ import annotations

import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import numpy as np

from .config import Color


BUNDLED_ACES_STUDIO_ID = "vpstitch://aces-studio-v4.0.0"
BUNDLED_ACES_STUDIO_FILENAME = (
    "studio-config-v4.0.0_aces-v2.0_ocio-v2.5.ocio"
)
OCIO_PARALLEL_MIN_PIXELS = 512 * 512
_OCIO_EXECUTOR: ThreadPoolExecutor | None = None
_OCIO_EXECUTOR_LOCK = threading.Lock()


def _ocio_worker_count() -> int:
    default = min(8, max(1, (os.cpu_count() or 1) - 2))
    try:
        requested = int(os.environ.get("VPSTITCH_OCIO_THREADS", str(default)))
    except ValueError:
        requested = default
    return min(16, max(1, requested))


def _ocio_executor() -> ThreadPoolExecutor:
    global _OCIO_EXECUTOR
    with _OCIO_EXECUTOR_LOCK:
        if _OCIO_EXECUTOR is None:
            _OCIO_EXECUTOR = ThreadPoolExecutor(
                max_workers=16,
                thread_name_prefix="vpstitch-ocio",
            )
    return _OCIO_EXECUTOR


def bundled_aces_studio_path() -> Path:
    root = (
        Path(str(getattr(sys, "_MEIPASS")))
        if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS")
        else Path(__file__).resolve().parents[1]
    )
    return root / "configs" / "ocio" / BUNDLED_ACES_STUDIO_FILENAME


def resolve_ocio_identifier(identifier: str) -> str:
    if identifier == BUNDLED_ACES_STUDIO_ID:
        path = bundled_aces_studio_path()
        if not path.is_file():
            raise FileNotFoundError(f"bundled ACES Studio config is missing: {path}")
        return str(path)
    return identifier


def load_ocio_config(identifier: str):
    import PyOpenColorIO as ocio

    identifier = resolve_ocio_identifier(identifier)
    if identifier.startswith("ocio://"):
        return ocio.Config.CreateFromBuiltinConfig(identifier.removeprefix("ocio://"))
    return ocio.Config.CreateFromFile(identifier)


def color_match_space(config: object, working_space: str) -> str:
    """Return the OCIO scene-linear space used for camera matching."""
    scene_linear = config.getColorSpace("scene_linear")
    if scene_linear is None:
        return working_space
    return str(scene_linear.getName())


def build_input_transform(
    config: object,
    camera_space: str,
    working_space: str,
    gain: tuple[float, float, float],
    strength: float,
    match_space: str | None = None,
):
    """Build camera -> scene-linear match -> working-space as one OCIO graph."""
    import PyOpenColorIO as ocio

    # Configs created before scene-linear matching stored gains in the project
    # working space. Keeping that default preserves existing projects and
    # already-enqueued render snapshots exactly; new matches persist ACEScg (or
    # the config's scene_linear role) explicitly.
    match_space = match_space or working_space
    effective = np.exp(
        np.log(np.clip(np.asarray(gain, dtype=np.float64), 1.0e-6, None))
        * float(strength)
    )
    matrix = ocio.MatrixTransform()
    matrix.setMatrix(
        [
            float(effective[0]),
            0.0,
            0.0,
            0.0,
            0.0,
            float(effective[1]),
            0.0,
            0.0,
            0.0,
            0.0,
            float(effective[2]),
            0.0,
            0.0,
            0.0,
            0.0,
            1.0,
        ]
    )
    transform = ocio.GroupTransform()
    transform.appendTransform(
        ocio.ColorSpaceTransform(src=camera_space, dst=match_space)
    )
    transform.appendTransform(matrix)
    if match_space != working_space:
        transform.appendTransform(
            ocio.ColorSpaceTransform(src=match_space, dst=working_space)
        )
    return transform


class ColorPipeline:
    """Applies optional OCIO transforms without introducing 8-bit buffers."""

    def __init__(
        self,
        settings: Color,
        camera_spaces: list[str | None],
        camera_gains: list[tuple[float, float, float]] | None = None,
    ):
        self.settings = settings
        self._camera_gains = camera_gains or [
            (1.0, 1.0, 1.0) for _ in camera_spaces
        ]
        if len(self._camera_gains) != len(camera_spaces):
            raise ValueError("camera gain count must match camera color-space count")
        self._input_processors: list[object | None] = [None] * len(camera_spaces)
        self._matched_input_processors: list[object | None] = [None] * len(
            camera_spaces
        )
        self._output_processor: object | None = None
        if settings.mode == "ocio":
            import PyOpenColorIO as ocio

            try:
                config = load_ocio_config(str(settings.ocio_config))
                self._input_processors = [
                    config.getProcessor(
                        str(space), str(settings.working_space)
                    ).getDefaultCPUProcessor()
                    for space in camera_spaces
                ]
                if settings.match_enabled:
                    self._matched_input_processors = [
                        config.getProcessor(
                            build_input_transform(
                                config,
                                str(space),
                                str(settings.working_space),
                                gain,
                                float(settings.match_strength),
                                settings.match_space,
                            )
                        ).getDefaultCPUProcessor()
                        for space, gain in zip(
                            camera_spaces, self._camera_gains, strict=True
                        )
                    ]
                if settings.output_mode == "display_view":
                    transform = ocio.DisplayViewTransform(
                        src=str(settings.working_space),
                        display=str(settings.display),
                        view=str(settings.view),
                    )
                    self._output_processor = config.getProcessor(
                        transform
                    ).getDefaultCPUProcessor()
                else:
                    self._output_processor = config.getProcessor(
                        str(settings.working_space), str(settings.output_space)
                    ).getDefaultCPUProcessor()
            except Exception as error:
                raise ValueError(
                    f"OCIO setup failed for {settings.ocio_config}: {error}"
                ) from error

    @staticmethod
    def to_float(image: np.ndarray) -> np.ndarray:
        if image.dtype == np.uint16:
            return image.astype(np.float32) / 65535.0
        if image.dtype == np.uint8:
            return image.astype(np.float32) / 255.0
        if image.dtype in {np.dtype("float16"), np.dtype("float32"), np.dtype("float64")}:
            return image.astype(np.float32, copy=False)
        raise TypeError(f"unsupported image dtype: {image.dtype}")

    @staticmethod
    def _apply(
        processor: object | None,
        image: np.ndarray,
        *,
        worker_count: int | None = None,
    ) -> np.ndarray:
        if processor is None:
            return image
        import PyOpenColorIO as ocio

        # OCIO applies in place. Keep the decoder/reference buffer immutable so
        # a second preview or comparison never color-transforms an already
        # transformed source frame.
        contiguous = np.array(image, dtype=np.float32, copy=True, order="C")
        height, width, channels = contiguous.shape
        apply_rgb = getattr(processor, "applyRGB", None)
        workers = min(
            _ocio_worker_count() if worker_count is None else max(1, worker_count),
            height,
        )
        if (
            callable(apply_rgb)
            and workers > 1
            and height * width >= OCIO_PARALLEL_MIN_PIXELS
        ):
            # PyOpenColorIO releases the GIL while applying a CPUProcessor.
            # Process disjoint packed-row views concurrently so the expensive
            # ACES output transform uses the available CPU cores. The transform
            # is pixel-local, therefore splitting rows is bit-identical to one
            # packed-image call.
            chunks = [
                chunk
                for chunk in np.array_split(contiguous, workers, axis=0)
                if chunk.size
            ]
            futures = [_ocio_executor().submit(apply_rgb, chunk) for chunk in chunks]
            for future in futures:
                future.result()
            return contiguous
        if callable(apply_rgb):
            apply_rgb(contiguous)
            return contiguous
        descriptor = ocio.PackedImageDesc(contiguous, width, height, channels)
        processor.apply(descriptor)
        return contiguous

    def input_to_working(
        self,
        camera_index: int,
        image: np.ndarray,
        *,
        apply_match: bool = True,
        worker_count: int | None = None,
    ) -> np.ndarray:
        processor = self._input_processors[camera_index]
        if apply_match and self.settings.match_enabled:
            processor = self._matched_input_processors[camera_index]
        return self._apply(
            processor,
            self.to_float(image),
            worker_count=worker_count,
        )

    def working_to_output(self, image: np.ndarray) -> np.ndarray:
        return self._apply(self._output_processor, image)


def quantize_u16(
    image: np.ndarray,
    dither: bool,
    seed: int,
    frame_index: int,
    tile_x: int,
    tile_y: int,
) -> np.ndarray:
    values = np.clip(image, 0.0, 1.0).astype(np.float32, copy=False)
    if dither:
        mixed_seed = (
            seed
            ^ ((frame_index + 1) * 0x9E3779B1)
            ^ ((tile_x + 1) * 0x85EBCA77)
            ^ ((tile_y + 1) * 0xC2B2AE3D)
        ) & 0xFFFFFFFF
        rng = np.random.default_rng(mixed_seed)
        # Triangular PDF dither with a +/- 1 LSB peak-to-peak range.
        noise = rng.random(values.shape, dtype=np.float32)
        noise -= rng.random(values.shape, dtype=np.float32)
        values = values + noise * np.float32(1.0 / 65535.0)
    return np.rint(np.clip(values, 0.0, 1.0) * np.float32(65535.0)).astype(
        np.uint16
    )
