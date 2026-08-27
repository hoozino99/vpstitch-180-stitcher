from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from math import ceil, degrees, floor
from pathlib import Path

import cv2
import numpy as np

from .config import MAX_CANVAS_HEIGHT, MAX_CANVAS_WIDTH, Camera, RigConfig
from .geometry import camera_map, camera_to_world, iter_tiles


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


@dataclass(frozen=True)
class FullPlateCanvas:
    """A no-crop cylindrical canvas fitted around every source boundary."""

    width: int
    height: int
    horizontal_fov_deg: float
    vertical_fov_deg: float
    source_horizontal_fov_deg: float
    source_vertical_fov_deg: float


def _align_up(value: float, alignment: int) -> int:
    return int(ceil(value / alignment) * alignment)


def _camera_boundary(camera: Camera, samples_per_edge: int) -> np.ndarray:
    lens = camera.lens
    x_min = camera.crop_left * camera.width
    x_max = (1.0 - camera.crop_right) * camera.width - 1.001
    y_min = camera.crop_top * camera.height
    y_max = (1.0 - camera.crop_bottom) * camera.height - 1.001
    xs = np.linspace(x_min, x_max, samples_per_edge)
    ys = np.linspace(y_min, y_max, samples_per_edge)
    rectangle = np.concatenate(
        [
            np.stack([xs, np.full_like(xs, y_min)], axis=-1),
            np.stack([xs, np.full_like(xs, y_max)], axis=-1),
            np.stack([np.full_like(ys, x_min), ys], axis=-1),
            np.stack([np.full_like(ys, x_max), ys], axis=-1),
        ]
    )
    if lens.circle_radius is not None:
        angles = np.linspace(0.0, 2.0 * np.pi, samples_per_edge * 4, endpoint=False)
        circle = np.stack(
            [
                lens.cx + lens.circle_radius * np.cos(angles),
                lens.cy + lens.circle_radius * np.sin(angles),
            ],
            axis=-1,
        )
        points = np.concatenate([rectangle, circle])
        inside_crop = (
            (points[:, 0] >= x_min)
            & (points[:, 0] <= x_max)
            & (points[:, 1] >= y_min)
            & (points[:, 1] <= y_max)
        )
        inside_circle = (
            (points[:, 0] - lens.cx) ** 2 + (points[:, 1] - lens.cy) ** 2
            <= lens.circle_radius**2 + 1e-6
        )
        points = points[inside_crop & inside_circle]
    else:
        points = rectangle

    matrix = np.array(
        [
            [lens.fx * camera.scale, 0.0, lens.cx],
            [0.0, lens.fy * camera.scale, lens.cy],
            [0.0, 0.0, 1.0],
        ],
        dtype=np.float64,
    )
    distortion = np.asarray(lens.distortion, dtype=np.float64)
    image_points = points.astype(np.float64).reshape(-1, 1, 2)
    if lens.model == "pinhole":
        normalized = cv2.undistortPoints(image_points, matrix, distortion)
        normalized = normalized.reshape(-1, 2)
        return np.column_stack([normalized, np.ones(len(normalized))])

    # Invert the exact equidistant polynomial used by geometry.camera_map.
    distorted = np.column_stack(
        [
            (points[:, 0] - lens.cx) / (lens.fx * camera.scale),
            (points[:, 1] - lens.cy) / (lens.fy * camera.scale),
        ]
    )
    radius = np.linalg.norm(distorted, axis=1)
    theta = np.minimum(radius, np.pi - 1e-7)
    k1, k2, k3, k4 = lens.distortion
    for _ in range(16):
        theta2 = theta * theta
        polynomial = (
            1.0
            + k1 * theta2
            + k2 * theta2**2
            + k3 * theta2**3
            + k4 * theta2**4
        )
        derivative = (
            1.0
            + 3.0 * k1 * theta2
            + 5.0 * k2 * theta2**2
            + 7.0 * k3 * theta2**3
            + 9.0 * k4 * theta2**4
        )
        if np.any(derivative <= 1e-8):
            raise ValueError(f"{camera.name} fisheye distortion is not monotonic")
        theta -= (theta * polynomial - radius) / derivative
    if np.any((theta < 0.0) | (theta >= np.pi) | ~np.isfinite(theta)):
        raise ValueError(f"{camera.name} fisheye boundary cannot be inverted")
    direction = np.divide(
        distorted,
        radius[:, None],
        out=np.zeros_like(distorted),
        where=radius[:, None] > 1e-12,
    )
    sine = np.sin(theta)
    return np.column_stack(
        [direction[:, 0] * sine, direction[:, 1] * sine, np.cos(theta)]
    )


