from __future__ import annotations

import os
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication

from vpstitch.gui import MainWindow, preview_dimensions


def test_preview_dimensions_preserve_canvas_aspect() -> None:
    assert preview_dimensions(15360, 3968) == (2048, 529)
    assert preview_dimensions(20000, 6000) == (2048, 614)


def test_gui_loads_sample_rig() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/five_cam_180.sample.json"))
    assert window.source_table.rowCount() == 5
    assert window.canvas_width.value() == 15360
    assert window.canvas_height.value() == 3968
    assert window.output_codec.currentData() == "ffv1-16"
    window.close()
    app.processEvents()


def test_gui_applies_builtin_aces_preset() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.apply_aces_preset()
    assert window.color_mode.currentData() == "ocio"
    assert window.ocio_config.text().startswith("ocio://studio-config-")
    assert window.input_space.text() == "Camera Rec.709"
    assert window.working_space.text() == "ACEScg"
    window.close()
    app.processEvents()


def test_gui_common_timeline_controls_frame_range() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {
        "fps": 24.0,
        "common_frames": 100,
        "timeline_timecode": "01:00:00:00",
    }
    window._timeline_maximum = 100
    window.timeline_in.setRange(0, 99)
    window.timeline_out.setRange(1, 100)
    window._set_timeline_range(10, 80)
    assert window.timeline_bar.values() == (10, 80)
    assert window.frame_limit.value() == 70
    assert "70 frames" in window.timeline_duration.text()
    window.close()
    app.processEvents()


def test_gui_tc_alignment_keeps_manual_camera_offset() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.source_table.item(0, 8).setText("2")
    inputs = [
        {
            "path": f"cam{index}.mov",
            "timecode": "01:00:00:00",
            "frame_count": 100,
            "skip_frames": 0,
        }
        for index in range(5)
    ]
    window._apply_alignment_payload(
        {
            "fps": 24.0,
            "common_frames": 100,
            "timeline_timecode": "01:00:00:00",
            "inputs": inputs,
        }
    )
    assert window.source_table.item(0, 8).text() == "2"
    assert window._timeline_maximum == 98
    window.close()
    app.processEvents()
