from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .config import Lens
from .imageio import read_image


class CalibrationError(ValueError):
    pass


@dataclass(frozen=True)
class LensCalibration:
    lens: Lens
    rms_error: float
    image_width: int
    image_height: int
    detected_images: tuple[str, ...]
    rejected_images: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["lens"]["distortion"] = list(self.lens.distortion)  # type: ignore[index]
        return result

    def write(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def checkerboard_object_points(
    columns: int, rows: int, square_size: float
) -> np.ndarray:
    if columns < 3 or rows < 3 or square_size <= 0:
        raise CalibrationError("checkerboard must be at least 3x3 with positive square size")
    points = np.zeros((rows * columns, 3), dtype=np.float32)
    points[:, :2] = np.mgrid[0:columns, 0:rows].T.reshape(-1, 2)
    points[:, :2] *= square_size
    return points


def _gray8(path: str | Path) -> tuple[np.ndarray, tuple[int, int]]:
    rgb = read_image(path)
    if rgb.dtype == np.uint16:
        analysis = np.right_shift(rgb, 8).astype(np.uint8)
    elif rgb.dtype == np.uint8:
        analysis = rgb
    else:
        finite = np.nan_to_num(rgb.astype(np.float32), nan=0.0, posinf=1.0, neginf=0.0)
        analysis = np.rint(np.clip(finite, 0.0, 1.0) * 255.0).astype(np.uint8)
    gray = cv2.cvtColor(analysis, cv2.COLOR_RGB2GRAY)
    return gray, (rgb.shape[1], rgb.shape[0])


def detect_checkerboards(
    paths: list[str | Path], columns: int, rows: int
) -> tuple[list[np.ndarray], tuple[int, int], list[str], list[str]]:
    observations: list[np.ndarray] = []
    accepted: list[str] = []
    rejected: list[str] = []
    image_size: tuple[int, int] | None = None
    for path in paths:
        gray, current_size = _gray8(path)
        if image_size is None:
            image_size = current_size
        elif image_size != current_size:
            raise CalibrationError(
                f"mixed calibration image sizes: {image_size} and {current_size}"
            )
        found, corners = cv2.findChessboardCornersSB(
            gray,
            (columns, rows),
            flags=cv2.CALIB_CB_EXHAUSTIVE | cv2.CALIB_CB_ACCURACY,
        )
        if found:
            observations.append(corners.astype(np.float32))
            accepted.append(str(path))
        else:
            rejected.append(str(path))
    if image_size is None:
        raise CalibrationError("no calibration images were provided")
    return observations, image_size, accepted, rejected


def calibrate_from_observations(
    object_points: list[np.ndarray],
    image_points: list[np.ndarray],
    image_size: tuple[int, int],
    model: str,
) -> tuple[Lens, float]:
    if len(image_points) < 8:
        raise CalibrationError(
            f"at least 8 detected checkerboard views are required, got {len(image_points)}"
        )
    width, height = image_size
    if model == "pinhole":
        rms, matrix, distortion, _, _ = cv2.calibrateCamera(
            [np.asarray(points, dtype=np.float32).reshape(-1, 3) for points in object_points],
            [np.asarray(points, dtype=np.float32).reshape(-1, 1, 2) for points in image_points],
            image_size,
            None,
            None,
            flags=cv2.CALIB_FIX_K3 | cv2.CALIB_ZERO_TANGENT_DIST,
        )
        coefficients = distortion.ravel()
        lens_distortion = (
            float(coefficients[0]),
            float(coefficients[1]),
            float(coefficients[2]) if coefficients.size > 2 else 0.0,
            float(coefficients[3]) if coefficients.size > 3 else 0.0,
        )
    elif model == "fisheye_equidistant":
        fisheye_objects = [
            np.asarray(points, dtype=np.float64).reshape(1, -1, 3)
            for points in object_points
        ]
        fisheye_images = [
            np.asarray(points, dtype=np.float64).reshape(1, -1, 2)
            for points in image_points
        ]
        matrix = np.array(
            [[width * 0.5, 0, width * 0.5], [0, width * 0.5, height * 0.5], [0, 0, 1]],
            dtype=np.float64,
        )
        distortion = np.zeros((4, 1), dtype=np.float64)
        rms, matrix, distortion, _, _ = cv2.fisheye.calibrate(
            fisheye_objects,
            fisheye_images,
            image_size,
            matrix,
            distortion,
            flags=cv2.fisheye.CALIB_RECOMPUTE_EXTRINSIC | cv2.fisheye.CALIB_FIX_SKEW,
            criteria=(cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_MAX_ITER, 200, 1e-9),
        )
        lens_distortion = tuple(float(value) for value in distortion.ravel())
    else:
        raise CalibrationError(f"unsupported calibration model: {model}")

    lens = Lens(
        model=model,
        fx=float(matrix[0, 0]),
        fy=float(matrix[1, 1]),
        cx=float(matrix[0, 2]),
        cy=float(matrix[1, 2]),
        distortion=lens_distortion,  # type: ignore[arg-type]
    )
    return lens, float(rms)


def calibrate_checkerboard(
    paths: list[str | Path],
    columns: int,
    rows: int,
    square_size: float,
    model: str,
) -> LensCalibration:
    observations, image_size, accepted, rejected = detect_checkerboards(
        paths, columns, rows
    )
    template = checkerboard_object_points(columns, rows, square_size)
    lens, rms = calibrate_from_observations(
        [template.copy() for _ in observations], observations, image_size, model
    )
    return LensCalibration(
        lens=lens,
        rms_error=rms,
        image_width=image_size[0],
        image_height=image_size[1],
        detected_images=tuple(accepted),
        rejected_images=tuple(rejected),
    )

