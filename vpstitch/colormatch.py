from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

import numpy as np


DEFAULT_LUMA_WEIGHTS = (0.2126, 0.7152, 0.0722)
ACESCG_LUMA_WEIGHTS = (0.2722287168, 0.6740817658, 0.0536895174)


@dataclass(frozen=True)
class OverlapDiagnostic:
    """Quality information for one usable camera-overlap constraint."""

    camera_a: int
    camera_b: int
    candidate_pixels: int
    used_pixels: int
    confidence: float
    rms_log_residual: float
    relative_gain: tuple[float, float, float]


@dataclass(frozen=True)
class ColorMatchDiagnostics:
    """Details describing which cameras and overlaps contributed to the solve."""

    reference_index: int
    connected: tuple[bool, ...]
    camera_confidence: tuple[float, ...]
    overlaps: tuple[OverlapDiagnostic, ...]
    rejected_overlaps: tuple[tuple[int, int, int], ...]


@dataclass(frozen=True)
class ColorMatchResult:
    """Per-camera RGB gains and deterministic solve diagnostics."""

    gains: np.ndarray
    confidence: np.ndarray
    diagnostics: ColorMatchDiagnostics


def _validate_inputs(
    images: Sequence[np.ndarray],
    masks: Sequence[np.ndarray],
    reference_index: int,
    luma_weights: Sequence[float],
    gain_limits: tuple[float, float],
) -> tuple[list[np.ndarray], list[np.ndarray], np.ndarray]:
    if not images:
        raise ValueError("at least one camera image is required")
    if len(images) != len(masks):
        raise ValueError("images and masks must contain the same number of cameras")
    if not 0 <= reference_index < len(images):
        raise IndexError("reference_index is outside the camera range")

    checked_images: list[np.ndarray] = []
    checked_masks: list[np.ndarray] = []
    common_shape: tuple[int, int] | None = None
    for index, (image, mask) in enumerate(zip(images, masks, strict=True)):
        image_array = np.asarray(image)
        mask_array = np.asarray(mask)
        if image_array.ndim != 3 or image_array.shape[2] != 3:
            raise ValueError(f"image {index} must have shape (height, width, 3)")
        if mask_array.shape == image_array.shape[:2] + (1,):
            mask_array = mask_array[..., 0]
        if mask_array.shape != image_array.shape[:2]:
            raise ValueError(f"mask {index} must match its image height and width")
        if common_shape is None:
            common_shape = image_array.shape[:2]
        elif image_array.shape[:2] != common_shape:
            raise ValueError("all aligned images must share the same dimensions")
        checked_images.append(image_array.astype(np.float64, copy=False))
        checked_masks.append(mask_array.astype(np.float64, copy=False))

    weights = np.asarray(luma_weights, dtype=np.float64)
    if weights.shape != (3,) or not np.all(np.isfinite(weights)):
        raise ValueError("luma_weights must contain three finite values")
    if np.any(weights < 0.0) or float(weights.sum()) <= 0.0:
        raise ValueError("luma_weights must be non-negative with a positive sum")
    weights /= weights.sum()

    low, high = gain_limits
    if not (0.0 < low <= 1.0 <= high):
        raise ValueError("gain_limits must be positive and contain unity")
    return checked_images, checked_masks, weights


def _conservative_gain(
    log_gain: np.ndarray,
    strength: float,
    limits: tuple[float, float],
    weights: np.ndarray,
) -> np.ndarray:
    gain = np.exp(log_gain * strength)
    gain /= float(np.dot(weights, gain))

    low, high = limits
    direction = gain - 1.0
    scale = 1.0
    for value in direction:
        if value > 0.0:
            scale = min(scale, (high - 1.0) / value)
        elif value < 0.0:
            scale = min(scale, (low - 1.0) / value)
    return 1.0 + max(0.0, scale) * direction


