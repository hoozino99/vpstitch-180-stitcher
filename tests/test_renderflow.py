from __future__ import annotations

from dataclasses import replace

import numpy as np

from vpstitch.config import load_config
from vpstitch.renderflow import (
    FrameBundleReader,
    decoded_bundle_bytes,
    should_prefetch_decode,
)


class _Decoder:
    def __init__(self, values: list[int]) -> None:
        self.values = iter(values)
        self.copy_flags: list[bool] = []

    def read(self, *, copy: bool = True) -> np.ndarray | None:
        self.copy_flags.append(copy)
        try:
            value = next(self.values)
        except StopIteration:
            return None
        return np.full((2, 2, 3), value, dtype=np.uint16)


def test_prefetch_reader_copies_owned_bundles() -> None:
    decoders = [_Decoder([1, 2]), _Decoder([3, 4])]
    with FrameBundleReader(decoders, prefetch=True) as reader:
        first = reader.read()
        second = reader.read(prefetch_next=False)
    assert [int(frame[0, 0, 0]) for frame in first if frame is not None] == [1, 3]
    assert [int(frame[0, 0, 0]) for frame in second if frame is not None] == [2, 4]
    assert all(decoder.copy_flags == [True, True] for decoder in decoders)


def test_non_prefetch_reader_reuses_decoder_buffers() -> None:
    decoder = _Decoder([1])
    with FrameBundleReader([decoder], prefetch=False) as reader:
        reader.read(prefetch_next=False)
    assert decoder.copy_flags == [False]


def test_prefetch_is_disabled_when_extra_bundle_exceeds_budget() -> None:
    config = load_config("configs/five_cam_180.sample.json")
    assert decoded_bundle_bytes(config) > 1
    assert not should_prefetch_decode(config, max_extra_bytes=1)
    tiny = replace(
        config,
        cameras=tuple(
            replace(camera, width=32, height=16) for camera in config.cameras
        ),
    )
    assert should_prefetch_decode(tiny, max_extra_bytes=1024 * 1024)