def recommend_full_plate_canvas(
    config: RigConfig,
    *,
    margin_fraction: float = 0.03,
    samples_per_edge: int = 512,
    alignment: int = 32,
) -> FullPlateCanvas:
    """Fit a square-pixel cylindrical canvas around all warped plate edges.

    The current horizontal pixels-per-radian is retained where possible. The
    fitted width is limited by the supported canvas maximum; height is derived
    from cylindrical projection scale so the center is not squeezed vertically.
    """
    output = config.output
    if output.projection != "cylindrical":
        raise ValueError("Full Plate Fit currently supports cylindrical projection")
    if not 0.0 <= margin_fraction <= 0.25:
        raise ValueError("margin_fraction must be between 0 and 0.25")
    if samples_per_edge < 8:
        raise ValueError("samples_per_edge must be at least 8")
    if alignment < 1:
        raise ValueError("alignment must be positive")

    yaw = np.deg2rad(output.center_yaw_deg)
    pitch = np.deg2rad(output.center_pitch_deg)
    yaw_rotation = np.array(
        [[np.cos(yaw), 0.0, np.sin(yaw)], [0.0, 1.0, 0.0], [-np.sin(yaw), 0.0, np.cos(yaw)]],
        dtype=np.float64,
    )
    pitch_rotation = np.array(
        [[1.0, 0.0, 0.0], [0.0, np.cos(pitch), -np.sin(pitch)], [0.0, np.sin(pitch), np.cos(pitch)]],
        dtype=np.float64,
    )
    # geometry._apply_view_rotation uses local @ view.T; invert it here.
    world_to_view = yaw_rotation @ pitch_rotation

    longitudes: list[np.ndarray] = []
    verticals: list[np.ndarray] = []
    for camera in config.cameras:
        camera_rays = _camera_boundary(camera, samples_per_edge)
        world_rays = camera_rays @ camera_to_world(camera).T
        view_rays = world_rays @ world_to_view
        horizontal_norm = np.hypot(view_rays[:, 0], view_rays[:, 2])
        finite = np.isfinite(view_rays).all(axis=1) & (horizontal_norm > 1e-9)
        if not np.any(finite):
            continue
        view_rays = view_rays[finite]
        horizontal_norm = horizontal_norm[finite]
        longitudes.append(np.arctan2(view_rays[:, 0], view_rays[:, 2]))
        verticals.append(view_rays[:, 1] / horizontal_norm)

    if not longitudes:
        raise ValueError("No finite source boundaries were found")

    source_half_horizontal = max(float(np.max(np.abs(values))) for values in longitudes)
    source_vertical_extent = max(float(np.max(np.abs(values))) for values in verticals)
    fitted_half_horizontal = source_half_horizontal * (1.0 + margin_fraction)
    fitted_vertical_extent = source_vertical_extent * (1.0 + margin_fraction)
    fitted_half_horizontal = min(np.pi, fitted_half_horizontal)

    horizontal_fov = 2.0 * fitted_half_horizontal
    vertical_fov = 2.0 * np.arctan(fitted_vertical_extent)
    current_density = output.width / np.deg2rad(output.horizontal_fov_deg)
    width = min(
        MAX_CANVAS_WIDTH,
        _align_up(current_density * horizontal_fov, alignment),
    )
    height = _align_up(width * 2.0 * fitted_vertical_extent / horizontal_fov, alignment)
    if height > MAX_CANVAS_HEIGHT:
        width = min(
            width,
            int(
                floor(
                    MAX_CANVAS_HEIGHT
                    * horizontal_fov
                    / (2.0 * fitted_vertical_extent)
                    / alignment
                )
            )
            * alignment,
        )
        height = _align_up(width * 2.0 * fitted_vertical_extent / horizontal_fov, alignment)
    if width < alignment or height < alignment:
        raise ValueError("Full plate fit is smaller than the supported canvas minimum")

    return FullPlateCanvas(
        width=width,
        height=height,
        horizontal_fov_deg=degrees(horizontal_fov),
        vertical_fov_deg=degrees(vertical_fov),
        source_horizontal_fov_deg=degrees(2.0 * source_half_horizontal),
        source_vertical_fov_deg=degrees(2.0 * np.arctan(source_vertical_extent)),
    )


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