def _estimate_overlap(
    image_a: np.ndarray,
    image_b: np.ndarray,
    mask_a: np.ndarray,
    mask_b: np.ndarray,
    *,
    dark_threshold: float,
    clip_threshold: float | None,
    mask_threshold: float,
    min_overlap_pixels: int,
    max_samples: int,
    outlier_sigma: float,
    weights: np.ndarray,
) -> tuple[int, int, np.ndarray, float, float] | None:
    valid = (
        np.isfinite(mask_a)
        & np.isfinite(mask_b)
        & (mask_a > mask_threshold)
        & (mask_b > mask_threshold)
        & np.all(np.isfinite(image_a), axis=2)
        & np.all(np.isfinite(image_b), axis=2)
    )
    luminance_a = image_a @ weights
    luminance_b = image_b @ weights
    valid &= (luminance_a > dark_threshold) & (luminance_b > dark_threshold)
    valid &= (np.min(image_a, axis=2) > 0.0) & (np.min(image_b, axis=2) > 0.0)
    if clip_threshold is not None:
        valid &= (np.max(image_a, axis=2) < clip_threshold) & (
            np.max(image_b, axis=2) < clip_threshold
        )

    flat_indices = np.flatnonzero(valid)
    candidate_count = int(flat_indices.size)
    if candidate_count < min_overlap_pixels:
        return None
    if candidate_count > max_samples:
        sample_positions = np.linspace(
            0, candidate_count - 1, max_samples, dtype=np.int64
        )
        flat_indices = flat_indices[sample_positions]

    flat_a = image_a.reshape(-1, 3)[flat_indices]
    flat_b = image_b.reshape(-1, 3)[flat_indices]
    lum_a = flat_a @ weights
    lum_b = flat_b @ weights
    log_ratios = np.log((flat_a / lum_a[:, None]) / (flat_b / lum_b[:, None]))

    center = np.median(log_ratios, axis=0)
    distances = np.linalg.norm(log_ratios - center, axis=1)
    distance_center = float(np.median(distances))
    mad = float(np.median(np.abs(distances - distance_center)))
    robust_scale = max(1.4826 * mad, 1.0e-6)
    inliers = distances <= distance_center + outlier_sigma * robust_scale
    used_count = int(np.count_nonzero(inliers))
    if used_count < min_overlap_pixels:
        return None

    accepted = log_ratios[inliers]
    relative_log_gain = np.median(accepted, axis=0)
    relative_gain = np.exp(relative_log_gain)
    relative_gain /= float(np.dot(weights, relative_gain))
    relative_log_gain = np.log(relative_gain)

    residuals = accepted - relative_log_gain
    rms = float(np.sqrt(np.mean(np.square(residuals))))
    sample_confidence = min(1.0, used_count / float(min_overlap_pixels * 4))
    inlier_confidence = used_count / float(flat_indices.size)
    residual_confidence = float(np.exp(-rms / 0.08))
    confidence = float(sample_confidence * inlier_confidence * residual_confidence)
    return candidate_count, used_count, relative_log_gain, confidence, rms


def _reference_connectivity(
    camera_count: int,
    reference_index: int,
    edges: Sequence[tuple[int, int, np.ndarray, float]],
) -> tuple[np.ndarray, np.ndarray]:
    connected = np.zeros(camera_count, dtype=bool)
    confidence = np.zeros(camera_count, dtype=np.float64)
    connected[reference_index] = True
    confidence[reference_index] = 1.0

    changed = True
    while changed:
        changed = False
        for camera_a, camera_b, _, edge_confidence in edges:
            if connected[camera_a]:
                path_confidence = confidence[camera_a] * edge_confidence
                if path_confidence > confidence[camera_b]:
                    connected[camera_b] = True
                    confidence[camera_b] = path_confidence
                    changed = True
            if connected[camera_b]:
                path_confidence = confidence[camera_b] * edge_confidence
                if path_confidence > confidence[camera_a]:
                    connected[camera_a] = True
                    confidence[camera_a] = path_confidence
                    changed = True
    return connected, confidence


