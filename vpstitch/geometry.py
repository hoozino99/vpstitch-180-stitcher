from __future__ import annotations

from dataclasses import dataclass
from math import ceil

import cv2
import numpy as np

from .config import Camera, Output


@dataclass(frozen=True)
class Tile:
    x: int
    y: int
    width: int
    height: int


def iter_tiles(output: Output):
    for y in range(0, output.height, output.tile_height):
        height = min(output.tile_height, output.height - y)
        for x in range(0, output.width, output.tile_width):
            width = min(output.tile_width, output.width - x)
            yield Tile(x=x, y=y, width=width, height=height)


def tile_count(output: Output) -> int:
    return ceil(output.width / output.tile_width) * ceil(output.height / output.tile_height)


def expand_tile(tile: Tile, output: Output, margin: int) -> Tile:
    if margin < 0:
        raise ValueError("tile margin cannot be negative")
    left = max(0, tile.x - margin)
    top = max(0, tile.y - margin)
    right = min(output.width, tile.x + tile.width + margin)
    bottom = min(output.height, tile.y + tile.height + margin)
    return Tile(left, top, right - left, bottom - top)


def _rot_x(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=np.float64)


def _rot_y(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=np.float64)


def _rot_z(angle: float) -> np.ndarray:
    c, s = np.cos(angle), np.sin(angle)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=np.float64)


def camera_to_world(camera: Camera) -> np.ndarray:
    yaw, pitch, roll = np.deg2rad(
        [camera.yaw_deg, camera.pitch_deg, camera.roll_deg]
    )
    return _rot_y(yaw) @ _rot_x(pitch) @ _rot_z(roll)


def cylindrical_world_rays(tile: Tile, output: Output) -> tuple[np.ndarray, np.ndarray]:
    xs = np.arange(tile.x, tile.x + tile.width, dtype=np.float64) + 0.5
    ys = np.arange(tile.y, tile.y + tile.height, dtype=np.float64) + 0.5
    local_longitude = (
        (xs / output.width - 0.5) * np.deg2rad(output.horizontal_fov_deg)
    )
    vertical_extent = np.tan(np.deg2rad(output.vertical_fov_deg) * 0.5)
    vertical = (ys / output.height - 0.5) * (2.0 * vertical_extent)
    lon, vert = np.meshgrid(local_longitude, vertical)
    rays = np.stack([np.sin(lon), vert, np.cos(lon)], axis=-1)
    rays /= np.linalg.norm(rays, axis=-1, keepdims=True)
    view_rotation = _rot_y(np.deg2rad(output.center_yaw_deg)) @ _rot_x(
        np.deg2rad(output.center_pitch_deg)
    )
    rays = rays @ view_rotation.T
    world_longitude = np.arctan2(rays[..., 0], rays[..., 2])
    return rays, world_longitude


def camera_map(
    camera: Camera, tile: Tile, output: Output
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rays_world, longitude = cylindrical_world_rays(tile, output)
    rotation = camera_to_world(camera)
    rays_camera = rays_world @ rotation
    x = rays_camera[..., 0]
    y = rays_camera[..., 1]
    z = rays_camera[..., 2]
    lens = camera.lens

    if lens.model == "pinhole":
        safe_z = np.where(np.abs(z) < 1e-9, 1e-9, z)
        normalized_x = x / safe_z
        normalized_y = y / safe_z
        k1, k2, p1, p2 = lens.distortion
        radius2 = normalized_x**2 + normalized_y**2
        radial = 1.0 + k1 * radius2 + k2 * radius2**2
        distorted_x = (
            normalized_x * radial
            + 2.0 * p1 * normalized_x * normalized_y
            + p2 * (radius2 + 2.0 * normalized_x**2)
        )
        distorted_y = (
            normalized_y * radial
            + p1 * (radius2 + 2.0 * normalized_y**2)
            + 2.0 * p2 * normalized_x * normalized_y
        )
        map_x = lens.fx * distorted_x + lens.cx
        map_y = lens.fy * distorted_y + lens.cy
        valid = z > 0.0
    else:
        rho = np.sqrt(x * x + y * y)
        theta = np.arctan2(rho, z)
        k1, k2, k3, k4 = lens.distortion
        theta2 = theta * theta
        theta_d = theta * (
            1.0
            + k1 * theta2
            + k2 * theta2**2
            + k3 * theta2**3
            + k4 * theta2**4
        )
        scale = np.divide(theta_d, rho, out=np.ones_like(theta_d), where=rho > 1e-9)
        map_x = lens.fx * x * scale + lens.cx
        map_y = lens.fy * y * scale + lens.cy
        valid = theta < np.pi

    valid &= (map_x >= 0.0) & (map_x <= camera.width - 1.001)
    valid &= (map_y >= 0.0) & (map_y <= camera.height - 1.001)
    if lens.circle_radius is not None:
        valid &= (
            (map_x - lens.cx) ** 2 + (map_y - lens.cy) ** 2
            <= lens.circle_radius**2
        )
    return (
        map_x.astype(np.float32),
        map_y.astype(np.float32),
        valid,
        longitude,
    )


def remap_camera(image: np.ndarray, map_x: np.ndarray, map_y: np.ndarray) -> np.ndarray:
    return cv2.remap(
        image,
        map_x,
        map_y,
        interpolation=cv2.INTER_LANCZOS4,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def seam_weights(
    cameras: tuple[Camera, ...],
    longitude: np.ndarray,
    valid_masks: list[np.ndarray],
    feather_deg: float,
) -> list[np.ndarray]:
    order = np.argsort([camera.yaw_deg for camera in cameras])
    yaws = np.deg2rad(np.array([cameras[i].yaw_deg for i in order], dtype=np.float64))
    boundaries = (yaws[:-1] + yaws[1:]) * 0.5
    feather = max(np.deg2rad(feather_deg), 1e-7)
    weights_ordered: list[np.ndarray] = []

    for rank, camera_index in enumerate(order):
        weight = np.ones_like(longitude, dtype=np.float32)
        if rank > 0:
            left = boundaries[rank - 1]
            t = np.clip((longitude - (left - feather)) / (2.0 * feather), 0.0, 1.0)
            weight *= (t * t * (3.0 - 2.0 * t)).astype(np.float32)
        if rank < len(order) - 1:
            right = boundaries[rank]
            t = np.clip(((right + feather) - longitude) / (2.0 * feather), 0.0, 1.0)
            weight *= (t * t * (3.0 - 2.0 * t)).astype(np.float32)
        weight *= valid_masks[camera_index].astype(np.float32)
        weights_ordered.append(weight)

    result = [np.zeros_like(longitude, dtype=np.float32) for _ in cameras]
    for rank, camera_index in enumerate(order):
        result[camera_index] = weights_ordered[rank]
    return result
