from __future__ import annotations

import numpy as np
import pytest

from vpstitch.cli import _color_match_analysis_config
from vpstitch.colormatch import DEFAULT_LUMA_WEIGHTS, solve_color_match
from vpstitch.config import load_config


def _scene(height: int = 48, width: int = 120) -> np.ndarray:
    y, x = np.mgrid[:height, :width]
    return np.stack(
        (
            0.15 + 0.45 * x / width,
            0.18 + 0.35 * y / height,
            0.12 + 0.25 * (x + y) / (width + height),
        ),
        axis=2,
    ).astype(np.float64)


def _normalized_gain(values: tuple[float, float, float]) -> np.ndarray:
    weights = np.asarray(DEFAULT_LUMA_WEIGHTS)
    gain = np.asarray(values, dtype=np.float64)
    return gain / np.dot(weights, gain)


def test_color_match_analysis_scales_camera_geometry_with_canvas() -> None:
    config = load_config("configs/drive_5cam_180.prores-hq.json")

    scaled, scale = _color_match_analysis_config(config)

    assert scaled.output.width == 1280
    assert scaled.output.height == round(config.output.height * scale)
    assert scale == pytest.approx(1280 / config.output.width)
    for source, camera in zip(config.cameras, scaled.cameras, strict=True):
        assert camera.width == round(source.width * scale)
        assert camera.height == round(source.height * scale)
        assert camera.lens.fx == pytest.approx(source.lens.fx * scale)
        assert camera.lens.fy == pytest.approx(source.lens.fy * scale)
        assert camera.lens.cx == pytest.approx(source.lens.cx * scale)
        assert camera.lens.cy == pytest.approx(source.lens.cy * scale)


def test_recovers_magenta_and_cyan_drift_with_outlier_rejection() -> None:
    base = _scene()
    expected = _normalized_gain((0.94, 1.05, 0.96))
    drifted = base / expected
    drifted = drifted.copy()
    drifted[:6] = 0.0
    drifted[6:12] = 1.0
    drifted[12:18, ::2] = drifted[12:18, ::2, ::-1]
    masks = [np.ones(base.shape[:2]), np.ones(base.shape[:2])]

    result = solve_color_match(
        [base, drifted], masks, 0, min_overlap_pixels=128
    )

    np.testing.assert_allclose(result.gains[0], 1.0, atol=0.0)
    np.testing.assert_allclose(result.gains[1], expected, rtol=0.025, atol=0.008)
    assert result.confidence[1] > 0.5
    assert result.diagnostics.overlaps[0].used_pixels < base.shape[0] * base.shape[1]


def test_gains_preserve_luminance_and_respect_strength_and_limits() -> None:
    base = _scene()
    drifted = base / _normalized_gain((0.65, 1.18, 1.25))
    mask = np.ones(base.shape[:2])

    result = solve_color_match(
        [base, drifted],
        [mask, mask],
        0,
        strength=0.5,
        gain_limits=(0.92, 1.08),
        min_overlap_pixels=128,
    )

    weights = np.asarray(DEFAULT_LUMA_WEIGHTS)
    np.testing.assert_allclose(result.gains @ weights, 1.0, atol=1.0e-12)
    assert np.all(result.gains >= 0.92)
    assert np.all(result.gains <= 1.08)
    assert np.linalg.norm(result.gains[1] - 1.0) < np.linalg.norm(
        _normalized_gain((0.65, 1.18, 1.25)) - 1.0
    )


def test_optional_exposure_match_is_only_enabled_when_luminance_is_not_preserved() -> None:
    base = _scene()
    drifted = base / 1.08
    mask = np.ones(base.shape[:2])

    chroma_only = solve_color_match(
        [base, drifted], [mask, mask], 0, min_overlap_pixels=128
    )
    exposure_and_chroma = solve_color_match(
        [base, drifted],
        [mask, mask],
        0,
        preserve_luminance=False,
        min_overlap_pixels=128,
    )

    np.testing.assert_allclose(chroma_only.gains[1], 1.0, atol=1.0e-12)
    np.testing.assert_allclose(exposure_and_chroma.gains[1], 1.08, rtol=1.0e-5)


