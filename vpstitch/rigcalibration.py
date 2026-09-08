from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path

import cv2
import numpy as np

from .calibration import CalibrationError
from .config import Camera, Lens, RigConfig
from .geometry import camera_to_world
from .imageio import read_image


@dataclass(frozen=True)
class PairAlignment:
    left_camera: str
    right_camera: str
    matches: int
    inliers: int
    inlier_ratio: float
    rms_angular_error_deg: float
    correction_from_initial_deg: float
    fit_region: str = "overlap"
    seam_matches: int = 0
    seam_median_before_deg: float | None = None
    seam_median_after_deg: float | None = None


@dataclass(frozen=True)
class RigAlignment:
    anchor_camera: str
    cameras: tuple[dict[str, float | str], ...]
    pairs: tuple[PairAlignment, ...]

    def to_dict(self) -> dict[str, object]:
        return {
            "anchor_camera": self.anchor_camera,
            "cameras": list(self.cameras),
            "pairs": [asdict(pair) for pair in self.pairs],
        }

    def write_report(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), indent=2), encoding="utf-8")


def _analysis_gray(path: str | Path, max_dimension: int) -> tuple[np.ndarray, float, float]:
    image = read_image(path)
    rgb = image.astype(np.float32)
    if np.issubdtype(image.dtype, np.integer):
        rgb /= float(np.iinfo(image.dtype).max)
    luminance = 0.2126 * rgb[..., 0] + 0.7152 * rgb[..., 1] + 0.0722 * rgb[..., 2]
    finite = luminance[np.isfinite(luminance)]
    if finite.size == 0:
        raise CalibrationError(f"image contains no finite pixels: {path}")
    low, high = np.percentile(finite, (0.5, 99.5))
    if high <= low:
        raise CalibrationError(f"image has no usable contrast: {path}")
    gray = np.rint(np.clip((luminance - low) / (high - low), 0.0, 1.0) * 255.0).astype(
        np.uint8
    )
    height, width = gray.shape
    scale = min(1.0, max_dimension / max(width, height))
    if scale < 1.0:
        resized_width = max(1, int(round(width * scale)))
        resized_height = max(1, int(round(height * scale)))
        gray = cv2.resize(gray, (resized_width, resized_height), interpolation=cv2.INTER_AREA)
    return gray, width / gray.shape[1], height / gray.shape[0]


def _sift_features(
    path: str | Path, max_dimension: int, feature_count: int
) -> tuple[list[cv2.KeyPoint], np.ndarray]:
    gray, scale_x, scale_y = _analysis_gray(path, max_dimension)
    detector = cv2.SIFT_create(nfeatures=feature_count, contrastThreshold=0.015)
    keypoints, descriptors = detector.detectAndCompute(gray, None)
    if descriptors is None or len(keypoints) < 20:
        raise CalibrationError(f"too few features detected in {path}: {len(keypoints)}")
    full_resolution = [
        cv2.KeyPoint(
            point.pt[0] * scale_x,
            point.pt[1] * scale_y,
            point.size * max(scale_x, scale_y),
            point.angle,
            point.response,
            point.octave,
            point.class_id,
        )
        for point in keypoints
    ]
    return full_resolution, descriptors


def _mutual_ratio_matches(
    left: np.ndarray, right: np.ndarray, ratio: float
) -> list[cv2.DMatch]:
    matcher = cv2.BFMatcher(cv2.NORM_L2)
    forward = matcher.knnMatch(left, right, k=2)
    reverse = matcher.knnMatch(right, left, k=2)
    reverse_good = {
        candidate.queryIdx: candidate.trainIdx
        for pair in reverse
        if len(pair) == 2
        for candidate, second in [pair]
        if candidate.distance < ratio * second.distance
    }
    matches = [
        candidate
        for pair in forward
        if len(pair) == 2
        for candidate, second in [pair]
        if candidate.distance < ratio * second.distance
        and reverse_good.get(candidate.trainIdx) == candidate.queryIdx
    ]
    return sorted(matches, key=lambda match: match.distance)


