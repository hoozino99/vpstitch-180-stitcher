from __future__ import annotations

import json
import sys
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import cv2
import pytest

import vpstitch.cli as cli
from vpstitch.cli import main
from vpstitch.config import Camera, Color, Lens, Output, RigConfig, Video
from vpstitch.ffmpegio import VideoDecoder, VideoEncoder, probe_video
from vpstitch.geometry import camera_to_world


class _TrackedResource:
    def __init__(self) -> None:
        self.close_count = 0

    def close(self) -> None:
        self.close_count += 1


class _TrackedMapCache(_TrackedResource):
    instances: list["_TrackedMapCache"] = []

    def __init__(self, *_args: object, **_kwargs: object) -> None:
        super().__init__()
        self.instances.append(self)

    def open(self, **_kwargs: object) -> "_TrackedMapCache":
        return self


def _patch_render_initialization(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    cameras = tuple(_source_camera(index, 64, 32) for index in range(3))
    config = RigConfig(
        cameras=cameras,
        output=Output(width=96, height=32),
        color=Color(),
        video=Video(fps=24, frames=1, output_codec="ffv1-16"),
    )
    monkeypatch.setattr(cli, "_apply_canvas_overrides", lambda _config, _args: config)
    monkeypatch.setattr(cli, "load_config", lambda _path: config)
    monkeypatch.setattr(
        cli,
        "probe_video",
        lambda _path: SimpleNamespace(fps=24.0, bit_depth=8),
    )
    monkeypatch.setattr(cli, "interpret_input_probes", lambda probes, _config: probes)
    monkeypatch.setattr(
        cli,
        "assess_inputs",
        lambda *_args, **_kwargs: SimpleNamespace(issues=[]),
    )
    monkeypatch.setattr(
        cli,
        "resolve_passthrough_video",
        lambda video, _probes: video,
    )
    monkeypatch.setattr(cli, "_planned_decoder_starts", lambda *_args: (None, None))
    _TrackedMapCache.instances.clear()
    monkeypatch.setattr(cli, "MapCache", _TrackedMapCache)
    monkeypatch.setattr(cli, "Stitcher", lambda *_args, **_kwargs: object())
    return SimpleNamespace(
        config="rig.json",
        frames=None,
        inputs=["p01.mov", "p02.mov", "p03.mov"],
        allow_low_bit_depth=True,
        alignment_plan=None,
        start_frame=0,
        decode_scale=1.0,
        map_cache="maps",
        output="stitched.mkv",
    )


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
    tmp_path: Path, monkeypatch, capsys
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
    alignment_path = tmp_path / "alignment.json"
    alignment_path.write_text(
        json.dumps(
            {
                "fps": 24.0,
                "common_frames": 2,
                "inputs": [
                    {
                        "path": str(path),
                        "skip_frames": 0,
                        "frame_count": 2,
                    }
                    for path in source_paths
                ],
            }
        ),
        encoding="utf-8",
    )
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
        "--alignment-plan",
        str(alignment_path),
        *[str(path) for path in source_paths],
    ]
    monkeypatch.setattr(sys, "argv", arguments)
    assert main() == 0
    output = capsys.readouterr().out
    assert "progress frames 0/2" in output
    assert "progress frames 1/2" in output
    assert "progress frames 2/2" in output

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


def test_extract_reference_scales_before_writing_preview_frame(
    tmp_path: Path, monkeypatch
) -> None:
    width, height = 64, 32
    camera = _source_camera(2, width, height)
    source = tmp_path / "cam.mkv"
    encoder = VideoEncoder(
        source,
        width,
        height,
        Video(fps=24, frames=1, output_codec="ffv1-16"),
    )
    encoder.write(_world_gradient(camera, 0))
    encoder.close()
    config = {
        "cameras": [
            {
                "name": camera.name,
                "width": width,
                "height": height,
                "yaw_deg": 0,
                "lens": {
                    "model": "pinhole",
                    "fx": camera.lens.fx,
                    "fy": camera.lens.fy,
                    "cx": camera.lens.cx,
                    "cy": camera.lens.cy,
                },
            }
        ],
        "output": {"width": 64, "height": 32, "horizontal_fov_deg": 72},
        "color": {"mode": "passthrough"},
        "video": {"fps": 24, "frames": 1, "output_codec": "ffv1-16"},
    }
    config_path = tmp_path / "rig.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    output = tmp_path / "references"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "vpstitch",
            "extract-reference",
            "--config",
            str(config_path),
            "--time",
            "0",
            "--scale",
            "0.5",
            "--output-dir",
            str(output),
            str(source),
        ],
    )
    assert main() == 0
    reference_bgr = cv2.imread(
        str(output / f"{camera.name}.png"), cv2.IMREAD_UNCHANGED
    )
    assert reference_bgr is not None
    reference = cv2.cvtColor(reference_bgr, cv2.COLOR_BGR2RGB)
    assert reference.shape == (16, 32, 3)
    assert reference.dtype == np.uint16
    manifest = json.loads((output / "reference_manifest.json").read_text())
    assert manifest["reference_scale"] == 0.5


def test_render_closes_started_decoders_when_later_decoder_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _patch_render_initialization(monkeypatch)
    decoders: list[_TrackedResource] = []

    def decoder_factory(*_args: object, **_kwargs: object) -> _TrackedResource:
        if len(decoders) == 2:
            raise OSError("third decoder failed")
        decoder = _TrackedResource()
        decoders.append(decoder)
        return decoder

    monkeypatch.setattr(cli, "VideoDecoder", decoder_factory)

    with pytest.raises(OSError, match="third decoder"):
        cli._stitch_video(args)

    assert [decoder.close_count for decoder in decoders] == [1, 1]
    assert _TrackedMapCache.instances[0].close_count == 1


def test_render_closes_decoders_and_cache_when_encoder_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _patch_render_initialization(monkeypatch)
    decoders: list[_TrackedResource] = []

    def decoder_factory(*_args: object, **_kwargs: object) -> _TrackedResource:
        decoder = _TrackedResource()
        decoders.append(decoder)
        return decoder

    monkeypatch.setattr(cli, "VideoDecoder", decoder_factory)

    def fail_encoder(*_args: object, **_kwargs: object) -> _TrackedResource:
        raise OSError("encoder failed")

    monkeypatch.setattr(cli, "VideoEncoder", fail_encoder)

    with pytest.raises(OSError, match="encoder failed"):
        cli._stitch_video(args)

    assert [decoder.close_count for decoder in decoders] == [1, 1, 1]
    assert _TrackedMapCache.instances[0].close_count == 1


def test_render_closes_every_resource_when_destination_creation_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args = _patch_render_initialization(monkeypatch)
    decoders: list[_TrackedResource] = []
    encoder = _TrackedResource()

    def decoder_factory(*_args: object, **_kwargs: object) -> _TrackedResource:
        decoder = _TrackedResource()
        decoders.append(decoder)
        return decoder

    def fail_memmap(*_args: object, **_kwargs: object) -> np.memmap:
        raise OSError("memmap failed")

    monkeypatch.setattr(cli, "VideoDecoder", decoder_factory)
    monkeypatch.setattr(cli, "VideoEncoder", lambda *_args, **_kwargs: encoder)
    monkeypatch.setattr(cli.np, "memmap", fail_memmap)

    with pytest.raises(OSError, match="memmap failed"):
        cli._stitch_video(args)

    assert [decoder.close_count for decoder in decoders] == [1, 1, 1]
    assert encoder.close_count == 1
    assert _TrackedMapCache.instances[0].close_count == 1
