from __future__ import annotations

import hashlib
from pathlib import Path

import numpy as np
import PyOpenColorIO as ocio

from vpstitch.color import (
    BUNDLED_ACES_STUDIO_ID,
    ColorPipeline,
    bundled_aces_studio_path,
)
from vpstitch.config import Camera, Color, Lens, Output, RigConfig
from vpstitch.geometry import camera_to_world
from vpstitch.pipeline import Stitcher


def _rig() -> RigConfig:
    width, height = 320, 240
    focal = width / (2.0 * np.tan(np.deg2rad(35.0)))
    cameras = tuple(
        Camera(
            f"cam{index}",
            width,
            height,
            yaw,
            0,
            0,
            Lens("pinhole", focal, focal, width / 2, height / 2),
        )
        for index, yaw in enumerate([-90, -45, 0, 45, 90])
    )
    output = Output(
        width=1024,
        height=256,
        horizontal_fov_deg=180,
        vertical_fov_deg=50,
        tile_width=256,
        tile_height=128,
        seam_feather_deg=3,
    )
    return RigConfig(cameras=cameras, output=output, color=Color(integer_dither=False))


def _world_gradient(camera: Camera) -> np.ndarray:
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
    value = np.clip((longitude + np.pi / 2.0) / np.pi, 0.0, 1.0)
    rgb = np.repeat(value[..., None], 3, axis=2)
    return np.rint(rgb * 65535.0).astype(np.uint16)


def test_five_camera_pipeline_preserves_smooth_16bit_gradient() -> None:
    rig = _rig()
    sources = [_world_gradient(camera) for camera in rig.cameras]
    destination = np.zeros((rig.output.height, rig.output.width, 3), dtype=np.uint16)
    Stitcher(rig).stitch_arrays(sources, destination)
    center = destination[rig.output.height // 2, :, 0].astype(np.float64) / 65535.0
    expected = (np.arange(rig.output.width) + 0.5) / rig.output.width
    assert np.unique(destination[..., 0]).size > 1000
    assert np.mean(destination[..., 0] == 0) == 0
    assert np.mean(np.abs(center - expected)) < 0.001


def test_ocio_raw_roundtrip_uses_float_image_descriptor(tmp_path: Path) -> None:
    config_path = tmp_path / "raw.ocio"
    config_path.write_text(ocio.Config.CreateRaw().serialize(), encoding="utf-8")
    pipeline = ColorPipeline(
        Color(
            mode="ocio",
            ocio_config=str(config_path),
            working_space="raw",
            output_space="raw",
        ),
        ["raw"],
    )
    source = np.random.default_rng(5).random((17, 23, 3), dtype=np.float32)
    output = pipeline.working_to_output(pipeline.input_to_working(0, source))
    np.testing.assert_array_equal(output, source)


def test_ocio_applies_real_input_to_working_transform(tmp_path: Path) -> None:
    config = ocio.Config()
    config.setMajorVersion(2)
    input_space = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE)
    input_space.setName("camera")
    input_space.setTransform(
        ocio.MatrixTransform(
            matrix=[
                2.0, 0.0, 0.0, 0.0,
                0.0, 2.0, 0.0, 0.0,
                0.0, 0.0, 2.0, 0.0,
                0.0, 0.0, 0.0, 1.0,
            ]
        ),
        ocio.COLORSPACE_DIR_TO_REFERENCE,
    )
    working = ocio.ColorSpace(ocio.REFERENCE_SPACE_SCENE)
    working.setName("working")
    config.addColorSpace(input_space)
    config.addColorSpace(working)
    config_path = tmp_path / "transform.ocio"
    config_path.write_text(config.serialize(), encoding="utf-8")
    pipeline = ColorPipeline(
        Color(
            mode="ocio",
            ocio_config=str(config_path),
            working_space="working",
            output_space="working",
        ),
        ["camera"],
    )
    source = np.full((4, 5, 3), 0.25, dtype=np.float32)
    transformed = pipeline.input_to_working(0, source)
    np.testing.assert_allclose(transformed, 0.5, atol=1e-7)


def test_float_destination_does_not_clip_extended_range() -> None:
    rig = _rig()
    sources = [
        _world_gradient(camera).astype(np.float32) / 65535.0 * 3.0 - 0.5
        for camera in rig.cameras
    ]
    destination = np.zeros(
        (rig.output.height, rig.output.width, 3), dtype=np.float16
    )
    Stitcher(rig).stitch_arrays(sources, destination)
    assert float(destination.min()) < 0.0
    assert float(destination.max()) > 1.0


def test_bundled_aces_studio_config_uri() -> None:
    config_path = bundled_aces_studio_path()
    assert config_path.is_file()
    assert hashlib.sha256(config_path.read_bytes()).hexdigest() == (
        "eda5b0008a43b72b98ad540e32eb0eb83b340dde54e35bddba64ccbafac1029a"
    )
    pipeline = ColorPipeline(
        Color(
            mode="ocio",
            ocio_config=BUNDLED_ACES_STUDIO_ID,
            working_space="ACEScg",
            output_space="Gamma 2.4 Encoded Rec.709",
        ),
        ["Camera Rec.709"],
    )
    source = np.full((4, 5, 3), 0.18, dtype=np.float32)
    result = pipeline.working_to_output(pipeline.input_to_working(0, source))
    assert result.dtype == np.float32
    assert result.shape == source.shape
    assert np.all(np.isfinite(result))