def pixels_to_rays(points: np.ndarray, lens: Lens) -> np.ndarray:
    pixels = np.asarray(points, dtype=np.float64).reshape(-1, 1, 2)
    matrix = np.array(
        [[lens.fx, 0.0, lens.cx], [0.0, lens.fy, lens.cy], [0.0, 0.0, 1.0]],
        dtype=np.float64,
    )
    distortion = np.asarray(lens.distortion, dtype=np.float64).reshape(-1, 1)
    if lens.model == "pinhole":
        normalized = cv2.undistortPoints(pixels, matrix, distortion).reshape(-1, 2)
    elif lens.model == "fisheye_equidistant":
        normalized = cv2.fisheye.undistortPoints(pixels, matrix, distortion).reshape(-1, 2)
    else:
        raise CalibrationError(f"unsupported lens model: {lens.model}")
    rays = np.column_stack(
        [normalized[:, 0], normalized[:, 1], np.ones(normalized.shape[0])]
    )
    rays /= np.linalg.norm(rays, axis=1, keepdims=True)
    return rays


def _wahba_rotation(left_rays: np.ndarray, right_rays: np.ndarray) -> np.ndarray:
    covariance = right_rays.T @ left_rays
    u, _, vt = np.linalg.svd(covariance)
    sign = 1.0 if np.linalg.det(u @ vt) >= 0.0 else -1.0
    return u @ np.diag([1.0, 1.0, sign]) @ vt


def angular_residuals(
    rotation_left_to_right: np.ndarray,
    left_rays: np.ndarray,
    right_rays: np.ndarray,
) -> np.ndarray:
    predicted = left_rays @ rotation_left_to_right.T
    cosine = np.sum(predicted * right_rays, axis=1)
    return np.arccos(np.clip(cosine, -1.0, 1.0))


def robust_rotation(
    left_rays: np.ndarray,
    right_rays: np.ndarray,
    threshold_deg: float = 1.25,
    iterations: int = 1500,
    seed: int = 7349,
) -> tuple[np.ndarray, np.ndarray, float]:
    left_rays = np.asarray(left_rays, dtype=np.float64).reshape(-1, 3)
    right_rays = np.asarray(right_rays, dtype=np.float64).reshape(-1, 3)
    if left_rays.shape != right_rays.shape or len(left_rays) < 6:
        raise CalibrationError("rotation fitting requires at least six ray correspondences")
    threshold = np.deg2rad(threshold_deg)
    generator = np.random.default_rng(seed)
    best = np.zeros(len(left_rays), dtype=bool)
    best_error = np.inf
    for _ in range(iterations):
        sample = generator.choice(len(left_rays), size=3, replace=False)
        candidate = _wahba_rotation(left_rays[sample], right_rays[sample])
        residuals = angular_residuals(candidate, left_rays, right_rays)
        inliers = residuals <= threshold
        count = int(np.count_nonzero(inliers))
        if count < 3:
            continue
        error = float(np.mean(residuals[inliers]))
        if count > int(np.count_nonzero(best)) or (
            count == int(np.count_nonzero(best)) and error < best_error
        ):
            best = inliers
            best_error = error
    if np.count_nonzero(best) < 6:
        raise CalibrationError("unable to find a stable inter-camera rotation")
    rotation = _wahba_rotation(left_rays[best], right_rays[best])
    residuals = angular_residuals(rotation, left_rays, right_rays)
    best = residuals <= threshold
    rotation = _wahba_rotation(left_rays[best], right_rays[best])
    residuals = angular_residuals(rotation, left_rays, right_rays)
    rms = float(np.sqrt(np.mean(np.square(residuals[best]))))
    return rotation, best, np.rad2deg(rms)


def _rotation_distance_deg(first: np.ndarray, second: np.ndarray) -> float:
    delta = first @ second.T
    cosine = np.clip((np.trace(delta) - 1.0) * 0.5, -1.0, 1.0)
    return float(np.rad2deg(np.arccos(cosine)))


def rotation_to_yaw_pitch_roll(rotation: np.ndarray) -> tuple[float, float, float]:
    # Inverse of Ry(yaw) @ Rx(pitch) @ Rz(roll), matching geometry.camera_to_world.
    pitch = np.arcsin(np.clip(-rotation[1, 2], -1.0, 1.0))
    cosine_pitch = np.cos(pitch)
    if abs(cosine_pitch) < 1e-7:
        raise CalibrationError("camera orientation is at an unsupported Euler singularity")
    yaw = np.arctan2(rotation[0, 2], rotation[2, 2])
    roll = np.arctan2(rotation[1, 0], rotation[1, 1])
    return tuple(float(value) for value in np.rad2deg([yaw, pitch, roll]))  # type: ignore[return-value]


