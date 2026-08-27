from __future__ import annotations

from dataclasses import replace
from pathlib import Path

import numpy as np

import vpstitch.interactive as interactive
from vpstitch.config import Camera, Color, Lens, Output, RigConfig
from vpstitch.imageio import write_png
from vpstitch.interactive import InteractivePreviewRenderer


def _rig() -> RigConfig:
    cameras = tuple(
        Camera(
            name=f"cam{index}",
            width=160,
            height=120,
            yaw_deg=yaw,
            pitch_deg=0.0,
            roll_deg=0.0,
            lens=Lens(
                model="pinhole",
                fx=95.0,
                fy=95.0,
                cx=80.0,
                cy=60.0,
            ),
        )
        for index, yaw in enumerate((-45.0, 0.0, 45.0))
    )
    return RigConfig(
        cameras=cameras,
        output=Output(
            width=640,
            height=240,
            horizontal_fov_deg=120.0,
            vertical_fov_deg=50.0,
            tile_width=320,
            tile_height=120,
        ),
        color=Color(integer_dither=False),
    )


def _plates(tmp_path: Path) -> list[Path]:
    paths = []
    for index in range(3):
        x = np.linspace(0.1, 0.9, 160, dtype=np.float32)
        plate = np.empty((120, 160, 3), dtype=np.float32)
        plate[..., 0] = x[None, :]
        plate[..., 1] = 0.2 + index * 0.25
        plate[..., 2] = 0.8 - x[None, :] * 0.5
        path = tmp_path / f"cam{index}.png"
        write_png(path, np.rint(plate * 65535.0).astype(np.uint16))
        paths.append(path)
    return paths


def test_interactive_renderer_reuses_sources_and_only_rewarps_changed_camera(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rig = _rig()
    paths = _plates(tmp_path)
    renderer = InteractivePreviewRenderer(max_width=320, max_height=180)
    reads = 0
    remaps = 0
    real_read = interactive.read_image
    real_remap = interactive.cv2.remap

    def tracked_read(path):  # type: ignore[no-untyped-def]
        nonlocal reads
        reads += 1
        return real_read(path)

    def tracked_remap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal remaps
        remaps += 1
        return real_remap(*args, **kwargs)

    monkeypatch.setattr(interactive, "read_image", tracked_read)
    monkeypatch.setattr(interactive.cv2, "remap", tracked_remap)

    first = renderer.render(rig, paths)
    changed_camera = replace(rig.cameras[1], yaw_deg=2.0)
    changed = replace(
        rig,
        cameras=(rig.cameras[0], changed_camera, rig.cameras[2]),
    )
    second = renderer.render(changed, paths)

    assert first.shape == (120, 320, 3)
    assert first.dtype == np.uint16
    assert reads == 3
    assert remaps == 4
    assert not np.array_equal(first, second)


def test_feather_change_reblends_without_rewarping_sources(
    tmp_path: Path,
    monkeypatch,
) -> None:
    rig = _rig()
    paths = _plates(tmp_path)
    renderer = InteractivePreviewRenderer(max_width=320, max_height=180)
    renderer.render(rig, paths)
    remaps = 0
    real_remap = interactive.cv2.remap

    def tracked_remap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal remaps
        remaps += 1
        return real_remap(*args, **kwargs)

    monkeypatch.setattr(interactive.cv2, "remap", tracked_remap)
    feathered = replace(
        rig,
        cameras=(
            replace(rig.cameras[0], feather_right_deg=8.0),
            rig.cameras[1],
            rig.cameras[2],
        ),
    )
    renderer.render(feathered, paths)

    assert remaps == 0


def test_frame_bundles_reuse_geometry_maps_but_remap_every_camera(
    monkeypatch,
) -> None:
    rig = _rig()
    renderer = InteractivePreviewRenderer(max_width=320, max_height=180)
    frames = [
        np.full((camera.height, camera.width, 3), index * 10000, dtype=np.uint16)
        for index, camera in enumerate(rig.cameras, start=1)
    ]
    maps = 0
    remaps = 0
    real_map = interactive.camera_map
    real_remap = interactive.cv2.remap

    def tracked_map(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal maps
        maps += 1
        return real_map(*args, **kwargs)

    def tracked_remap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal remaps
        remaps += 1
        return real_remap(*args, **kwargs)

    monkeypatch.setattr(interactive, "camera_map", tracked_map)
    monkeypatch.setattr(interactive.cv2, "remap", tracked_remap)

    first = renderer.render_frames(rig, frames, frame_token=10)
    frames[0] = np.full_like(frames[0], 50000)
    second = renderer.render_frames(rig, frames, frame_token=11)

    assert maps == len(rig.cameras)
    assert remaps == len(rig.cameras) * 2
    assert not np.array_equal(first, second)


def test_same_stream_frame_reuses_working_pixels_for_output_setting_change(
    monkeypatch,
) -> None:
    rig = _rig()
    renderer = InteractivePreviewRenderer(max_width=320, max_height=180)
    frames = [
        np.full((camera.height, camera.width, 3), index * 10000, dtype=np.uint16)
        for index, camera in enumerate(rig.cameras, start=1)
    ]
    remaps = 0
    real_remap = interactive.cv2.remap

    def tracked_remap(*args, **kwargs):  # type: ignore[no-untyped-def]
        nonlocal remaps
        remaps += 1
        return real_remap(*args, **kwargs)

    monkeypatch.setattr(interactive.cv2, "remap", tracked_remap)
    renderer.render_frames(rig, frames, frame_token=12)
    changed = replace(rig, color=replace(rig.color, integer_dither=True))
    renderer.render_frames(changed, frames, frame_token=12)

    assert remaps == len(rig.cameras)
