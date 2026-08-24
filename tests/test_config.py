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
