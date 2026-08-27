from __future__ import annotations

from pathlib import Path

import numpy as np

from vpstitch.config import Camera, Lens, Output, RigConfig
from vpstitch.geometry import Tile, camera_map
from vpstitch.mapcache import MapCache


def test_map_cache_matches_direct_projection(tmp_path: Path) -> None:
    camera = Camera(
        "center", 160, 120, 0, 0, 0, Lens("pinhole", 100, 100, 80, 60)
    )
    output = Output(
        width=128,
        height=64,
        horizontal_fov_deg=80,
        vertical_fov_deg=50,
        tile_width=64,
        tile_height=32,
    )
    config = RigConfig(cameras=(camera,), output=output)
    cache = MapCache(config, tmp_path).open()
    assert cache.streaming is False
    tile = Tile(64, 32, 64, 32)
    cached = cache.tile_maps(tile)[0]
    direct = camera_map(camera, tile, output)
    np.testing.assert_allclose(cached[0], np.where(direct[2], direct[0], -1.0))
    np.testing.assert_allclose(cached[1], np.where(direct[2], direct[1], -1.0))
    np.testing.assert_array_equal(cached[2], direct[2])
    np.testing.assert_allclose(cached[3], direct[3])


def test_large_map_cache_streams_owned_tiles(tmp_path: Path) -> None:
    camera = Camera(
        "center", 160, 120, 0, 0, 0, Lens("pinhole", 100, 100, 80, 60)
    )
    output = Output(
        width=128,
        height=64,
        horizontal_fov_deg=80,
        vertical_fov_deg=50,
        tile_width=64,
        tile_height=32,
    )
    config = RigConfig(cameras=(camera,), output=output)
    cache = MapCache(config, tmp_path, max_persistent_bytes=0).open()
    tile = Tile(64, 32, 64, 32)

    first = cache.tile_maps(tile)[0]
    second = cache.tile_maps(tile)[0]
    direct = camera_map(camera, tile, output)

    assert cache.streaming is True
    assert first[0].flags.owndata
    assert first[1].flags.owndata
    assert not isinstance(first[0], np.memmap)
    np.testing.assert_allclose(first[0], np.where(direct[2], direct[0], -1.0))
    np.testing.assert_allclose(first[1], np.where(direct[2], direct[1], -1.0))
    np.testing.assert_array_equal(first[0], second[0])
    np.testing.assert_array_equal(first[1], second[1])
    cache.close()


def test_map_cache_rejects_negative_persistent_limit(tmp_path: Path) -> None:
    camera = Camera(
        "center", 160, 120, 0, 0, 0, Lens("pinhole", 100, 100, 80, 60)
    )
    config = RigConfig(cameras=(camera,), output=Output(width=128, height=64))
    with np.testing.assert_raises(ValueError):
        MapCache(config, tmp_path, max_persistent_bytes=-1)
