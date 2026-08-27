from __future__ import annotations

from concurrent.futures import Future, ThreadPoolExecutor
import os
from typing import Protocol

import numpy as np

from .config import RigConfig


DEFAULT_PREFETCH_MAX_BYTES = 256 * 1024 * 1024


class FrameDecoder(Protocol):
    def read(self, *, copy: bool = True) -> np.ndarray | None: ...


def decoded_bundle_bytes(config: RigConfig) -> int:
    return sum(camera.width * camera.height * 3 * 2 for camera in config.cameras)


def should_prefetch_decode(
    config: RigConfig,
    *,
    max_extra_bytes: int | None = None,
    preference: str | None = None,
) -> bool:
    requested = (
        preference or os.environ.get("VPSTITCH_DECODE_PREFETCH", "auto")
    ).strip().lower()
    if requested in {"0", "false", "off", "disabled"}:
        return False
    if requested in {"1", "true", "on", "enabled"}:
        return True
    limit = DEFAULT_PREFETCH_MAX_BYTES if max_extra_bytes is None else max_extra_bytes
    return decoded_bundle_bytes(config) <= max(0, limit)


class FrameBundleReader:
    """Read one synchronized bundle ahead only when its memory cost is safe."""

    def __init__(self, decoders: list[FrameDecoder], *, prefetch: bool) -> None:
        self._decoders = decoders
        self.prefetch = bool(prefetch)
        self._executor: ThreadPoolExecutor | None = None
        self._future: Future[list[np.ndarray | None]] | None = None
        if self.prefetch:
            self._executor = ThreadPoolExecutor(
                max_workers=1,
                thread_name_prefix="vpstitch-decode",
            )
            self._future = self._executor.submit(self._read_bundle, True)

    def _read_bundle(self, copy: bool) -> list[np.ndarray | None]:
        return [decoder.read(copy=copy) for decoder in self._decoders]

    def read(self, *, prefetch_next: bool = True) -> list[np.ndarray | None]:
        if not self.prefetch:
            return self._read_bundle(False)
        if self._future is None:
            return [None] * len(self._decoders)
        bundle = self._future.result()
        if prefetch_next and all(frame is not None for frame in bundle):
            assert self._executor is not None
            self._future = self._executor.submit(self._read_bundle, True)
        else:
            self._future = None
        return bundle

    def close(self) -> None:
        executor = self._executor
        self._executor = None
        self._future = None
        if executor is not None:
            executor.shutdown(wait=True, cancel_futures=True)

    def __enter__(self) -> FrameBundleReader:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()
