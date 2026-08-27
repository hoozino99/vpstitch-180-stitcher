from __future__ import annotations

from dataclasses import dataclass
import os
from statistics import median
from time import perf_counter

import cv2
import numpy as np


@dataclass(frozen=True, slots=True)
class RemapBackendDecision:
    backend: str
    reason: str
    cpu_seconds: float | None = None
    opencl_seconds: float | None = None
    max_normalized_error: float | None = None
    mean_normalized_error: float | None = None
    max_absolute_error: float | None = None


def opencl_available() -> bool:
    try:
        cv2.ocl.setUseOpenCL(cv2.ocl.haveOpenCL())
        return bool(cv2.ocl.haveOpenCL() and cv2.ocl.useOpenCL())
    except cv2.error:
        return False


def opencl_device_name() -> str | None:
    if not opencl_available():
        return None
    try:
        return str(cv2.ocl.Device_getDefault().name()).strip() or None
    except (AttributeError, cv2.error):
        return None


def _preference(value: str | None = None) -> str:
    requested = (value or os.environ.get("VPSTITCH_REMAP_BACKEND", "auto")).strip().lower()
    return requested if requested in {"auto", "cpu", "opencl"} else "auto"


def _normalization_scale(image: np.ndarray) -> float:
    if np.issubdtype(image.dtype, np.integer):
        return float(np.iinfo(image.dtype).max)
    finite = np.asarray(image)[np.isfinite(image)]
    if finite.size == 0:
        return 1.0
    return max(1.0, float(np.max(np.abs(finite))))


def _cpu_remap(
    image: np.ndarray,
    map_x: np.ndarray,
    map_y: np.ndarray,
    interpolation: int,
) -> np.ndarray:
    return cv2.remap(
        image,
        map_x,
        map_y,
        interpolation=interpolation,
        borderMode=cv2.BORDER_CONSTANT,
        borderValue=0,
    )


def choose_timed_backend(
    cpu_seconds: float,
    opencl_seconds: float,
    *,
    quality_ok: bool,
    min_speedup: float,
    preference: str = "auto",
) -> tuple[str, str]:
    if not quality_ok:
        return "cpu", "OpenCL output exceeded the quality tolerance"
    if _preference(preference) == "opencl":
        return "opencl", "OpenCL forced after quality validation"
    if opencl_seconds <= cpu_seconds * (1.0 - min_speedup):
        ratio = cpu_seconds / max(opencl_seconds, np.finfo(float).eps)
        return "opencl", f"OpenCL measured {ratio:.2f}x faster"
    return "cpu", "CPU measured faster after transfer costs"


def select_remap_backend(
    image: np.ndarray,
    map_x: np.ndarray,
    map_y: np.ndarray,
    *,
    interpolation: int,
    source_reuse_count: int = 1,
    maps_reused: bool = True,
    min_pixels: int = 240_000,
    max_probe_source_bytes: int = 256 * 1024 * 1024,
    min_speedup: float = 0.08,
    max_normalized_error: float = 1.0 / 1024.0,
    mean_normalized_error: float = 1.0 / 8192.0,
    max_absolute_error: float | None = None,
    iterations: int = 3,
    preference: str | None = None,
) -> RemapBackendDecision:
    """Measure the complete remap cost and select the useful backend.

    The OpenCL time includes GPU-to-CPU download. Source upload is amortized by
    the number of tiles using that source; map upload is included unless maps
    persist between frames. This avoids selecting a GPU merely because the
    kernel itself is fast while transfers make the complete operation slower.
    """
    mode = _preference(preference)
    if mode == "cpu":
        return RemapBackendDecision("cpu", "forced by VPSTITCH_REMAP_BACKEND")
    if map_x.size < min_pixels and mode == "auto":
        return RemapBackendDecision("cpu", "small remap is cheaper on CPU")
    if image.nbytes > max_probe_source_bytes and mode == "auto":
        return RemapBackendDecision(
            "cpu", "OpenCL probe would exceed the source-upload memory budget"
        )
    if not opencl_available():
        return RemapBackendDecision("cpu", "OpenCL is unavailable")

    iterations = max(1, int(iterations))
    source_reuse_count = max(1, int(source_reuse_count))
    try:
        cpu_samples: list[float] = []
        cpu_result = _cpu_remap(image, map_x, map_y, interpolation)
        for _ in range(iterations):
            started = perf_counter()
            cpu_result = _cpu_remap(image, map_x, map_y, interpolation)
            cpu_samples.append(perf_counter() - started)

        started = perf_counter()
        gpu_image = cv2.UMat(image)
        source_upload = perf_counter() - started
        gpu_map_x = cv2.UMat(map_x) if maps_reused else None
        gpu_map_y = cv2.UMat(map_y) if maps_reused else None

        # Warm the OpenCL kernel before timing it.
        warm_x = gpu_map_x if gpu_map_x is not None else cv2.UMat(map_x)
        warm_y = gpu_map_y if gpu_map_y is not None else cv2.UMat(map_y)
        cv2.remap(
            gpu_image,
            warm_x,
            warm_y,
            interpolation=interpolation,
            borderMode=cv2.BORDER_CONSTANT,
            borderValue=0,
        ).get()

        gpu_samples: list[float] = []
        gpu_result = cpu_result
        for _ in range(iterations):
            started = perf_counter()
            current_x = gpu_map_x if gpu_map_x is not None else cv2.UMat(map_x)
            current_y = gpu_map_y if gpu_map_y is not None else cv2.UMat(map_y)
            gpu_result = cv2.remap(
                gpu_image,
                current_x,
                current_y,
                interpolation=interpolation,
                borderMode=cv2.BORDER_CONSTANT,
                borderValue=0,
            ).get()
            gpu_samples.append(perf_counter() - started)
    except (cv2.error, MemoryError, ValueError) as error:
        return RemapBackendDecision("cpu", f"OpenCL benchmark failed: {error}")

    absolute_error = np.abs(
        cpu_result.astype(np.float64) - gpu_result.astype(np.float64)
    )
    scale = _normalization_scale(cpu_result)
    error = absolute_error / scale
    maximum = float(np.max(error)) if error.size else 0.0
    mean = float(np.mean(error)) if error.size else 0.0
    absolute_maximum = float(np.max(absolute_error)) if error.size else 0.0
    if max_absolute_error is None and np.issubdtype(cpu_result.dtype, np.floating):
        # A fixed scene-linear cap prevents one extreme HDR highlight from
        # making visible errors elsewhere look insignificant after normalization.
        max_absolute_error = 1.0e-4
    cpu_seconds = float(median(cpu_samples))
    gpu_seconds = float(median(gpu_samples)) + source_upload / source_reuse_count
    quality_ok = (
        maximum <= max_normalized_error
        and mean <= mean_normalized_error
        and (
            max_absolute_error is None
            or absolute_maximum <= max_absolute_error
        )
    )
    backend, reason = choose_timed_backend(
        cpu_seconds,
        gpu_seconds,
        quality_ok=quality_ok,
        min_speedup=min_speedup,
        preference=mode,
    )
    return RemapBackendDecision(
        backend,
        reason,
        cpu_seconds=cpu_seconds,
        opencl_seconds=gpu_seconds,
        max_normalized_error=maximum,
        mean_normalized_error=mean,
        max_absolute_error=absolute_maximum,
    )
