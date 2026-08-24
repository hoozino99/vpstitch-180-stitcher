from __future__ import annotations

import cv2
import numpy as np

from .config import Flow


def _analysis_gray(image: np.ndarray) -> np.ndarray:
    # DIS uses 8-bit analysis images. The image samples used for the final warp
    # remain float32; this conversion cannot quantize the rendered pixels.
    clipped = np.clip(image, 0.0, 1.0)
    gray = (
        clipped[..., 0] * 0.2126
        + clipped[..., 1] * 0.7152
        + clipped[..., 2] * 0.0722
    )
    return np.rint(gray * 255.0).astype(np.uint8)


def _dis(settings: Flow):
    preset = {
        "ultrafast": cv2.DISOPTICAL_FLOW_PRESET_ULTRAFAST,
        "fast": cv2.DISOPTICAL_FLOW_PRESET_FAST,
        "medium": cv2.DISOPTICAL_FLOW_PRESET_MEDIUM,
    }[settings.preset]
    algorithm = cv2.DISOpticalFlow_create(preset)
    algorithm.setUseSpatialPropagation(True)
    return algorithm


def align_target_to_reference(
    reference: np.ndarray,
    target: np.ndarray,
    overlap: np.ndarray,
    settings: Flow,
) -> tuple[np.ndarray, np.ndarray]:
    """Align target to reference and return the warped image and confidence.

    Confidence combines forward/backward consistency, displacement limits, and
    overlap validity. Unreliable pixels fall back to the unwarped target.
    """
    if reference.shape != target.shape or overlap.shape != reference.shape[:2]:
        raise ValueError("flow inputs must share dimensions")
    if not np.any(overlap):
        return target, np.zeros(overlap.shape, dtype=np.float32)

    ref_gray = _analysis_gray(reference)
    target_gray = _analysis_gray(target)
    # Give both analysis plates identical values outside the actual overlap so
    # invalid/black projection borders cannot pull the flow field sideways.
    ref_gray = ref_gray.copy()
    target_gray = target_gray.copy()
    ref_gray[~overlap] = 0
    target_gray[~overlap] = 0
    forward = _dis(settings).calc(ref_gray, target_gray, None)
    backward = _dis(settings).calc(target_gray, ref_gray, None)
    height, width = overlap.shape
    grid_x, grid_y = np.meshgrid(
        np.arange(width, dtype=np.float32), np.arange(height, dtype=np.float32)
    )
    sample_x = grid_x + forward[..., 0]
    sample_y = grid_y + forward[..., 1]
    warped = cv2.remap(
        target,
        sample_x,
        sample_y,
        cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_REFLECT101,
    )
    backward_at_match = cv2.remap(
        backward,
        sample_x,
        sample_y,
        cv2.INTER_LINEAR,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )
    consistency_error = np.linalg.norm(forward + backward_at_match, axis=2)
    displacement = np.linalg.norm(forward, axis=2)
    in_bounds = (
        (sample_x >= 0.0)
        & (sample_x <= width - 1.0)
        & (sample_y >= 0.0)
        & (sample_y <= height - 1.0)
    )
    target_overlap = cv2.remap(
        overlap.astype(np.uint8),
        sample_x,
        sample_y,
        cv2.INTER_NEAREST,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    ).astype(bool)
    confidence = np.exp(-0.5 * consistency_error).astype(np.float32)
    confidence *= (displacement <= settings.max_displacement_px).astype(np.float32)
    confidence *= overlap.astype(np.float32)
    confidence *= in_bounds.astype(np.float32)
    confidence *= target_overlap.astype(np.float32)
    confidence = np.where(
        confidence >= settings.confidence_threshold, confidence, 0.0
    ).astype(np.float32)
    result = target * (1.0 - confidence[..., None]) + warped * confidence[..., None]
    return result.astype(np.float32), confidence


def refine_adjacent_overlaps(
    images: list[np.ndarray],
    weights: list[np.ndarray],
    settings: Flow,
) -> list[np.ndarray]:
    if not settings.enabled:
        return images
    result = [image.copy() for image in images]
    for index in range(len(result) - 1):
        overlap = (weights[index] > 1e-4) & (weights[index + 1] > 1e-4)
        result[index + 1], _ = align_target_to_reference(
            result[index], result[index + 1], overlap, settings
        )
    return result
