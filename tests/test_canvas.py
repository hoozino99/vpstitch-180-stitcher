from __future__ import annotations

from vpstitch.canvas import analyze_canvas
from vpstitch.config import Camera, Lens, Output, RigConfig


def test_canvas_report_finds_full_width_safe_crop() -> None:
    width, height = 320, 240
    cameras = tuple(
        Camera(
            f"cam{index}",
            width,
            height,
            yaw,
            0,
            0,
            Lens("pinhole", 230, 230, width / 2, height / 2),
        )
        for index, yaw in enumerate([-90, -45, 0, 45, 90])
    )
    config = RigConfig(
        cameras=cameras,
        output=Output(
            width=1000,
            height=300,
            horizontal_fov_deg=180,
            vertical_fov_deg=50,
            tile_width=250,
            tile_height=100,
        ),
    )
    report, mask = analyze_canvas(config)
    assert mask.shape == (300, 1000)
    assert report.coverage_fraction > 0.99
    assert report.valid_bbox == (0, 0, 1000, 300)
    assert report.safe_full_width_crop == (0, 0, 1000, 300)

