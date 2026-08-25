from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialog, QFileDialog, QLabel, QToolBar

from vpstitch.config import load_config
from vpstitch.gui import (
    GUI_MASTER_BIT_DEPTHS,
    InputSettingsDialog,
    MainWindow,
    TrimRangeBar,
    order_camera_plates,
    plate_number,
    preview_dimensions,
)
from vpstitch.renderqueue import RenderJob, RenderQueueStore, RenderStatus
from vpstitch.project import Bin, ProjectStore


def test_plate_number_recognizes_one_based_clip_and_folder_names(tmp_path: Path) -> None:
    assert plate_number(tmp_path / "drive_P01_take.mov") == 1
    assert plate_number(tmp_path / "camera-5.mov") == 5
    assert plate_number(tmp_path / "front_08.mov") == 8
    assert plate_number(tmp_path / "P06" / "A001.mov") == 6


def test_three_camera_plates_are_auto_ordered_from_p06() -> None:
    paths = ["shot_P08.mov", "shot_P06.mov", "shot_P07.mov"]
    ordered, numbers = order_camera_plates(paths)
    assert ordered == ["shot_P06.mov", "shot_P07.mov", "shot_P08.mov"]
    assert numbers == [6, 7, 8]


def test_five_camera_plates_are_auto_ordered_from_p01() -> None:
    paths = [
        "shot_P05.mov",
        "shot_P02.mov",
        "shot_P04.mov",
        "shot_P01.mov",
        "shot_P03.mov",
    ]
    ordered, numbers = order_camera_plates(paths)
    assert ordered == [f"shot_P{number:02d}.mov" for number in range(1, 6)]
    assert numbers == [1, 2, 3, 4, 5]


def test_camera_plate_import_rejects_incomplete_numbering() -> None:
    with pytest.raises(ValueError, match="P06, P07, P08"):
        order_camera_plates(["shot_P05.mov", "shot_P06.mov", "shot_P08.mov"])
    with pytest.raises(ValueError, match="P06, P07, P08"):
        order_camera_plates(["shot_P01.mov", "shot_P02.mov", "shot_P03.mov"])


def test_preview_dimensions_preserve_canvas_aspect() -> None:
    assert preview_dimensions(15360, 3968) == (3840, 992)
    assert preview_dimensions(20000, 6000) == (3840, 1152)
    assert preview_dimensions(20000, 32) == (3840, 6)


def test_timeline_track_is_vertically_centered() -> None:
    app = QApplication.instance() or QApplication([])
    timeline = TrimRangeBar()
    timeline.resize(800, 42)
    assert timeline._track_bounds()[2] == 21.0


def test_timeline_has_distinct_trim_range_and_playhead() -> None:
    app = QApplication.instance() or QApplication([])
    timeline = TrimRangeBar()
    timeline.set_frame_range(100, 10, 80, 40)
    assert timeline.values() == (10, 80)
    assert timeline.playhead() == 40
    timeline.set_playhead(72)
    assert timeline.playhead() == 72
    timeline.set_frame_range(100, 20, 60)
    assert timeline.playhead() == 59


def test_gui_loads_sample_rig() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/five_cam_180.sample.json"))
    assert window.source_table.rowCount() == 5
    assert window.canvas_width.value() == 15360
    assert window.canvas_height.value() == 3968
    assert window.output_codec.currentData() == "prores-hq"
    window.close()
    app.processEvents()


def test_gui_full_plate_fit_updates_manual_canvas_controls() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/five_cam_180.sample.json"))
    old_horizontal_fov = window.h_fov.value()

    window.fit_full_plates()

    assert window.h_fov.value() > old_horizontal_fov
    assert window.canvas_width.value() <= 20_000
    assert window.canvas_height.value() <= 6_000
    assert "FULL PLATES" in window.canvas_ratio.text()
    assert window._preview_ready is False
    window.close()
    app.processEvents()