def _refine_seam_rotation(
    left_rays: np.ndarray,
    right_rays: np.ndarray,
    left_camera: Camera,
    right_camera: Camera,
    rotation: np.ndarray,
    half_width_deg: float,
    threshold_deg: float,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, float, float] | None:
    """Prefer a supported rotation at the visible blend, with a global fallback.

    A wide overlap can contain multiple parallax depths and lens residuals.
    Maximizing its total inlier count may leave a double edge at the actual
    seam. Refine only when a majority of enough seam-local correspondences
    support a substantially better fit; keep one fixed rotation for the clip.
    """
    world = left_rays @ camera_to_world(left_camera).T
    world += right_rays @ camera_to_world(right_camera).T
    longitude = np.rad2deg(np.arctan2(world[:, 0], world[:, 2]))
    seam = (left_camera.yaw_deg + right_camera.yaw_deg) * 0.5
    distance = np.abs((longitude - seam + 180.0) % 360.0 - 180.0)
    region = distance <= half_width_deg
    if np.count_nonzero(region) < 30:
        return None
    left, right = left_rays[region], right_rays[region]
    try:
        refined, inliers, _ = robust_rotation(
            left, right, threshold_deg=min(threshold_deg, 0.2)
        )
    except CalibrationError:
        return None
    if np.count_nonzero(inliers) < 25 or np.mean(inliers) < 0.5:
        return None
    # Avoid accepting a distant alternative model or a fit supported only by
    # a tiny patch with too little angular extent to constrain orientation.
    support = left[inliers]
    angles = np.rad2deg(np.arctan2(support[:, :2], support[:, 2:3]))
    extent = np.ptp(angles, axis=0)
    if extent[0] < 1.0 or extent[1] < 0.25:
        return None
    before = float(np.rad2deg(np.median(angular_residuals(rotation, left, right))))
    after = float(np.rad2deg(np.median(angular_residuals(refined, left, right))))
    if (
        _rotation_distance_deg(rotation, refined) > 2.0
        or before < 0.03
        or after >= before * 0.8
    ):
        return None
    return refined, region, inliers, before, after


def _pair_rotation(
    left_camera: Camera,
    right_camera: Camera,
    left_features: tuple[list[cv2.KeyPoint], np.ndarray],
    right_features: tuple[list[cv2.KeyPoint], np.ndarray],
    ratio: float,
    threshold_deg: float,
    max_correction_deg: float,
    seam_half_width_deg: float | None = None,
) -> tuple[np.ndarray, PairAlignment]:
    left_keypoints, left_descriptors = left_features
    right_keypoints, right_descriptors = right_features
    matches = _mutual_ratio_matches(left_descriptors, right_descriptors, ratio)
    if len(matches) < 30:
        raise CalibrationError(
            f"{left_camera.name}/{right_camera.name}: only {len(matches)} reliable feature matches"
        )
    left_pixels = np.array([left_keypoints[m.queryIdx].pt for m in matches])
    right_pixels = np.array([right_keypoints[m.trainIdx].pt for m in matches])
    left_rays = pixels_to_rays(left_pixels, left_camera.lens)
    right_rays = pixels_to_rays(right_pixels, right_camera.lens)
    rotation, inliers, rms = robust_rotation(
        left_rays, right_rays, threshold_deg=threshold_deg
    )
    inlier_count = int(np.count_nonzero(inliers))
    if inlier_count < 20 or inlier_count / len(matches) < 0.15:
        raise CalibrationError(
            f"{left_camera.name}/{right_camera.name}: unstable alignment "
            f"({inlier_count}/{len(matches)} inliers)"
        )
    fit_region = "overlap"
    seam_matches = 0
    seam_before = seam_after = None
    match_count = len(matches)
    if seam_half_width_deg is not None:
        refinement = _refine_seam_rotation(
            left_rays, right_rays, left_camera, right_camera, rotation,
            seam_half_width_deg, threshold_deg,
        )
        if refinement is not None:
            rotation, region, inliers, seam_before, seam_after = refinement
            match_count = seam_matches = int(np.count_nonzero(region))
            inlier_count = int(np.count_nonzero(inliers))
            residuals = angular_residuals(
                rotation, left_rays[region][inliers], right_rays[region][inliers]
            )
            rms = float(np.rad2deg(np.sqrt(np.mean(residuals**2))))
            fit_region = "seam"
    initial_left = camera_to_world(left_camera)
    initial_right = camera_to_world(right_camera)
    initial_relative = initial_right.T @ initial_left
    correction = _rotation_distance_deg(rotation, initial_relative)
    if correction > max_correction_deg:
        raise CalibrationError(
            f"{left_camera.name}/{right_camera.name}: estimated rotation differs from "
            f"the configured rig by {correction:.2f} degrees (limit {max_correction_deg:.2f})"
        )
    return rotation, PairAlignment(
        left_camera=left_camera.name,
        right_camera=right_camera.name,
        matches=match_count,
        inliers=inlier_count,
        inlier_ratio=inlier_count / match_count,
        rms_angular_error_deg=rms,
        correction_from_initial_deg=correction,
        fit_region=fit_region,
        seam_matches=seam_matches,
        seam_median_before_deg=seam_before,
        seam_median_after_deg=seam_after,
    )


