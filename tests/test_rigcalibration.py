from __future__ import annotations

import numpy as np

from vpstitch.config import Camera, Lens
from vpstitch.geometry import camera_to_world
from vpstitch.rigcalibration import (
    angular_residuals,
    pixels_to_rays,
    robust_rotation,
    rotation_to_yaw_pitch_roll,
)


def _camera(yaw: float, pitch: float, roll: float) -> Camera:
    return Camera(
        name="test",
        width=1200,
        height=800,
        yaw_deg=yaw,
        pitch_deg=pitch,
        roll_deg=roll,
        lens=Lens("pinhole", 800.0, 805.0, 600.0, 400.0),
    )


def test_rotation_euler_roundtrip() -> None:
    camera = _camera(47.5, -2.25, 1.75)
    solved = rotation_to_yaw_pitch_roll(camera_to_world(camera))
    assert np.allclose(solved, [camera.yaw_deg, camera.pitch_deg, camera.roll_deg])


def test_pixel_center_maps_to_forward_ray() -> None:
    lens = _camera(0.0, 0.0, 0.0).lens
    ray = pixels_to_rays(np.array([[lens.cx, lens.cy]]), lens)[0]
    assert np.allclose(ray, [0.0, 0.0, 1.0])


def test_robust_rotation_rejects_outliers() -> None:
    generator = np.random.default_rng(12)
    left = generator.normal(size=(500, 3))
    left[:, 2] = np.abs(left[:, 2]) + 0.25
    left /= np.linalg.norm(left, axis=1, keepdims=True)
    target = camera_to_world(_camera(43.0, -1.2, 0.7)).T
    right = left @ target.T
    right += generator.normal(scale=2e-4, size=right.shape)
    right /= np.linalg.norm(right, axis=1, keepdims=True)
    right[:100] = generator.normal(size=(100, 3))
    right[:100] /= np.linalg.norm(right[:100], axis=1, keepdims=True)

    solved, inliers, rms = robust_rotation(
        left, right, threshold_deg=0.25, iterations=800
    )
    assert np.count_nonzero(inliers) >= 395
    assert rms < 0.05
    assert np.rad2deg(np.max(angular_residuals(solved, left[100:], right[100:]))) < 0.1
