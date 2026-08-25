from __future__ import annotations

import json
from pathlib import Path

import pytest

from vpstitch.config import ConfigError, load_config


def test_sample_config_is_15k_five_camera_passthrough() -> None:
    config = load_config(Path("configs/five_cam_180.sample.json"))
    assert len(config.cameras) == 5
    assert config.output.width == 15360
    assert config.color.mode == "passthrough"
    assert config.video is not None
    assert config.video.output_codec == "ffv1-16"


def test_rejects_hevc_canvas_above_standard_picture_level(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["output"]["width"] = 15360
    source["output"]["height"] = 3968
    source["video"]["output_codec"] = "hevc-444-10"
    path = tmp_path / "hevc-too-large.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ConfigError, match="HEVC picture level"):
        load_config(path)


def test_accepts_prores_h264_mp4_and_dpx_outputs(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["output"]["width"] = 1920
    source["output"]["height"] = 1080
    for codec in (
        "prores-hq",
        "h264-mp4-10",
        "h264-proxy",
        "dpx12-sequence",
    ):
        source["video"]["output_codec"] = codec
        path = tmp_path / f"{codec}.json"
        path.write_text(json.dumps(source), encoding="utf-8")
        assert load_config(path).video.output_codec == codec


def test_rejects_removed_tiff_sequence_output(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["video"]["output_codec"] = "tiff16-sequence"
    path = tmp_path / "removed-tiff-output.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ConfigError, match="video.output_codec"):
        load_config(path)


def test_accepts_rectilinear_projection(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["output"]["projection"] = "rectilinear"
    path = tmp_path / "rectilinear.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    assert load_config(path).output.projection == "rectilinear"


def test_accepts_cylindrical_rugby_projection(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["output"]["projection"] = "cylindrical_rugby"
    source["output"]["rugby_strength"] = 0.10
    path = tmp_path / "cylindrical-rugby.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    config = load_config(path)
    assert config.output.projection == "cylindrical_rugby"
    assert config.output.rugby_strength == 0.10


def test_validates_per_camera_input_interpretation(tmp_path: Path) -> None:
    source = json.loads(Path("configs/five_cam_180.sample.json").read_text(encoding="utf-8"))
    source["cameras"][0]["input_color_space"] = "bt709"
    source["cameras"][0]["input_video_range"] = "tv"
    path = tmp_path / "interpreted.json"
    path.write_text(json.dumps(source), encoding="utf-8")
    camera = load_config(path).cameras[0]
    assert camera.input_color_space == "bt709"
    assert camera.input_video_range == "tv"

    source["cameras"][0]["input_video_range"] = "broadcast-ish"
    path.write_text(json.dumps(source), encoding="utf-8")
    with pytest.raises(ConfigError, match="input_video_range"):
        load_config(path)
