from __future__ import annotations

import cv2
import numpy as np

from vpstitch.config import Flow
from vpstitch.flow import align_target_to_reference


def test_dis_flow_reduces_shift_error_without_quantizing_output() -> None:
    rng = np.random.default_rng(44)
    reference = cv2.GaussianBlur(
        rng.random((96, 160, 3), dtype=np.float32), (0, 0), 2.0
    )
    matrix = np.float32([[1, 0, 3], [0, 1, -2]])
    target = cv2.warpAffine(
        reference, matrix, (160, 96), flags=cv2.INTER_LANCZOS4, borderMode=cv2.BORDER_REFLECT101
    )
    overlap = np.ones((96, 160), dtype=bool)
    aligned, confidence = align_target_to_reference(
        reference,
        target,
        overlap,
        Flow(enabled=True, preset="medium", confidence_threshold=0.05),
    )
    before = np.mean(np.abs(reference[8:-8, 8:-8] - target[8:-8, 8:-8]))
    after = np.mean(np.abs(reference[8:-8, 8:-8] - aligned[8:-8, 8:-8]))
    assert aligned.dtype == np.float32
    assert np.mean(confidence[8:-8, 8:-8]) > 0.25
    assert after < before * 0.75

