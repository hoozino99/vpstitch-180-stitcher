from __future__ import annotations

import numpy as np


def weighted_blend(images: list[np.ndarray], weights: list[np.ndarray]) -> np.ndarray:
    if len(images) != len(weights) or not images:
        raise ValueError("images and weights must be non-empty and have equal length")
    height, width = weights[0].shape
    accumulator = np.zeros((height, width, 3), dtype=np.float32)
    weight_sum = np.zeros((height, width), dtype=np.float32)
    for image, weight in zip(images, weights, strict=True):
        if image.shape != accumulator.shape:
            raise ValueError("all images must be HxWx3 with the same dimensions")
        accumulator += image * weight[..., None]
        weight_sum += weight
    valid = weight_sum > 1e-8
    accumulator[valid] /= weight_sum[valid, None]
    accumulator[~valid] = 0.0
    return accumulator