@pytest.mark.parametrize("camera_count", [3, 5])
def test_propagates_match_through_adjacent_camera_chain(camera_count: int) -> None:
    height = 40
    segment = 32
    overlap = 12
    width = segment + (camera_count - 1) * (segment - overlap)
    base = _scene(height, width)
    masks: list[np.ndarray] = []
    images: list[np.ndarray] = []
    expected: list[np.ndarray] = []
    for camera in range(camera_count):
        start = camera * (segment - overlap)
        mask = np.zeros((height, width), dtype=np.float64)
        mask[:, start : start + segment] = 1.0
        correction = _normalized_gain(
            (1.0 - 0.015 * camera, 1.0 + 0.012 * camera, 1.0 - 0.008 * camera)
        )
        masks.append(mask)
        images.append(base / correction)
        expected.append(correction)

    result = solve_color_match(
        images, masks, 0, min_overlap_pixels=200, gain_limits=(0.8, 1.2)
    )

    assert all(result.diagnostics.connected)
    assert all(value > 0.0 for value in result.confidence)
    np.testing.assert_allclose(result.gains, expected, rtol=0.015, atol=0.006)
    assert not any(
        edge.camera_a == 0 and edge.camera_b == camera_count - 1
        for edge in result.diagnostics.overlaps
    )


def test_insufficient_overlap_leaves_disconnected_camera_at_identity() -> None:
    base = _scene(height=32, width=96)
    mask_a = np.zeros(base.shape[:2])
    mask_b = np.zeros(base.shape[:2])
    mask_c = np.zeros(base.shape[:2])
    mask_a[:, :40] = 1.0
    mask_b[:, 24:64] = 1.0
    mask_c[:, 80:] = 1.0

    result = solve_color_match(
        [base, base / _normalized_gain((0.97, 1.02, 0.98)), base * 1.1],
        [mask_a, mask_b, mask_c],
        0,
        min_overlap_pixels=128,
    )

    assert result.diagnostics.connected == (True, True, False)
    np.testing.assert_allclose(result.gains[2], 1.0, atol=0.0)
    assert result.confidence[2] == 0.0


def test_narrow_conflicting_shortcut_cannot_pull_two_full_seams_off_match() -> None:
    height, width = 40, 140
    base = _scene(height, width)
    corrections = [
        _normalized_gain((1.0, 1.0, 1.0)),
        _normalized_gain((0.98, 1.01, 0.99)),
        _normalized_gain((0.96, 1.02, 1.01)),
    ]
    images = [base / correction for correction in corrections]
    masks = [np.zeros((height, width), dtype=np.float64) for _ in range(3)]
    masks[0][:, :70] = 1.0
    masks[1][:, 40:110] = 1.0
    masks[2][:, 80:] = 1.0

    # A tiny non-adjacent sliver sees unrelated content. It is valid enough to
    # form an edge, but carries far less evidence than either real seam.
    masks[0][:, 80:82] = 1.0
    images[0] = images[0].copy()
    images[0][:, 80:82] *= np.array([1.12, 0.92, 1.08])

    result = solve_color_match(
        images, masks, 0, min_overlap_pixels=64, gain_limits=(0.8, 1.2)
    )

    assert any(
        edge.camera_a == 0 and edge.camera_b == 2
        for edge in result.diagnostics.overlaps
    )
    np.testing.assert_allclose(result.gains, corrections, rtol=0.012, atol=0.004)


def test_identical_cameras_return_identity_deterministically() -> None:
    base = _scene()
    masks = [np.ones(base.shape[:2]) for _ in range(3)]

    first = solve_color_match([base, base.copy(), base.copy()], masks, 1)
    second = solve_color_match([base, base.copy(), base.copy()], masks, 1)

    np.testing.assert_allclose(first.gains, 1.0, atol=1.0e-12)
    np.testing.assert_array_equal(first.gains, second.gains)
    np.testing.assert_array_equal(first.confidence, second.confidence)
