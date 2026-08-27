from __future__ import annotations

import cv2
import numpy as np

from vpstitch.compute import choose_timed_backend, select_remap_backend


def _sample() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    image = np.arange(64 * 32 * 3, dtype=np.uint16).reshape(32, 64, 3)
    xs, ys = np.meshgrid(
        np.arange(64, dtype=np.float32),
        np.arange(32, dtype=np.float32),
    )
    return image, xs, ys


def test_cpu_preference_skips_opencl_benchmark() -> None:
    image, map_x, map_y = _sample()
    decision = select_remap_backend(
        image,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        preference="cpu",
    )
    assert decision.backend == "cpu"
    assert decision.cpu_seconds is None


def test_auto_keeps_small_remaps_on_cpu() -> None:
    image, map_x, map_y = _sample()
    decision = select_remap_backend(
        image,
        map_x,
        map_y,
        interpolation=cv2.INTER_LINEAR,
        min_pixels=10_000,
        preference="auto",
    )
    assert decision.backend == "cpu"
    assert "small" in decision.reason


def test_timed_policy_requires_a_real_speed_margin() -> None:
    backend, _reason = choose_timed_backend(
        1.0,
        0.95,
        quality_ok=True,
        min_speedup=0.08,
    )
    assert backend == "cpu"
    backend, _reason = choose_timed_backend(
        1.0,
        0.75,
        quality_ok=True,
        min_speedup=0.08,
    )
    assert backend == "opencl"


def test_timed_policy_rejects_fast_but_inaccurate_opencl() -> None:
    backend, reason = choose_timed_backend(
        1.0,
        0.1,
        quality_ok=False,
        min_speedup=0.08,
    )
    assert backend == "cpu"
    assert "quality" in reason
