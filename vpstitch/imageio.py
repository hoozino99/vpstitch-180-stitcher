from __future__ import annotations

import json
import os
from dataclasses import asdict
from pathlib import Path

import cv2
import numpy as np
import tifffile

from .config import Color, Video


def read_image(path: str | Path) -> np.ndarray:
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix in {".tif", ".tiff"}:
        image = tifffile.imread(path)
    else:
        os.environ.setdefault("OPENCV_IO_ENABLE_OPENEXR", "1")
        bgr = cv2.imread(str(path), cv2.IMREAD_UNCHANGED)
        if bgr is None:
            raise OSError(f"unable to read image: {path}")
        image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError(f"expected RGB image, got {image.shape}: {path}")
    return np.ascontiguousarray(image[..., :3])


def create_tiff_memmap(path: str | Path, shape: tuple[int, int, int]) -> np.memmap:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    return tifffile.memmap(
        path,
        shape=shape,
        dtype=np.uint16,
        photometric="rgb",
        bigtiff=True,
    )


class TiffSequenceEncoder:
    """Writes an exact uint16 RGB master sequence without a YUV conversion."""

    def __init__(
        self,
        directory: str | Path,
        width: int,
        height: int,
        video: Video,
        color: Color,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.video = video
        self.color = color
        self.frame_index = 0

    def write(self, frame: np.ndarray) -> None:
        expected = (self.height, self.width, 3)
        if frame.dtype != np.uint16 or frame.shape != expected:
            raise ValueError(f"TIFF sequence expects uint16 RGB frames with shape {expected}")
        path = self.directory / f"frame_{self.frame_index:06d}.tif"
        if path.exists():
            raise OSError(f"refusing to overwrite existing sequence frame: {path}")
        tifffile.imwrite(
            path,
            frame,
            photometric="rgb",
            bigtiff=True,
            metadata=None,
        )
        self.frame_index += 1

    def close(self) -> None:
        manifest = {
            "format": "uint16-rgb-bigtiff-sequence",
            "width": self.width,
            "height": self.height,
            "frames": self.frame_index,
            "fps": self.video.fps,
            "color": asdict(self.color),
            "video_color_tags": {
                "color_primaries": self.video.color_primaries,
                "color_trc": self.video.color_trc,
                "colorspace": self.video.colorspace,
                "color_range": self.video.color_range,
            },
        }
        (self.directory / "vpstitch_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )


class ExrSequenceEncoder:
    """Writes half-float RGB OpenEXR while preserving negative and over-range values."""

    def __init__(
        self,
        directory: str | Path,
        width: int,
        height: int,
        video: Video,
        color: Color,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.width = width
        self.height = height
        self.video = video
        self.color = color
        self.frame_index = 0

    def write(self, frame: np.ndarray) -> None:
        import OpenEXR

        expected = (self.height, self.width, 3)
        if frame.dtype not in {np.dtype("float16"), np.dtype("float32")} or frame.shape != expected:
            raise ValueError(f"EXR sequence expects float RGB frames with shape {expected}")
        path = self.directory / f"frame_{self.frame_index:06d}.exr"
        if path.exists():
            raise OSError(f"refusing to overwrite existing sequence frame: {path}")
        header = {
            "compression": OpenEXR.ZIP_COMPRESSION,
            "type": OpenEXR.scanlineimage,
            "vpstitchColor": json.dumps(asdict(self.color)),
        }
        pixels = np.ascontiguousarray(frame, dtype=np.float16)
        OpenEXR.File(header, {"RGB": pixels}).write(str(path))
        self.frame_index += 1

    def close(self) -> None:
        manifest = {
            "format": "float16-rgb-openexr-sequence",
            "width": self.width,
            "height": self.height,
            "frames": self.frame_index,
            "fps": self.video.fps,
            "color": asdict(self.color),
            "preserves_negative_and_over_range": True,
        }
        (self.directory / "vpstitch_manifest.json").write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )
