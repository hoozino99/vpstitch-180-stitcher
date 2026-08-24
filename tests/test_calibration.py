from __future__ import annotations

import cv2
import numpy as np

from vpstitch.calibration import calibrate_from_observations, checkerboard_object_points


def test_pinhole_calibration_recovers_intrinsics() -> None:
    width, height = 1280, 800
    matrix = np.array([[900.0, 0, 640.0], [0, 905.0, 400.0], [0, 0, 1.0]])
    distortion = np.zeros(5)
    board = checkerboard_object_points(9, 6, 0.035)
    objects: list[np.ndarray] = []
    images: list[np.ndarray] = []
    for index in range(12):
        rotation = np.array(
            [0.04 * np.sin(index), 0.06 * np.cos(index * 0.7), 0.02 * np.sin(index * 0.3)]
        )
        translation = np.array(
            [0.08 * np.sin(index * 0.5), 0.05 * np.cos(index * 0.4), 0.8 + index * 0.035]
        )
        projected, _ = cv2.projectPoints(board, rotation, translation, matrix, distortion)
        objects.append(board.copy())
        images.append(projected.astype(np.float32))
    lens, rms = calibrate_from_observations(objects, images, (width, height), "pinhole")
    assert rms < 0.01
    assert abs(lens.fx - matrix[0, 0]) / matrix[0, 0] < 0.01
    assert abs(lens.fy - matrix[1, 1]) / matrix[1, 1] < 0.01
    assert abs(lens.cx - matrix[0, 2]) < 3.0
    assert abs(lens.cy - matrix[1, 2]) < 3.0

