from __future__ import annotations

from copy import deepcopy

import pytest

from vpstitch.autostitch import (
    apply_auto_stitch_solution,
    prepare_auto_stitch_config,
    update_fine_tune_metadata,
    validate_alignment_report,
)


def _config(count: int = 3) -> dict[str, object]:
    return {
        "cameras": [
            {
                "name": f"cam{index}",
                "width": 600,
                "height": 400,
                "yaw_deg": float(index * 40 - 40),
                "pitch_deg": 0.0,
                "roll_deg": 0.0,
                "lens": {
                    "model": "pinhole",
                    "fx": 360.0,
                    "fy": 360.0,
                    "cx": 300.0,
                    "cy": 200.0,
                    "distortion": [0.0, 0.0, 0.0, 0.0],
                },
            }
            for index in range(count)
        ],
        "output": {"seam_feather_deg": 4.0},
    }


def _report(count: int = 3, *, rms: float = 0.4) -> dict[str, object]:
    return {
        "pairs": [
            {
                "left_camera": f"cam{index}",
                "right_camera": f"cam{index + 1}",
                "matches": 160,
                "inliers": 145,
                "inlier_ratio": 145 / 160,
                "rms_angular_error_deg": rms,
                "correction_from_initial_deg": 0.1,
            }
            for index in range(count - 1)
        ]
    }


def test_prepare_auto_stitch_config_removes_prior_manual_geometry() -> None:
    profile = _config()
    preview = deepcopy(profile)
    camera = preview["cameras"][0]
    camera.update(
        {
            "yaw_deg": -75.0,
            "scale": 0.94,
            "crop_left": 0.04,
            "feather_right_deg": 2.0,
            "fine_tune": {"active": True},
        }
    )
    camera["lens"]["distortion"] = [0.1, 0.02, 0.0, 0.0]

    prepared = prepare_auto_stitch_config(preview, profile)

    assert prepared["cameras"][0]["yaw_deg"] == -40.0
    assert "scale" not in prepared["cameras"][0]
    assert "crop_left" not in prepared["cameras"][0]
    assert "feather_right_deg" not in prepared["cameras"][0]
    assert "fine_tune" not in prepared["cameras"][0]
    assert prepared["cameras"][0]["lens"]["distortion"] == [0.0] * 4


def test_apply_auto_stitch_solution_resets_manual_values_and_records_base() -> None:
    profile = _config(5)
    working = deepcopy(profile)
    working["cameras"][0].update(
        {"scale": 0.94, "crop_left": 0.03, "feather_left_deg": 2.0}
    )
    solved = deepcopy(profile)
    solved["cameras"][0]["yaw_deg"] = -41.25

    aligned, validation = apply_auto_stitch_solution(
        working, solved, profile, _report(5)
    )

    first = aligned["cameras"][0]
    assert first["yaw_deg"] == -41.25
    assert "scale" not in first
    assert "crop_left" not in first
    assert "feather_left_deg" not in first
    assert first["auto_stitch_base"]["yaw_deg"] == -41.25
    assert first["fine_tune"]["active"] is False
    assert aligned["auto_stitch"]["validation"]["status"] == "good"
    assert validation["status"] == "good"


def test_fine_tune_metadata_tracks_offsets_without_changing_render_values() -> None:
    profile = _config()
    solved = deepcopy(profile)
    aligned, _ = apply_auto_stitch_solution(profile, solved, profile, _report())
    camera = aligned["cameras"][0]
    camera["yaw_deg"] += 0.2
    camera["scale"] = 0.98

    update_fine_tune_metadata(camera)

    assert camera["yaw_deg"] == pytest.approx(-39.8)
    assert camera["scale"] == 0.98
    assert camera["fine_tune"]["yaw_offset_deg"] == pytest.approx(0.2)
    assert camera["fine_tune"]["active"] is True


def test_alignment_validation_rejects_a_torn_adjacent_pair() -> None:
    report = _report()
    report["pairs"][0]["inliers"] = 22
    report["pairs"][0]["inlier_ratio"] = 0.2
    report["pairs"][0]["rms_angular_error_deg"] = 1.8

    validation = validate_alignment_report(report)
    assert validation["status"] == "failed"
    with pytest.raises(ValueError, match="cam0/cam1"):
        apply_auto_stitch_solution(_config(), _config(), _config(), report)
