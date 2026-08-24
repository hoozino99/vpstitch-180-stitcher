from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication, QToolBar

from vpstitch.config import load_config
from vpstitch.gui import (
    MainWindow,
    TrimRangeBar,
    order_camera_plates,
    plate_number,
    preview_dimensions,
)


def test_plate_number_recognizes_one_based_clip_and_folder_names(tmp_path: Path) -> None:
    assert plate_number(tmp_path / "drive_P01_take.mov") == 1
    assert plate_number(tmp_path / "camera-5.mov") == 5
    assert plate_number(tmp_path / "rear_03.mov") == 3
    assert plate_number(tmp_path / "P04" / "A001.mov") == 4


def test_camera_plates_are_auto_ordered_from_p01() -> None:
    paths = ["shot_P03.mov", "shot_P01.mov", "shot_P02.mov"]
    ordered, numbers = order_camera_plates(paths)
    assert ordered == ["shot_P01.mov", "shot_P02.mov", "shot_P03.mov"]
    assert numbers == [1, 2, 3]


def test_camera_plate_import_rejects_incomplete_numbering() -> None:
    with pytest.raises(ValueError, match="P01, P02, P03"):
        order_camera_plates(["shot_P01.mov", "shot_P03.mov", "shot_P05.mov"])


def test_preview_dimensions_preserve_canvas_aspect() -> None:
    assert preview_dimensions(15360, 3968) == (2048, 529)
    assert preview_dimensions(20000, 6000) == (2048, 614)


def test_timeline_track_is_vertically_centered() -> None:
    app = QApplication.instance() or QApplication([])
    timeline = TrimRangeBar()
    timeline.resize(800, 32)
    assert timeline._track_bounds()[2] == 16.0


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


def test_gui_uses_compact_resolve_style_workspace() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    assert window.findChild(QToolBar) is None
    assert window.log_box.isHidden()
    assert not window.inspector_panel.isHidden()
    assert window.source_table.isColumnHidden(3)
    assert all(window.source_table.isColumnHidden(column) for column in (5, 6, 7, 8))
    assert "Auto Profile" in window.profile_label.text()
    assert window.rig_align_button.isEnabled() is False
    window.close()
    app.processEvents()


def test_gui_source_table_keeps_full_paths_while_showing_clip_names(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    paths = [str(tmp_path / f"camera-{index}.mov") for index in range(5)]
    window.source_table.set_paths(paths)
    assert window.source_table.paths() == paths
    assert window.source_table.item(0, 0).text() == "CAM 1"
    assert window.source_table.item(0, 1).text() == "camera-0.mov"
    window._update_source_status()
    assert "5 of 5" in window.source_status.text()
    window.close()
    app.processEvents()


def test_gui_imports_three_or_five_numbered_plates_in_camera_order(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))

    three = [tmp_path / f"front_P{number:02d}.mov" for number in (3, 1, 2)]
    for path in three:
        path.touch()
    window._set_video_sources([str(path) for path in three])
    assert window.source_table.rowCount() == 3
    assert [Path(path).name for path in window.source_table.paths()] == [
        "front_P01.mov",
        "front_P02.mov",
        "front_P03.mov",
    ]
    assert [window.source_table.item(row, 0).text() for row in range(3)] == [
        "CAM 1",
        "CAM 2",
        "CAM 3",
    ]
    assert len(window.config_data["cameras"]) == 3
    assert "3-CAMERA" in window.app_subtitle.text()
    assert "3 of 3" in window.source_status.text()
    assert window._validate_sources() == window.source_table.paths()
    working_config = tmp_path / "three-camera-rig.json"
    working_config.write_text(json.dumps(window._collect_config()), encoding="utf-8")
    assert len(load_config(working_config).cameras) == 3

    five = [tmp_path / f"rear_P{number:02d}.mov" for number in (5, 2, 4, 1, 3)]
    for path in five:
        path.touch()
    window._set_video_sources([str(path) for path in five])
    assert window.source_table.rowCount() == 5
    assert [Path(path).name for path in window.source_table.paths()] == [
        f"rear_P{number:02d}.mov" for number in range(1, 6)
    ]
    assert len(window.config_data["cameras"]) == 5
    assert "5-CAMERA" in window.app_subtitle.text()
    assert "5 of 5" in window.source_status.text()
    window.close()
    app.processEvents()


def test_gui_inspector_and_jobs_drawers_toggle() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()
    window.inspector_toggle.click()
    assert window.inspector_panel.isHidden()
    window.inspector_toggle.click()
    assert not window.inspector_panel.isHidden()
    window.jobs_toggle.click()
    assert window.log_box.isVisible()
    assert window.jobs_toggle.text() == "HIDE JOBS"
    window.jobs_toggle.click()
    assert window.log_box.isHidden()
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
