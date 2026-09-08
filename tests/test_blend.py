from __future__ import annotations

import numpy as np
import pytest

from vpstitch.blend import weighted_blend


@pytest.mark.parametrize("dtype", [np.float32, np.uint16])
def test_blend_preserves_pixels_and_inputs(dtype) -> None:
    rng = np.random.default_rng(71)
    images = [(rng.random((67, 131, 3)) * 40000).astype(dtype) for _ in range(5)]
    weights = [rng.random((67, 131), dtype=np.float32) for _ in images]
    for weight in weights:
        weight[:3] = 0
        weight[3] = 1e-10
    original = [image.copy() for image in images]
    expected = np.zeros(images[0].shape, dtype=np.float32)
    total = np.zeros(weights[0].shape, dtype=np.float32)
    for image, weight in zip(images, weights):
        expected += image * weight[..., None]
        total += weight
    valid = total > 1e-8
    expected[valid] /= total[valid, None]
    expected[~valid] = 0

    result = weighted_blend(images, weights)
    np.testing.assert_array_equal(result, expected)
    for image, before in zip(images, original):
        np.testing.assert_array_equal(image, before)
