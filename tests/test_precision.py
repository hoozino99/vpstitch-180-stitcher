from __future__ import annotations

import numpy as np

from vpstitch.blend import weighted_blend
from vpstitch.color import quantize_u16


def test_float_blend_retains_more_than_eight_bit_precision() -> None:
    ramp = np.arange(4096, dtype=np.float32) / 4095.0
    first = np.repeat(ramp[None, :, None], 3, axis=2)
    second = np.clip(first + 1.0 / 8192.0, 0.0, 1.0)
    weights = [
        np.full((1, 4096), 0.35, dtype=np.float32),
        np.full((1, 4096), 0.65, dtype=np.float32),
    ]
    output = weighted_blend([first, second], weights)
    encoded = quantize_u16(output, False, 1, 0, 0, 0)
    assert np.unique(encoded[..., 0]).size > 4000


def test_dither_is_reproducible_but_tile_decorrelated() -> None:
    image = np.full((32, 64, 3), 0.5, dtype=np.float32)
    first = quantize_u16(image, True, 99, 3, 0, 0)
    second = quantize_u16(image, True, 99, 3, 0, 0)
    other_tile = quantize_u16(image, True, 99, 3, 1024, 0)
    np.testing.assert_array_equal(first, second)
    assert not np.array_equal(first, other_tile)
    assert np.unique(first).size >= 2

