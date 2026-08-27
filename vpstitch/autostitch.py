from __future__ import annotations

import copy
from typing import Any


ROTATION_KEYS = ("yaw_deg", "pitch_deg", "roll_deg")
MANUAL_GEOMETRY_KEYS = (
    "scale",
    "crop_left",
    "crop_right",
    "crop_top",
    "crop_bottom",
    "feather_left_deg",
    "feather_right_deg",
)


def _camera_map(raw: dict[str, Any]) -> dict[str, dict[str, Any]]:
    cameras = raw.get("cameras")
    if not isinstance(cameras, list) or not cameras:
        raise ValueError("Auto Stitch config has no cameras")
    result: dict[str, dict[str, Any]] = {}
    for camera in cameras:
        if not isinstance(camera, dict) or not isinstance(camera.get("name"), str):
            raise ValueError("Auto Stitch cameras must have unique names")
        name = camera["name"]
        if name in result:
            raise ValueError(f"Duplicate Auto Stitch camera name: {name}")
        result[name] = camera
    return result


def _copy_profile_geometry(
    camera: dict[str, Any],
    profile_camera: dict[str, Any],
    *,
    include_rotation: bool,
) -> None:
    keys = MANUAL_GEOMETRY_KEYS
    if include_rotation:
        keys = ROTATION_KEYS + keys
    for key in keys:
        if key in profile_camera:
            camera[key] = copy.deepcopy(profile_camera[key])
        else:
            camera.pop(key, None)

    lens = camera.get("lens")
    profile_lens = profile_camera.get("lens")
    if isinstance(lens, dict) and isinstance(profile_lens, dict):
        if "distortion" in profile_lens:
            lens["distortion"] = copy.deepcopy(profile_lens["distortion"])


def prepare_auto_stitch_config(
    preview_config: dict[str, Any],
    rig_profile: dict[str, Any],
) -> dict[str, Any]:
    """Return a calibration config with prior manual fine tuning removed.

    Preview dimensions and scaled intrinsics are intentionally retained. The rig
    profile contributes only the stable camera geometry used as the solver seed.
    """

    prepared = copy.deepcopy(preview_config)
    profile_cameras = _camera_map(rig_profile)
    for camera in _camera_map(prepared).values():
        name = camera["name"]
        if name not in profile_cameras:
            raise ValueError(f"Rig profile is missing camera {name}")
        _copy_profile_geometry(
            camera,
            profile_cameras[name],
            include_rotation=True,
        )
        camera.pop("auto_stitch_base", None)
        camera.pop("fine_tune", None)
    return prepared


def validate_alignment_report(report: dict[str, Any]) -> dict[str, Any]:
    pairs = report.get("pairs")
    if not isinstance(pairs, list) or not pairs:
        raise ValueError("Auto Stitch report has no adjacent camera results")

    validated: list[dict[str, Any]] = []
    overall = "good"
    for pair in pairs:
        if not isinstance(pair, dict):
            raise ValueError("Auto Stitch report contains an invalid camera pair")
        left = str(pair.get("left_camera", "?"))
        right = str(pair.get("right_camera", "?"))
        matches = int(pair.get("matches", 0))
        inliers = int(pair.get("inliers", 0))
        ratio = float(pair.get("inlier_ratio", 0.0))
        rms = float(pair.get("rms_angular_error_deg", float("inf")))

        if inliers < 25 or ratio < 0.25 or rms > 1.5:
            status = "failed"
            overall = "failed"
        elif inliers < 50 or ratio < 0.5 or rms > 0.85:
            status = "review"
            if overall == "good":
                overall = "review"
        else:
            status = "good"
        validated.append(
            {
                "left_camera": left,
                "right_camera": right,
                "matches": matches,
                "inliers": inliers,
                "inlier_ratio": ratio,
                "rms_angular_error_deg": rms,
                "status": status,
            }
        )
    return {"status": overall, "pairs": validated}