def solve_color_match(
    images: Sequence[np.ndarray],
    masks: Sequence[np.ndarray],
    reference_index: int,
    *,
    strength: float = 1.0,
    gain_limits: tuple[float, float] = (0.85, 1.18),
    luma_weights: Sequence[float] = DEFAULT_LUMA_WEIGHTS,
    dark_threshold: float = 0.01,
    clip_threshold: float | None = 0.98,
    mask_threshold: float = 0.5,
    min_overlap_pixels: int = 256,
    max_samples: int = 100_000,
    outlier_sigma: float = 3.5,
) -> ColorMatchResult:
    """Estimate static, luminance-neutral RGB gains from aligned overlaps.

    Every geometrically overlapping camera pair contributes a robust relative
    chromaticity constraint.  A weighted global solve propagates those
    constraints through a connected overlap graph, so a camera does not need to
    overlap the reference camera directly.  Cameras outside the reference
    component retain identity gains and report zero confidence.

    The solver is deliberately spatial-only: it consumes one set of already
    aligned scene-linear images and has no temporal or per-frame state.
    """

    if not 0.0 <= strength <= 1.0:
        raise ValueError("strength must be between zero and one")
    if min_overlap_pixels < 1 or max_samples < min_overlap_pixels:
        raise ValueError("sample limits must allow at least min_overlap_pixels")
    if dark_threshold < 0.0:
        raise ValueError("dark_threshold must be non-negative")
    if clip_threshold is not None and clip_threshold <= dark_threshold:
        raise ValueError("clip_threshold must be greater than dark_threshold")
    if outlier_sigma <= 0.0:
        raise ValueError("outlier_sigma must be positive")

    checked_images, checked_masks, weights = _validate_inputs(
        images, masks, reference_index, luma_weights, gain_limits
    )
    camera_count = len(checked_images)
    overlaps: list[OverlapDiagnostic] = []
    rejected: list[tuple[int, int, int]] = []
    edges: list[tuple[int, int, np.ndarray, float]] = []

    for camera_a in range(camera_count):
        for camera_b in range(camera_a + 1, camera_count):
            mask_overlap = (
                (checked_masks[camera_a] > mask_threshold)
                & (checked_masks[camera_b] > mask_threshold)
            )
            raw_overlap = int(np.count_nonzero(mask_overlap))
            if raw_overlap < min_overlap_pixels:
                if raw_overlap:
                    rejected.append((camera_a, camera_b, raw_overlap))
                continue
            estimate = _estimate_overlap(
                checked_images[camera_a],
                checked_images[camera_b],
                checked_masks[camera_a],
                checked_masks[camera_b],
                dark_threshold=dark_threshold,
                clip_threshold=clip_threshold,
                mask_threshold=mask_threshold,
                min_overlap_pixels=min_overlap_pixels,
                max_samples=max_samples,
                outlier_sigma=outlier_sigma,
                weights=weights,
            )
            if estimate is None:
                rejected.append((camera_a, camera_b, raw_overlap))
                continue
            candidate_count, used_count, delta, confidence, rms = estimate
            edges.append((camera_a, camera_b, delta, confidence))
            overlaps.append(
                OverlapDiagnostic(
                    camera_a=camera_a,
                    camera_b=camera_b,
                    candidate_pixels=candidate_count,
                    used_pixels=used_count,
                    confidence=confidence,
                    rms_log_residual=rms,
                    relative_gain=tuple(float(value) for value in np.exp(delta)),
                )
            )

    connected, camera_confidence = _reference_connectivity(
        camera_count, reference_index, edges
    )
    unknown_cameras = [
        index
        for index in range(camera_count)
        if index != reference_index and connected[index]
    ]
    unknown_columns = {camera: column for column, camera in enumerate(unknown_cameras)}
    solved_log_gains = np.zeros((camera_count, 3), dtype=np.float64)

    usable_edges = [
        edge for edge in edges if connected[edge[0]] and connected[edge[1]]
    ]
    if unknown_cameras and usable_edges:
        matrix = np.zeros((len(usable_edges), len(unknown_cameras)), dtype=np.float64)
        targets = np.zeros((len(usable_edges), 3), dtype=np.float64)
        for row, (camera_a, camera_b, delta, confidence) in enumerate(usable_edges):
            row_weight = np.sqrt(max(confidence, 1.0e-6))
            if camera_a != reference_index:
                matrix[row, unknown_columns[camera_a]] = -row_weight
            if camera_b != reference_index:
                matrix[row, unknown_columns[camera_b]] = row_weight
            targets[row] = delta * row_weight
        solution, _, _, _ = np.linalg.lstsq(matrix, targets, rcond=None)
        for camera, column in unknown_columns.items():
            solved_log_gains[camera] = solution[column]

    gains = np.ones((camera_count, 3), dtype=np.float64)
    for camera in unknown_cameras:
        gains[camera] = _conservative_gain(
            solved_log_gains[camera], strength, gain_limits, weights
        )
    gains[reference_index] = 1.0

    diagnostics = ColorMatchDiagnostics(
        reference_index=reference_index,
        connected=tuple(bool(value) for value in connected),
        camera_confidence=tuple(float(value) for value in camera_confidence),
        overlaps=tuple(overlaps),
        rejected_overlaps=tuple(rejected),
    )
    return ColorMatchResult(
        gains=gains,
        confidence=camera_confidence.copy(),
        diagnostics=diagnostics,
    )
