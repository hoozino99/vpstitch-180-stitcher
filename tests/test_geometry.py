from __future__ import annotations

import numpy as np

from vpstitch.config import Camera, Lens, Output
from vpstitch.geometry import (
    Tile,
    camera_map,
    cylindrical_world_rays,
    cylindrical_rugby_world_rays,
    expand_tile,
    iter_tiles,
    seam_weights,
    tile_count,
    rectilinear_world_rays,
)


def _camera(name: str, yaw: float) -> Camera:
    return Camera(
        name=name,
        width=640,
        height=480,
        yaw_deg=yaw,
        pitch_deg=0,
        roll_deg=0,
        lens=Lens(model="pinhole", fx=320, fy=320, cx=320, cy=240),
    )


def test_15k_tile_plan_covers_exact_output() -> None:
    output = Output(width=15360, height=3968, tile_width=1024, tile_height=512)
    tiles = list(iter_tiles(output))
    assert len(tiles) == tile_count(output) == 120
    assert sum(tile.width * tile.height for tile in tiles) == output.width * output.height


def test_expand_tile_clamps_to_canvas() -> None:
    output = Output(width=1000, height=500, tile_width=256, tile_height=128)
    assert expand_tile(Tile(0, 0, 256, 128), output, 48) == Tile(0, 0, 304, 176)
    assert expand_tile(Tile(900, 450, 100, 50), output, 48) == Tile(852, 402, 148, 98)


def test_center_camera_maps_center_pixel() -> None:
    camera = _camera("center", 0)
    output = Output(
        width=640,
        height=480,
        horizontal_fov_deg=90,
        vertical_fov_deg=73.739795,
        tile_width=640,
        tile_height=480,
    )
    map_x, map_y, valid, _ = camera_map(camera, Tile(0, 0, 640, 480), output)
    assert valid[239, 319]
    assert abs(float(map_x[239, 319]) - 319.5) < 1.0
    assert abs(float(map_y[239, 319]) - 239.5) < 1.0


def test_canvas_center_yaw_targets_matching_camera() -> None:
    camera = _camera("right", 45)
    output = Output(
        width=320,
        height=240,
        horizontal_fov_deg=60,
        vertical_fov_deg=50,
        center_yaw_deg=45,
        tile_width=320,
        tile_height=240,
    )
    map_x, map_y, valid, _ = camera_map(camera, Tile(0, 0, 320, 240), output)
    assert valid[119, 159]
    assert abs(float(map_x[119, 159]) - 319.5) < 1.0
    assert abs(float(map_y[119, 159]) - 239.5) < 1.0


def test_rectilinear_center_ray_is_forward() -> None:
    output = Output(
        width=320,
        height=240,
        horizontal_fov_deg=160,
        vertical_fov_deg=50,
        projection="rectilinear",
        tile_width=320,
        tile_height=240,
    )
    rays, _ = rectilinear_world_rays(Tile(0, 0, 320, 240), output)
    center = rays[119, 159]
    assert center[2] > 0.99
    assert abs(float(center[0])) < 0.01
    assert abs(float(center[1])) < 0.01


def test_cylindrical_rugby_compresses_vertical_scale_at_sides() -> None:
    output = Output(
        width=320,
        height=240,
        horizontal_fov_deg=180,
        vertical_fov_deg=50,
        projection="cylindrical_rugby",
        rugby_strength=0.10,
        tile_width=320,
        tile_height=240,
    )
    rays, _ = cylindrical_rugby_world_rays(Tile(0, 0, 320, 240), output)
    assert abs(float(rays[0, 0, 1])) < abs(float(rays[0, 159, 1]))


def test_cylindrical_rugby_expands_center_relative_to_sides() -> None:
    base = Output(
        width=320,
        height=240,
        horizontal_fov_deg=180,
        vertical_fov_deg=50,
        tile_width=320,
        tile_height=240,
    )
    rugby = Output(
        width=320,
        height=240,
        horizontal_fov_deg=180,
        vertical_fov_deg=50,
        projection="cylindrical_rugby",
        rugby_strength=0.10,
        tile_width=320,
        tile_height=240,
    )
    _, base_longitude = cylindrical_world_rays(Tile(0, 0, 320, 240), base)
    _, rugby_longitude = cylindrical_rugby_world_rays(Tile(0, 0, 320, 240), rugby)
    assert abs(float(rugby_longitude[120, 80])) < abs(float(base_longitude[120, 80]))


def test_seam_weights_are_smooth_and_nonnegative() -> None:
    cameras = tuple(_camera(f"cam{i}", yaw) for i, yaw in enumerate([-90, -45, 0, 45, 90]))
    longitude = np.deg2rad(np.linspace(-90, 90, 1001))[None, :]
    valid = [np.ones_like(longitude, dtype=bool) for _ in cameras]
    weights = seam_weights(cameras, longitude, valid, feather_deg=4)
    total = np.sum(weights, axis=0)
    assert np.all(total > 0)
    assert all(np.all(weight >= 0) for weight in weights)
    center = 500
    assert weights[2][0, center] > 0.99