def apply_auto_stitch_solution(
    working_config: dict[str, Any],
    solved_config: dict[str, Any],
    rig_profile: dict[str, Any],
    report: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    validation = validate_alignment_report(report)
    if validation["status"] == "failed":
        failed = [
            f"{pair['left_camera']}/{pair['right_camera']}"
            for pair in validation["pairs"]
            if pair["status"] == "failed"
        ]
        raise ValueError(
            "Auto Stitch rejected unstable adjacent seam(s): " + ", ".join(failed)
        )

    result = copy.deepcopy(working_config)
    solved_cameras = _camera_map(solved_config)
    profile_cameras = _camera_map(rig_profile)
    for camera in _camera_map(result).values():
        name = camera["name"]
        if name not in solved_cameras or name not in profile_cameras:
            raise ValueError(f"Auto Stitch result is missing camera {name}")
        solved = solved_cameras[name]
        _copy_profile_geometry(
            camera,
            profile_cameras[name],
            include_rotation=False,
        )
        for key in ROTATION_KEYS:
            camera[key] = float(solved[key])

        base = {
            key: copy.deepcopy(camera.get(key))
            for key in ROTATION_KEYS + MANUAL_GEOMETRY_KEYS
        }
        lens = camera.get("lens")
        if isinstance(lens, dict):
            base["distortion"] = copy.deepcopy(lens.get("distortion", []))
        camera["auto_stitch_base"] = base
        camera["fine_tune"] = {
            "yaw_offset_deg": 0.0,
            "pitch_offset_deg": 0.0,
            "roll_offset_deg": 0.0,
            "active": False,
        }

    result["auto_stitch"] = {
        "version": 1,
        "validation": copy.deepcopy(validation),
    }
    return result, validation


def update_fine_tune_metadata(camera: dict[str, Any]) -> None:
    base = camera.get("auto_stitch_base")
    if not isinstance(base, dict):
        return
    distortion = camera.get("lens", {}).get("distortion", [])
    base_distortion = base.get("distortion", [])
    count = max(len(distortion), len(base_distortion))
    distortion_offsets = [
        float(distortion[index] if index < len(distortion) else 0.0)
        - float(base_distortion[index] if index < len(base_distortion) else 0.0)
        for index in range(count)
    ]
    fine_tune = {
        "yaw_offset_deg": float(camera.get("yaw_deg", 0.0))
        - float(base.get("yaw_deg", 0.0)),
        "pitch_offset_deg": float(camera.get("pitch_deg", 0.0))
        - float(base.get("pitch_deg", 0.0)),
        "roll_offset_deg": float(camera.get("roll_deg", 0.0))
        - float(base.get("roll_deg", 0.0)),
        "scale": float(camera.get("scale", 1.0)),
        "crop_left": float(camera.get("crop_left", 0.0)),
        "crop_right": float(camera.get("crop_right", 0.0)),
        "crop_top": float(camera.get("crop_top", 0.0)),
        "crop_bottom": float(camera.get("crop_bottom", 0.0)),
        "feather_left_deg": camera.get("feather_left_deg"),
        "feather_right_deg": camera.get("feather_right_deg"),
        "distortion_offsets": distortion_offsets,
    }
    fine_tune["active"] = any(
        abs(float(fine_tune[key])) > 1e-9
        for key in ("yaw_offset_deg", "pitch_offset_deg", "roll_offset_deg")
    ) or any(abs(value) > 1e-9 for value in distortion_offsets)
    base_scale = base.get("scale")
    if abs(float(camera.get("scale", 1.0)) - float(base_scale or 1.0)) > 1e-9:
        fine_tune["active"] = True
    for key in ("crop_left", "crop_right", "crop_top", "crop_bottom"):
        if abs(float(camera.get(key, 0.0)) - float(base.get(key) or 0.0)) > 1e-9:
            fine_tune["active"] = True
    for key in ("feather_left_deg", "feather_right_deg"):
        if camera.get(key) != base.get(key):
            fine_tune["active"] = True
    camera["fine_tune"] = fine_tune
