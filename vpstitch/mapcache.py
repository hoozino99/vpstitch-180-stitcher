from __future__ import annotations

import hashlib
import json
from dataclasses import asdict
from pathlib import Path

import numpy as np

from .config import RigConfig
from .geometry import Tile, camera_map, iter_tiles


CACHE_VERSION = 1


def geometry_key(config: RigConfig) -> str:
    payload = {
        "version": CACHE_VERSION,
        "cameras": [asdict(camera) for camera in config.cameras],
        "output": asdict(config.output),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:20]


class MapCache:
    """Disk-backed fixed-rig projection maps reusable across video frames."""

    def __init__(self, config: RigConfig, root: str | Path):
        self.config = config
        self.key = geometry_key(config)
        self.directory = Path(root) / self.key
        self._maps: list[tuple[np.memmap, np.memmap]] = []

    @property
    def metadata_path(self) -> Path:
        return self.directory / "metadata.json"

    def is_complete(self) -> bool:
        if not self.metadata_path.exists():
            return False
        try:
            metadata = json.loads(self.metadata_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return (
            metadata.get("version") == CACHE_VERSION
            and metadata.get("geometry_key") == self.key
            and metadata.get("width") == self.config.output.width
            and metadata.get("height") == self.config.output.height
            and metadata.get("camera_count") == len(self.config.cameras)
            and all(
                (self.directory / f"camera-{index}-x.npy").exists()
                and (self.directory / f"camera-{index}-y.npy").exists()
                for index in range(len(self.config.cameras))
            )
        )

    def build(self, progress=None) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        shape = (self.config.output.height, self.config.output.width)
        total = len(self.config.cameras) * sum(1 for _ in iter_tiles(self.config.output))
        completed = 0
        for index, camera in enumerate(self.config.cameras):
            map_x = np.lib.format.open_memmap(
                self.directory / f"camera-{index}-x.npy",
                mode="w+",
                dtype=np.float32,
                shape=shape,
            )
            map_y = np.lib.format.open_memmap(
                self.directory / f"camera-{index}-y.npy",
                mode="w+",
                dtype=np.float32,
                shape=shape,
            )
            for tile in iter_tiles(self.config.output):
                tile_x, tile_y, valid, _ = camera_map(camera, tile, self.config.output)
                tile_x[~valid] = -1.0
                tile_y[~valid] = -1.0
                rows = slice(tile.y, tile.y + tile.height)
                columns = slice(tile.x, tile.x + tile.width)
                map_x[rows, columns] = tile_x
                map_y[rows, columns] = tile_y
                completed += 1
                if progress:
                    progress(completed, total)
            map_x.flush()
            map_y.flush()
        metadata = {
            "version": CACHE_VERSION,
            "geometry_key": self.key,
            "width": self.config.output.width,
            "height": self.config.output.height,
            "camera_count": len(self.config.cameras),
        }
        self.metadata_path.write_text(json.dumps(metadata, indent=2), encoding="utf-8")

    def open(self, build: bool = True, progress=None) -> "MapCache":
        if not self.is_complete():
            if not build:
                raise OSError(f"projection map cache is incomplete: {self.directory}")
            self.build(progress=progress)
        self._maps = [
            (
                np.load(self.directory / f"camera-{index}-x.npy", mmap_mode="r"),
                np.load(self.directory / f"camera-{index}-y.npy", mmap_mode="r"),
            )
            for index in range(len(self.config.cameras))
        ]
        return self

    def tile_maps(
        self, tile: Tile
    ) -> list[tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]]:
        if not self._maps:
            raise RuntimeError("map cache must be opened before use")
        rows = slice(tile.y, tile.y + tile.height)
        columns = slice(tile.x, tile.x + tile.width)
        # With output pitch the world longitude varies vertically, so derive it
        # from the same view rotation used to build the maps.
        from .geometry import cylindrical_world_rays

        _, longitude = cylindrical_world_rays(tile, self.config.output)
        result = []
        for map_x, map_y in self._maps:
            tile_x = np.asarray(map_x[rows, columns])
            tile_y = np.asarray(map_y[rows, columns])
            valid = (tile_x >= 0.0) & (tile_y >= 0.0)
            result.append((tile_x, tile_y, valid, longitude))
        return result
