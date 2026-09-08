from __future__ import annotations

import numpy as np

from vpstitch.config import Camera, Lens
from vpstitch.geometry import camera_to_world
from vpstitch.rigcalibration import (
    _refine_seam_rotation,
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


def _seam_correspondences(count: int = 80):
    rng = np.random.default_rng(34)
    left_camera, right_camera = _camera(-38, 0, 0), _camera(0, 0, 0)
    longitude = np.deg2rad(rng.uniform(-22, -16, count))
    world = np.column_stack((np.sin(longitude), rng.uniform(-0.15, 0.15, count), np.cos(longitude)))
    world /= np.linalg.norm(world, axis=1, keepdims=True)
    left = world @ camera_to_world(left_camera)
    right = world + rng.normal(0, 1e-4, world.shape)
    right /= np.linalg.norm(right, axis=1, keepdims=True)
    return left_camera, right_camera, left, right


def test_seam_refinement_corrects_background_bias_without_per_frame_flow() -> None:
    a, b, left, right = _seam_correspondences()
    biased = camera_to_world(_camera(-38.6, 0.05, 0))
    result = _refine_seam_rotation(left, right, a, b, biased, 4, 1.25)
    assert result is not None
    refined, _, _, before, after = result
    assert before > 0.5
    assert after < 0.02
    assert np.rad2deg(np.max(angular_residuals(refined, left, right))) < 0.03


def test_seam_refinement_keeps_global_fit_when_support_is_sparse() -> None:
    a, b, left, right = _seam_correspondences(29)
    assert _refine_seam_rotation(
        left, right, a, b, camera_to_world(_camera(-38.6, 0, 0)), 4, 1.25
    ) is None


def test_seam_refinement_does_not_replace_an_already_accurate_fit() -> None:
    a, b, left, right = _seam_correspondences()
    assert _refine_seam_rotation(
        left, right, a, b, camera_to_world(a), 4, 1.25
    ) is None


def test_seam_refinement_rejects_a_large_alternative_rotation() -> None:
    a, b, left, right = _seam_correspondences()
    assert _refine_seam_rotation(
        left, right, a, b, camera_to_world(_camera(-41, 0, 0)), 4, 1.25
    ) is None