def test_gui_builds_small_cached_playback_proxy_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/five_cam_180.sample.json"))
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(str(source))
    window.source_table.set_paths(sources)
    window._tc_alignment = {"fps": 24.0}
    window._timeline_maximum = 48
    window._timeline_updating = True
    window.timeline_in.setRange(0, 47)
    window.timeline_out.setRange(1, 48)
    window.timeline_in.setValue(4)
    window.timeline_out.setValue(28)
    window._timeline_updating = False
    window.timeline_bar.set_frame_range(48, 4, 28, 4)
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        window,
        "_run_cli",
        lambda task, arguments, success=None, failure=None: captured.append(
            (task, arguments)
        ),
    )

    window._build_playback(tmp_path / "proxy.mp4", "proxy-key", sources)

    assert captured[0][0] == "BUILD PLAYBACK PROXY"
    config_path = Path(captured[0][1][captured[0][1].index("--config") + 1])
    proxy_config = json.loads(config_path.read_text(encoding="utf-8"))
    assert proxy_config["output"]["width"] <= 1280
    assert proxy_config["output"]["height"] <= 720
    assert proxy_config["video"]["output_codec"] == "h264-proxy"
    assert proxy_config["video"]["frames"] == 24
    window.close()
    app.processEvents()


def test_gui_render_all_processes_timeline_snapshots_sequentially(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.render_queue = RenderQueueStore(tmp_path / "render-queue.json")
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(source)
    config = json.loads(Path("configs/five_cam_180.sample.json").read_text())
    for number in range(2):
        window.render_queue.add(
            RenderJob.create(
                name=f"Take {number + 1}",
                source_paths=sources,
                config_snapshot=config,
                output_path=tmp_path / f"take-{number + 1}.mov",
                in_frame=0,
                out_frame=12,
            )
        )
    started: list[str] = []

    def run_cli(task, arguments, success=None, failure=None):
        started.append(task)
        if success:
            success()

    monkeypatch.setattr(window, "_run_cli", run_cli)
    window._refresh_queue_table()
    window.render_all_queue_jobs()
    for _ in range(4):
        app.processEvents()

    assert started == ["QUEUE · Take 1", "QUEUE · Take 2"]
    assert all(job.status is RenderStatus.DONE for job in window.render_queue.jobs)
    assert window._queue_running is False
    window.close()
    app.processEvents()


def test_gui_master_outputs_are_limited_to_10_or_12_bit() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    codecs = [
        str(window.output_codec.itemData(index))
        for index in range(window.output_codec.count())
    ]
    assert set(codecs) == set(GUI_MASTER_BIT_DEPTHS)
    assert {GUI_MASTER_BIT_DEPTHS[codec] for codec in codecs} <= {10, 12}
    window.close()
    app.processEvents()


def test_gui_applies_detected_source_depth_without_manual_flag() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    inputs = [
        {
            "path": f"P{index:02d}.mov",
            "pixel_format": "yuv420p",
            "bit_depth": 8,
        }
        for index in range(1, 6)
    ]
    window._plate_numbers = list(range(1, 6))
    window.source_table.set_paths([str(item["path"]) for item in inputs])
    window._apply_source_probe_payload({"inputs": inputs, "issues": []})
    assert window._source_probes == inputs
    assert window.source_table.item(0, 4).text() == "8b AUTO"
    assert "SOURCE 8-bit → MASTER 10/12-bit" in window.source_status.text()
    assert "preview allowed" in window.preview_note.text()
    window.close()
    app.processEvents()


def test_gui_uses_compact_resolve_style_workspace() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    assert window.findChild(QToolBar) is None
    assert not window.inspector_panel.isHidden()
    assert window.right_tabs.currentIndex() == 0
    assert window.right_tabs.tabText(1) == "RENDER QUEUE"
    assert window.right_tabs.tabText(2) == "TASK LOG"
    assert window.media_tree.topLevelItemCount() == 1
    assert window.source_table.isColumnHidden(3)
    assert all(window.source_table.isColumnHidden(column) for column in (5, 6, 7, 8))
    assert "Auto Profile" in window.profile_label.text()
    assert window.rig_align_button.isEnabled() is False
    window.close()
    app.processEvents()


def test_imported_plate_set_becomes_timeline_in_unified_media_pool(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Production" / "project.json"
    store = ProjectStore.create(project_path, name="Production")
    store.add_bin(Bin.create("Location A"))
    window = MainWindow(project_path)
    clips = [tmp_path / f"take_07_P{number:02d}.mov" for number in (8, 6, 7)]
    for clip in clips:
        clip.touch()

    window._set_video_sources([str(clip) for clip in clips])

    assert len(window.project_store.timelines) == 1
    timeline = window.project_store.timelines[0]
    assert [Path(path).name for path in timeline.source_paths] == [
        "take_07_P06.mov",
        "take_07_P07.mov",
        "take_07_P08.mov",
    ]
    assert window._active_timeline_id == timeline.id
    root = window.media_tree.topLevelItem(0)
    labels = [root.child(index).text(0) for index in range(root.childCount())]
    assert any("P06–P08" in label for label in labels) or any(
        "P06–P08" in root.child(index).child(0).text(0)
        for index in range(root.childCount())
        if root.child(index).childCount()
    )
    window.close()
    app.processEvents()


def test_project_canvas_and_ocio_defaults_are_applied_to_new_workspace(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "ACES Project" / "project.json"
    ProjectStore.create(
        project_path,
        name="ACES Project",
        settings_snapshot={
            "output": {"width": 18_000, "height": 5_000},
            "color": {
                "mode": "ocio",
                "ocio_config": "ocio://show-config",
                "working_space": "ACEScg",
                "output_space": "ACES 1.0 SDR-video",
            },
        },
    )

    window = MainWindow(project_path)

    assert window.canvas_width.value() == 18_000
    assert window.canvas_height.value() == 5_000
    assert window.color_mode.currentData() == "ocio"
    assert window.ocio_config.text() == "ocio://show-config"
    assert window.working_space.text() == "ACEScg"
    assert window.output_space.text() == "ACES 1.0 SDR-video"
    window.close()
    app.processEvents()


def test_preview_transport_shortcuts_route_from_preview(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()
    commands: list[str] = []
    monkeypatch.setattr(window, "toggle_preview_fullscreen", lambda: commands.append("P"))
    monkeypatch.setattr(window, "toggle_playback", lambda: commands.append("SPACE"))
    monkeypatch.setattr(window, "play_reverse", lambda: commands.append("J"))
    monkeypatch.setattr(window, "stop_playback", lambda: commands.append("K"))
    monkeypatch.setattr(window, "step_playback", lambda value: commands.append(str(value)))

    window.preview.setFocus()
    for key in (Qt.Key.Key_P, Qt.Key.Key_Space, Qt.Key.Key_J, Qt.Key.Key_K, Qt.Key.Key_Left, Qt.Key.Key_Right):
        QTest.keyClick(window.preview.viewport(), key)

    assert commands == ["P", "SPACE", "J", "K", "-1", "1"]
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


def test_gui_reuses_import_dialog_to_avoid_macos_native_teardown(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    monkeypatch.setattr(
        QFileDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Rejected,
    )
    window.choose_videos()
    first_dialog = window._import_dialog
    window.choose_videos()
    assert first_dialog is not None
    assert window._import_dialog is first_dialog
    window.close()
    app.processEvents()


def test_input_settings_keep_bitrate_read_only_and_persist_interpretation(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    paths = [str(tmp_path / f"P{number:02d}.mov") for number in range(1, 6)]
    window.source_table.set_paths(paths)
    probe = {
        "codec": "h264",
        "width": 5952,
        "height": 3968,
        "fps": 24.0,
        "pixel_format": "yuv420p",
        "bit_depth": 8,
        "bit_rate": 84_300_000,
        "colorspace": "bt709",
        "color_range": "tv",
    }
    dialog = InputSettingsDialog(
        window,
        [paths[0]],
        [probe],
        [{"input_color_space": None, "input_video_range": None}],
    )
    labels = " ".join(label.text() for label in dialog.findChildren(QLabel))
    assert "84.3 Mb/s" in labels
    assert "intrinsic source properties" in labels
    dialog.color_space.setCurrentIndex(dialog.color_space.findData("bt709"))
    dialog.video_range.setCurrentIndex(dialog.video_range.findData("tv"))
    assert dialog.values() == {
        "input_color_space": "bt709",
        "input_video_range": "tv",
    }
    window._source_overrides[paths[0]] = dialog.values()
    config = window._collect_config()
    assert config["cameras"][0]["input_color_space"] == "bt709"
    assert config["cameras"][0]["input_video_range"] == "tv"
    assert window.source_table.selectionMode().name == "ExtendedSelection"
    dialog.close()
    window.close()
    app.processEvents()


def test_gui_imports_three_or_five_numbered_plates_in_camera_order(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    first_camera_item = window.source_table.item(0, 0)
    fifth_camera_item = window.source_table.item(4, 0)

    three = [tmp_path / f"front_P{number:02d}.mov" for number in (8, 6, 7)]
    for path in three:
        path.touch()
    window._set_video_sources([str(path) for path in three])
    assert window.source_table.camera_count() == 3
    assert window.source_table.rowCount() == 5
    assert all(window.source_table.isRowHidden(row) for row in (3, 4))
    assert window.source_table.item(0, 0) is first_camera_item
    assert [Path(path).name for path in window.source_table.paths()] == [
        "front_P06.mov",
        "front_P07.mov",
        "front_P08.mov",
    ]
    assert [window.source_table.item(row, 0).text() for row in range(3)] == [
        "CAM 6",
        "CAM 7",
        "CAM 8",
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
    assert window.source_table.camera_count() == 5
    assert window.source_table.rowCount() == 5
    assert all(not window.source_table.isRowHidden(row) for row in range(5))
    assert window.source_table.item(0, 0) is first_camera_item
    assert window.source_table.item(4, 0) is fifth_camera_item
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
    assert window.inspector_panel.isVisible()
    assert window.right_tabs.currentIndex() == 1
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
    assert window.ocio_config.text() == "vpstitch://aces-studio-v4.0.0"
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
    assert window.timeline_bar.playhead() == 10
    assert window.timeline_playhead.value() == 10
    assert window.frame_limit.value() == 70
    assert "70 frames" in window.timeline_duration.text()
    window.close()
    app.processEvents()


def test_gui_preview_config_scales_sources_and_lens_to_fitted_4k(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    source = Path("configs/drive_5cam_180.prores-hq.json")
    destination = tmp_path / "preview-config.json"
    preview_width, preview_height = preview_dimensions(20_000, 5_504)
    scale = window._write_preview_config(
        source, destination, preview_width, preview_height
    )
    preview = json.loads(destination.read_text(encoding="utf-8"))
    assert scale == pytest.approx(preview_width / 20_000)
    assert preview["output"]["width"] == preview_width
    assert preview["output"]["height"] == preview_height
    assert preview["cameras"][0]["width"] == round(5952 * scale)
    assert preview["cameras"][0]["height"] == round(3968 * scale)
    assert preview["cameras"][0]["lens"]["fx"] == pytest.approx(3720 * scale)
    assert load_config(destination).cameras[0].width == round(5952 * scale)
    window.close()
    app.processEvents()


def test_gui_preview_caps_large_camera_inputs_when_canvas_is_already_4k(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    raw = json.loads(
        Path("configs/drive_5cam_180.prores-hq.json").read_text(encoding="utf-8")
    )
    raw["output"]["width"] = 3840
    raw["output"]["height"] = 2160
    source = tmp_path / "4k-output-high-res-inputs.json"
    source.write_text(json.dumps(raw), encoding="utf-8")
    destination = tmp_path / "preview-config.json"
    scale = window._write_preview_config(source, destination, 3840, 2160)
    preview = json.loads(destination.read_text(encoding="utf-8"))
    assert scale == pytest.approx(2160 / 3968)
    assert preview["output"]["width"] == 3840
    assert preview["output"]["height"] == 2160
    assert preview["cameras"][0]["width"] == 3240
    assert preview["cameras"][0]["height"] == 2160
    window.close()
    app.processEvents()


def test_gui_queues_latest_playhead_during_preview(monkeypatch) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {"fps": 24.0}
    window._timeline_maximum = 100
    window.timeline_in.setRange(0, 99)
    window.timeline_out.setRange(1, 100)
    window._set_timeline_range(0, 100)
    window._preview_ready = True
    window._preview_in_progress = True
    window._set_playhead(42)
    window._scrub_preview()
    assert window._pending_scrub_frame == 42
    refreshed: list[int] = []
    monkeypatch.setattr(
        window,
        "create_preview",
        lambda: refreshed.append(window.timeline_playhead.value()),
    )
    window._finish_preview_frame(10)
    assert refreshed == [42]
    assert window._pending_scrub_frame is None
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
