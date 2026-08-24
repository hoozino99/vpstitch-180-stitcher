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
    tile = Tile(64, 32, 64, 32)
    cached = cache.tile_maps(tile)[0]
    direct = camera_map(camera, tile, output)
    np.testing.assert_allclose(cached[0], np.where(direct[2], direct[0], -1.0))
    np.testing.assert_allclose(cached[1], np.where(direct[2], direct[1], -1.0))
    np.testing.assert_array_equal(cached[2], direct[2])
    np.testing.assert_allclose(cached[3], direct[3])

