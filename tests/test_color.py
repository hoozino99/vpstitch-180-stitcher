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


def test_standard_srgb_viewer_is_a_colorimetric_round_trip() -> None:
    settings = Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScct",
        output_mode="display_view",
        display="sRGB - Display",
        view="Video (colorimetric)",
    )
    pipeline = ColorPipeline(settings, ["sRGB - Display"])
    source = np.array(
        [
            [[0.18, 0.18, 0.18], [0.9, 0.2, 0.1]],
            [[0.1, 0.8, 0.2], [1.0, 1.0, 1.0]],
        ],
        dtype=np.float32,
    )

    output = pipeline.working_to_output(pipeline.input_to_working(0, source))

    np.testing.assert_allclose(output, source, rtol=0.0, atol=4e-5)


def test_pq_and_vlog_delivery_signals_remain_visibly_distinct_from_rec709() -> None:
    ramp = np.linspace(0.0, 1.0, 33, dtype=np.float32)
    source = np.stack([ramp, ramp, ramp], axis=-1)[None, :, :]
    common = {
        "mode": "ocio",
        "ocio_config": BUNDLED_ACES_STUDIO_ID,
        "working_space": "ACEScg",
        "integer_dither": False,
    }
    settings = {
        "rec709": Color(
            **common,
            output_mode="display_view",
            display="sRGB - Display",
            view="Video (colorimetric)",
        ),
        "p3pq": Color(
            **common,
            output_mode="display_view",
            display="ST2084-P3-D65 - Display",
            view="ACES 2.0 - HDR 1000 nits (P3 D65)",
        ),
        "vlog": Color(
            **common,
            output_mode="colorspace",
            output_space="V-Log V-Gamut",
        ),
    }
    outputs: dict[str, np.ndarray] = {}
    for name, color in settings.items():
        pipeline = ColorPipeline(color, ["Camera Rec.709"])
        working = pipeline.input_to_working(0, source.copy())
        outputs[name] = pipeline.working_to_output(working)

    assert np.max(np.abs(outputs["rec709"] - outputs["p3pq"])) > 0.4
    assert np.max(np.abs(outputs["rec709"] - outputs["vlog"])) > 0.3
    assert np.max(np.abs(outputs["p3pq"] - outputs["vlog"])) > 0.1


def test_camera_match_gain_is_applied_in_scene_linear_space_with_strength() -> None:
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
    np.testing.assert_array_equal(source, np.array([[[0.1, 0.2, 0.3]]], np.float32))


def test_camera_match_stays_scene_linear_when_working_space_is_log() -> None:
    source = np.array([[[0.1, 0.2, 0.3]]], dtype=np.float32)
    gain = np.array([1.08, 0.96, 1.02], dtype=np.float32)
    settings = Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScct",
        output_space="Gamma 2.4 Encoded Rec.709",
        match_enabled=True,
        match_space="ACEScg",
        match_strength=0.5,
    )
    actual = ColorPipeline(settings, ["Camera Rec.709"], [tuple(gain)]).input_to_working(
        0, source
    )

    config = load_ocio_config(BUNDLED_ACES_STUDIO_ID)
    linear = source.copy()
    config.getProcessor("Camera Rec.709", "ACEScg").getDefaultCPUProcessor().applyRGB(
        linear
    )
    linear *= np.sqrt(gain).reshape(1, 1, 3)
    config.getProcessor("ACEScg", "ACEScct").getDefaultCPUProcessor().applyRGB(
        linear
    )

    np.testing.assert_allclose(actual, linear, rtol=2e-5, atol=2e-7)


def test_parallel_ocio_output_is_bit_identical_to_single_thread(monkeypatch) -> None:
    settings = Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScct",
        output_mode="display_view",
        display="ST2084-P3-D65 - Display",
        view="ACES 2.0 - HDR 1000 nits (P3 D65)",
    )
    pipeline = ColorPipeline(settings, ["Camera Rec.709"])
    image = np.random.default_rng(19).random((512, 512, 3), dtype=np.float32)

    monkeypatch.setenv("VPSTITCH_OCIO_THREADS", "1")
    single_thread = pipeline.working_to_output(image.copy())
    monkeypatch.setenv("VPSTITCH_OCIO_THREADS", "4")
    parallel = pipeline.working_to_output(image.copy())

    np.testing.assert_array_equal(parallel, single_thread)
