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
        encoded = np.fromfile(path, dtype=np.uint8)
        bgr = cv2.imdecode(encoded, cv2.IMREAD_UNCHANGED)
        if bgr is None:
            raise OSError(f"unable to read image: {path}")
        image = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
    if image.ndim != 3 or image.shape[2] < 3:
        raise ValueError(f"expected RGB image, got {image.shape}: {path}")
    return np.ascontiguousarray(image[..., :3])


def write_png(path: str | Path, image: np.ndarray) -> None:
    """Write an RGB PNG through a Unicode-safe path on macOS and Windows."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    bgr = cv2.cvtColor(image, cv2.COLOR_RGB2BGR)
    success, encoded = cv2.imencode(".png", bgr)
    if not success:
        raise OSError(f"unable to encode PNG: {path}")
    encoded.tofile(path)


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