def calibrate_rig_rotation(
    config: RigConfig,
    image_paths: list[str | Path],
    max_dimension: int = 2000,
    feature_count: int = 12000,
    match_ratio: float = 0.72,
    angular_threshold_deg: float = 1.25,
    max_correction_deg: float = 12.0,
    seam_aware: bool = False,
) -> tuple[list[np.ndarray], RigAlignment]:
    if len(image_paths) != len(config.cameras):
        raise CalibrationError(
            f"expected {len(config.cameras)} synchronized images, got {len(image_paths)}"
        )
    if not 500 <= max_dimension <= 6000:
        raise CalibrationError("analysis max dimension must be between 500 and 6000")
    features = [
        _sift_features(path, max_dimension, feature_count) for path in image_paths
    ]
    pair_rotations: list[np.ndarray] = []
    pair_reports: list[PairAlignment] = []
    for index in range(len(config.cameras) - 1):
        rotation, report = _pair_rotation(
            config.cameras[index],
            config.cameras[index + 1],
            features[index],
            features[index + 1],
            match_ratio,
            angular_threshold_deg,
            max_correction_deg,
            seam_half_width_deg=(
                max(
                    4.0,
                    config.cameras[index].feather_right_deg
                    if config.cameras[index].feather_right_deg is not None
                    else config.output.seam_feather_deg,
                    config.cameras[index + 1].feather_left_deg
                    if config.cameras[index + 1].feather_left_deg is not None
                    else config.output.seam_feather_deg,
                )
                if seam_aware else None
            ),
        )
        pair_rotations.append(rotation)
        pair_reports.append(report)

    anchor = min(
        range(len(config.cameras)), key=lambda index: abs(config.cameras[index].yaw_deg)
    )
    world_rotations: list[np.ndarray | None] = [None] * len(config.cameras)
    world_rotations[anchor] = camera_to_world(config.cameras[anchor])
    for index in range(anchor, len(config.cameras) - 1):
        assert world_rotations[index] is not None
        world_rotations[index + 1] = world_rotations[index] @ pair_rotations[index].T
    for index in range(anchor - 1, -1, -1):
        assert world_rotations[index + 1] is not None
        world_rotations[index] = world_rotations[index + 1] @ pair_rotations[index]

    solved = [rotation for rotation in world_rotations if rotation is not None]
    camera_report: list[dict[str, float | str]] = []
    for camera, rotation in zip(config.cameras, solved, strict=True):
        yaw, pitch, roll = rotation_to_yaw_pitch_roll(rotation)
        camera_report.append(
            {
                "name": camera.name,
                "yaw_deg": yaw,
                "pitch_deg": pitch,
                "roll_deg": roll,
            }
        )
    return solved, RigAlignment(
        anchor_camera=config.cameras[anchor].name,
        cameras=tuple(camera_report),
        pairs=tuple(pair_reports),
    )


def write_calibrated_config(
    source_path: str | Path,
    output_path: str | Path,
    alignment: RigAlignment,
) -> None:
    raw = json.loads(Path(source_path).read_text(encoding="utf-8"))
    values = {camera["name"]: camera for camera in alignment.cameras}
    for camera in raw["cameras"]:
        solved = values[camera["name"]]
        camera["yaw_deg"] = solved["yaw_deg"]
        camera["pitch_deg"] = solved["pitch_deg"]
        camera["roll_deg"] = solved["roll_deg"]
    destination = Path(output_path)
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps(raw, indent=2), encoding="utf-8")
