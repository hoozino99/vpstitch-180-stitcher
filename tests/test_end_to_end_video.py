from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np

from vpstitch.cli import main
from vpstitch.config import Camera, Lens, Video
from vpstitch.ffmpegio import VideoDecoder, VideoEncoder, probe_video
from vpstitch.geometry import camera_to_world


def _source_camera(index: int, width: int, height: int) -> Camera:
    yaw = [-90.0, -45.0, 0.0, 45.0, 90.0][index]
    focal = width / (2.0 * np.tan(np.deg2rad(36.0)))
    return Camera(
        f"cam{index}",
        width,
        height,
        yaw,
        0.0,
        0.0,
        Lens("pinhole", focal, focal, width / 2.0, height / 2.0),
    )


def _world_gradient(camera: Camera, frame_index: int) -> np.ndarray:
    u, v = np.meshgrid(
        np.arange(camera.width, dtype=np.float64) + 0.5,
        np.arange(camera.height, dtype=np.float64) + 0.5,
    )
    rays = np.stack(
        [
            (u - camera.lens.cx) / camera.lens.fx,
            (v - camera.lens.cy) / camera.lens.fy,
            np.ones_like(u),
        ],
        axis=-1,
    )
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    world = rays @ camera_to_world(camera).T
    longitude = np.arctan2(world[..., 0], world[..., 2])
    horizontal = np.clip((longitude + np.pi / 2.0) / np.pi, 0.0, 1.0)
    vertical = np.broadcast_to(v / camera.height, horizontal.shape)
    blue = np.full_like(horizontal, 0.2 + frame_index * 0.05)
    rgb = np.stack([horizontal, vertical, blue], axis=-1)
    return np.rint(rgb * 65535.0).astype(np.uint16)


def test_five_video_cli_roundtrip_is_16bit_and_smooth(
    tmp_path: Path, monkeypatch
) -> None:
    source_width, source_height = 320, 240
    output_width, output_height = 1024, 256
    cameras = [_source_camera(index, source_width, source_height) for index in range(5)]
    source_paths: list[Path] = []
    for index, camera in enumerate(cameras):
        path = tmp_path / f"cam{index}.mkv"
        encoder = VideoEncoder(
            path,
            source_width,
            source_height,
            Video(fps=24, frames=2, output_codec="ffv1-16"),
        )
        encoder.write(_world_gradient(camera, 0))
        encoder.write(_world_gradient(camera, 1))
        encoder.close()
        source_paths.append(path)

    config = {
        "cameras": [
            {
                "name": camera.name,
                "width": camera.width,
                "height": camera.height,
                "yaw_deg": camera.yaw_deg,
                "pitch_deg": camera.pitch_deg,
                "roll_deg": camera.roll_deg,
                "lens": {
                    "model": camera.lens.model,
                    "fx": camera.lens.fx,
                    "fy": camera.lens.fy,
                    "cx": camera.lens.cx,
                    "cy": camera.lens.cy,
                    "distortion": list(camera.lens.distortion),
                },
            }
            for camera in cameras
        ],
        "output": {
            "width": output_width,
            "height": output_height,
            "horizontal_fov_deg": 180,
            "vertical_fov_deg": 50,
            "tile_width": 256,
            "tile_height": 128,
            "seam_feather_deg": 4,
        },
        "color": {"mode": "passthrough", "integer_dither": False},
        "video": {"fps": 24, "frames": 2, "output_codec": "ffv1-16"},
    }
    config_path = tmp_path / "rig.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output_path = tmp_path / "stitched.mkv"
    arguments = [
        "vpstitch",
        "stitch-video",
        "--config",
        str(config_path),
        "--output",
        str(output_path),
        "--map-cache",
        str(tmp_path / "maps"),
        *[str(path) for path in source_paths],
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    assert main() == 0

    probe = probe_video(output_path)
    assert probe.bit_depth == 16
    assert (probe.width, probe.height) == (output_width, output_height)
    decoder = VideoDecoder(
        output_path,
        Camera(
            "output",
            output_width,
            output_height,
            0,
            0,
            0,
            Lens("pinhole", 1, 1, 0, 0),
        ),
        24,
    )
    first = decoder.read()
    second = decoder.read()
    decoder.close()
    assert first is not None and second is not None
    assert np.unique(first[..., 0]).size > 900
    assert np.count_nonzero(first == 0) == 0
    assert np.mean(second[..., 2]) > np.mean(first[..., 2])
