from __future__ import annotations

import numpy as np

from vpstitch.color import BUNDLED_ACES_STUDIO_ID, ColorPipeline, load_ocio_config
from vpstitch.config import Color


def test_bundled_aces_exposes_required_hdr_delivery_views() -> None:
    config = load_ocio_config(BUNDLED_ACES_STUDIO_ID)
    p3_views = set(config.getViews("ST2084-P3-D65 - Display"))
    pq_views = set(config.getViews("Rec.2100-PQ - Display"))

    assert "ACES 2.0 - HDR 1000 nits (P3 D65)" in p3_views
    assert "ACES 2.0 - HDR 1000 nits (Rec.2020)" in pq_views


def test_display_view_pipeline_builds_and_processes_hdr_pixels() -> None:
    settings = Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScg",
        output_mode="display_view",
        display="Rec.2100-PQ - Display",
        view="ACES 2.0 - HDR 1000 nits (Rec.2020)",
    )
    pipeline = ColorPipeline(settings, ["Camera Rec.709"])
    image = np.full((4, 6, 3), 0.18, dtype=np.float32)
    original = image.copy()

    output = pipeline.working_to_output(image)

    assert output.shape == image.shape
    assert output.dtype == np.float32
    assert np.all(np.isfinite(output))
    assert not np.allclose(output, original)


def test_camera_match_gain_is_applied_in_working_space_with_strength() -> None:
    source = np.array([[[0.1, 0.2, 0.3]]], dtype=np.float32)
    base_settings = Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScg",
        output_space="Gamma 2.4 Encoded Rec.709",
    )
    base = ColorPipeline(base_settings, ["Camera Rec.709"]).input_to_working(
        0, source
    )
    matched = ColorPipeline(
        Color(
            **{
                **base_settings.__dict__,
                "match_enabled": True,
                "match_strength": 0.5,
            }
        ),
        ["Camera Rec.709"],
        [(1.08, 0.96, 1.02)],
    ).input_to_working(0, source)

    expected_gain = np.sqrt(np.array([1.08, 0.96, 1.02], dtype=np.float32))
    np.testing.assert_allclose(matched, base * expected_gain, rtol=1e-5, atol=1e-7)
