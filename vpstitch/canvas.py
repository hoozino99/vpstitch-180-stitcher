from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import ceil, floor
from pathlib import Path

import cv2
import numpy as np

from .config import RigConfig
from .geometry import camera_map, iter_tiles


@dataclass(frozen=True)
class CanvasReport:
    canvas_width: int
    canvas_height: int
    analyzed_width: int
    analyzed_height: int
    coverage_fraction: float
    valid_bbox: tuple[int, int, int, int] | None
    safe_full_width_crop: tuple[int, int, int, int] | None

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _longest_true_run(values: np.ndarray) -> tuple[int, int] | None:
    best_start = best_end = -1
    start = -1
    for index, value in enumerate(values.tolist() + [False]):
        if value and start < 0:
            start = index
        elif not value and start >= 0:
            if index - start > best_end - best_start:
                best_start, best_end = start, index
            start = -1
    return None if best_start < 0 else (best_start, best_end)


def analyze_canvas(
    config: RigConfig,
    max_analysis_width: int = 2000,
    max_analysis_height: int = 750,
) -> tuple[CanvasReport, np.ndarray]:
    output = config.output
    scale = min(
        1.0,
        max_analysis_width / output.width,
        max_analysis_height / output.height,
    )
    width = max(32, int(round(output.width * scale)))
    height = max(32, int(round(output.height * scale)))
    analysis_output = replace(
        output,
        width=width,
        height=height,
        tile_width=min(output.tile_width, width),
        tile_height=min(output.tile_height, height),
    )
    mask = np.zeros((height, width), dtype=bool)
    for tile in iter_tiles(analysis_output):
        valid = np.zeros((tile.height, tile.width), dtype=bool)
        for camera in config.cameras:
            valid |= camera_map(camera, tile, analysis_output)[2]
        mask[
            tile.y : tile.y + tile.height,
            tile.x : tile.x + tile.width,
        ] = valid

    ys, xs = np.nonzero(mask)
    bbox = None
    if xs.size:
        x0 = floor(int(xs.min()) * output.width / width)
        y0 = floor(int(ys.min()) * output.height / height)
        x1 = ceil((int(xs.max()) + 1) * output.width / width)
        y1 = ceil((int(ys.max()) + 1) * output.height / height)
        bbox = (x0, y0, min(output.width, x1), min(output.height, y1))

    safe = None
    run = _longest_true_run(np.all(mask, axis=1))
    if run is not None:
        # Round inward because this is intended as a conservative no-black crop.
        y0 = ceil(run[0] * output.height / height)
        y1 = floor(run[1] * output.height / height)
        if y1 > y0:
            safe = (0, y0, output.width, y1)

    report = CanvasReport(
        canvas_width=output.width,
        canvas_height=output.height,
        analyzed_width=width,
        analyzed_height=height,
        coverage_fraction=float(np.mean(mask)),
        valid_bbox=bbox,
        safe_full_width_crop=safe,
    )
    return report, mask


def write_coverage_mask(path: str | Path, mask: np.ndarray) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if not cv2.imwrite(str(path), mask.astype(np.uint8) * 255):
        raise OSError(f"unable to write coverage mask: {path}")

