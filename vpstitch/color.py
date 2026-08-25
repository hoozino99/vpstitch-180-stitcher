from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

from .config import Color


BUNDLED_ACES_STUDIO_ID = "vpstitch://aces-studio-v4.0.0"
BUNDLED_ACES_STUDIO_FILENAME = (
    "studio-config-v4.0.0_aces-v2.0_ocio-v2.5.ocio"
)


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


class ColorPipeline:
    """Applies optional OCIO transforms without introducing 8-bit buffers."""

    def __init__(self, settings: Color, camera_spaces: list[str | None]):
        self.settings = settings
        self._input_processors: list[object | None] = [None] * len(camera_spaces)
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
    def _apply(processor: object | None, image: np.ndarray) -> np.ndarray:
        if processor is None:
            return image
        import PyOpenColorIO as ocio

        contiguous = np.ascontiguousarray(image, dtype=np.float32)
        height, width, channels = contiguous.shape
        descriptor = ocio.PackedImageDesc(contiguous, width, height, channels)
        processor.apply(descriptor)
        return contiguous

    def input_to_working(self, camera_index: int, image: np.ndarray) -> np.ndarray:
        return self._apply(self._input_processors[camera_index], self.to_float(image))

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
    values = np.clip(image, 0.0, 1.0).astype(np.float64)
    if dither:
        mixed_seed = (
            seed
            ^ ((frame_index + 1) * 0x9E3779B1)
            ^ ((tile_x + 1) * 0x85EBCA77)
            ^ ((tile_y + 1) * 0xC2B2AE3D)
        ) & 0xFFFFFFFF
        rng = np.random.default_rng(mixed_seed)
        # Triangular PDF dither with a +/- 1 LSB peak-to-peak range.
        noise = rng.random(values.shape) - rng.random(values.shape)
        values += noise / 65535.0
    return np.rint(np.clip(values, 0.0, 1.0) * 65535.0).astype(np.uint16)
