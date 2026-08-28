from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QPixmap, QWheelEvent
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QFileDialog,
    QInputDialog,
    QLabel,
    QMessageBox,
    QToolBar,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QWidget,
)

from vpstitch.config import load_config, repair_legacy_p3_pq_target
from vpstitch.gui import (
    GUI_MASTER_BIT_DEPTHS,
    ChevronComboBox,
    FullscreenPreviewLabel,
    InputSettingsDialog,
    MainWindow,
    NewTimelineDialog,
    PlateAssignmentDialog,
    ProjectManagerDialog,
    ScrubbableDoubleSpinBox,
    TrimRangeBar,
    order_camera_plates,
    plate_number,
    preview_dimensions,
    resolved_render_output,
    suggest_camera_assignment,
    split_render_output,
)
from vpstitch.renderqueue import RenderJob, RenderQueueStore, RenderStatus
from vpstitch.project import (
    Bin,
    MediaCacheStatus,
    MediaRecord,
    PlaybackCacheStatus,
    ProjectStore,
    StitchStatus,
    TimelineRecord,
)
from vpstitch.sourcecache import SourceProxyCommand, plan_source_proxy


def _cache_source_fps(window: MainWindow, sources: list[str], fps: float) -> None:
    window._source_probes = [
        {"path": str(source), "fps": fps, "bit_depth": 10, "pixel_format": "yuv420p10le"}
        for source in sources
    ]


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


def test_arbitrary_clip_names_get_deterministic_manual_camera_slots() -> None:
    paths = ["take_z.mov", "take_a.mov", "take_m.mov"]

    ordered, manual = suggest_camera_assignment(paths, 3)

    assert manual is True
    assert ordered == ["take_a.mov", "take_m.mov", "take_z.mov"]


def test_manual_camera_assignment_dialog_maps_each_clip_once() -> None:
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    paths = ["take_z.mov", "take_a.mov", "take_m.mov"]
    dialog = PlateAssignmentDialog(parent, paths=paths, camera_count=3)

    dialog.slot_combos[0].setCurrentIndex(dialog.slot_combos[0].findData("take_m.mov"))
    dialog.slot_combos[1].setCurrentIndex(dialog.slot_combos[1].findData("take_z.mov"))
    dialog.slot_combos[2].setCurrentIndex(dialog.slot_combos[2].findData("take_a.mov"))

    assert dialog.values() == ["take_m.mov", "take_z.mov", "take_a.mov"]
    assert dialog.assign_button is not None and dialog.assign_button.isEnabled()
    dialog.close()
    parent.close()
    app.processEvents()


def test_camera_plate_import_rejects_incomplete_numbering() -> None:
    with pytest.raises(ValueError, match="P06, P07, P08"):
        order_camera_plates(["shot_P05.mov", "shot_P06.mov", "shot_P08.mov"])
    with pytest.raises(ValueError, match="P06, P07, P08"):
        order_camera_plates(["shot_P01.mov", "shot_P02.mov", "shot_P03.mov"])


def test_new_timeline_dialog_makes_camera_layout_explicit() -> None:
    app = QApplication.instance() or QApplication([])
    parent = MainWindow()
    dialog = NewTimelineDialog(
        parent,
        default_name="Front Take",
        suggested_count=3,
        selected_plate_count=3,
    )

    assert dialog.values() == ("Front Take", 3, True)
    assert "P06–P08" in dialog.layout_buttons[3].text()
    assert "P01–P05" in dialog.layout_buttons[5].text()
    dialog.layout_buttons[5].click()
    assert dialog.values() == ("Front Take", 5, False)
    assert not dialog.add_selected.isEnabled()
    dialog.close()
    parent.close()
    app.processEvents()


def test_preview_dimensions_preserve_canvas_aspect() -> None:
    assert preview_dimensions(15360, 3968) == (3840, 992)
    assert preview_dimensions(20000, 6000) == (3840, 1152)
    assert preview_dimensions(20000, 32) == (3840, 6)


def test_render_output_name_is_canonical_and_codec_specific(tmp_path: Path) -> None:
    assert resolved_render_output(tmp_path, "Take 01.mov.mov", "prores-hq") == (
        tmp_path / "Take 01.mov"
    )
    assert resolved_render_output(tmp_path, "Take 01.MP4", "h264-mp4-10") == (
        tmp_path / "Take 01.mp4"
    )
    assert resolved_render_output(tmp_path, "Take 01.dpx", "dpx12-sequence") == (
        tmp_path / "Take 01"
    )
    assert split_render_output(tmp_path / "Take 01.mov", "prores-hq") == (
        tmp_path,
        "Take 01",
    )
    with pytest.raises(ValueError, match="separators"):
        resolved_render_output(tmp_path, "nested/Take 01", "prores-hq")


def test_project_manager_new_project_button_opens_dialog(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    manager = ProjectManagerDialog()
    opened: list[str] = []

    def reject_new_project(dialog: QDialog) -> QDialog.DialogCode:
        opened.append(dialog.windowTitle())
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", reject_new_project)
    manager.new_project_button.click()

    assert opened == ["New VP Stitch Project"]
    manager.close()
    app.processEvents()


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
    assert proxy_config["output"]["width"] <= 960
    assert proxy_config["output"]["height"] <= 540
    assert proxy_config["video"]["output_codec"] == "h264-proxy"
    assert proxy_config["video"]["frames"] == 24
    window.close()
    app.processEvents()


def test_gui_playback_cache_uses_proxy_native_geometry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Proxy Geometry" / "project.json"
    store = ProjectStore.create(project_path, name="Proxy Geometry")
    proxies: list[str] = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        proxy = tmp_path / f"P{number:02d}-proxy.mp4"
        source.touch()
        proxy.touch()
        record = store.add_media(MediaRecord.create(source, media_id=f"p{number}"))
        store.update_media(
            record.id,
            source_cache_path=proxy,
            source_cache_status=MediaCacheStatus.READY,
        )
        proxies.append(str(proxy))

    window = MainWindow(project_path)
    config_path = Path("configs/five_cam_180.sample.json")
    original = json.loads(config_path.read_text(encoding="utf-8"))
    window.load_config(config_path)
    window._tc_alignment = {"fps": 24.0}
    window._timeline_maximum = 48
    window.timeline_bar.set_frame_range(48, 0, 24, 0)
    monkeypatch.setattr(
        "vpstitch.gui.probe_video",
        lambda _path: SimpleNamespace(width=810, height=540),
    )
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        window,
        "_run_cli",
        lambda task, arguments, success=None, failure=None: captured.append(
            (task, arguments)
        ),
    )

    window._build_playback(tmp_path / "playback.mp4", "proxy-native", proxies)

    arguments = captured[0][1]
    assert arguments[arguments.index("--decode-scale") + 1] == "1.000000000"
    assert arguments[-5:] == proxies
    playback_config = json.loads(
        Path(arguments[arguments.index("--config") + 1]).read_text(encoding="utf-8")
    )
    camera = playback_config["cameras"][0]
    original_camera = original["cameras"][0]
    assert (camera["width"], camera["height"]) == (810, 540)
    assert camera["lens"]["fx"] == pytest.approx(
        original_camera["lens"]["fx"] * 810 / original_camera["width"]
    )
    assert camera["lens"]["fy"] == pytest.approx(
        original_camera["lens"]["fy"] * 540 / original_camera["height"]
    )
    window.close()
    app.processEvents()


def test_viewer_monitor_is_preview_only_and_keeps_hdr_delivery_locked(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    window.color_mode.setCurrentIndex(window.color_mode.findData("ocio"))
    window.ocio_config.setText("vpstitch://aces-studio-v4.0.0")
    assert window._reload_ocio_spaces(quiet=True)
    window.output_mode.setCurrentIndex(window.output_mode.findData("display_view"))
    window.output_display.setCurrentIndex(
        window.output_display.findData("Rec.2100-PQ - Display")
    )
    window._delivery_display_changed()
    window.output_view.setCurrentIndex(
        window.output_view.findText("ACES 2.0 - HDR 1000 nits (Rec.2020)")
    )
    window.viewer_monitor.setCurrentIndex(
        window.viewer_monitor.findData("sdr-rec709")
    )
    delivery = window._collect_config()
    source = tmp_path / "delivery.json"
    preview = tmp_path / "viewer.json"
    source.write_text(json.dumps(delivery), encoding="utf-8")

    window._write_preview_config(
        source, preview, 960, 248, viewer_transform=True
    )
    viewer = json.loads(preview.read_text(encoding="utf-8"))

    assert delivery["color"]["display"] == "Rec.2100-PQ - Display"
    assert delivery["video"]["color_trc"] == "smpte2084"
    assert viewer["color"]["display"] == "sRGB - Display"
    assert viewer["color"]["view"] == "ACES 2.0 - SDR 100 nits (Rec.709)"
    assert viewer["video"]["color_trc"] == "bt709"
    assert "viewer_monitor" not in delivery
    assert [
        window.viewer_monitor.itemText(index)
        for index in range(window.viewer_monitor.count())
    ] == ["Standard Rec.709", "Match delivery target"]
    window.close()
    app.processEvents()


def test_delivery_controls_distinguish_pq_from_apple_edr_and_expose_vlog() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    window.color_mode.setCurrentIndex(window.color_mode.findData("ocio"))
    window.ocio_config.setText("vpstitch://aces-studio-v4.0.0")
    assert window._reload_ocio_spaces(quiet=True)

    assert window.output_display.itemText(
        window.output_display.findData("ST2084-P3-D65 - Display")
    ) == "P3-D65 PQ"
    assert window.output_display.itemText(
        window.output_display.findData("Display P3 HDR - Display")
    ) == "Apple Display P3 HDR · EDR, not PQ"
    assert window.output_space.findText("V-Log V-Gamut") >= 0
    assert window.output_space.itemText(0) == "V-Log V-Gamut"

    window.output_mode.setCurrentIndex(window.output_mode.findData("display_view"))
    window.output_display.setCurrentIndex(
        window.output_display.findData("ST2084-P3-D65 - Display")
    )
    window._delivery_display_changed()
    window.output_view.setCurrentIndex(
        window.output_view.findText("ACES 2.0 - HDR 1000 nits (P3 D65)")
    )
    pq_delivery = window._collect_config()

    assert pq_delivery["color"]["display"] == "ST2084-P3-D65 - Display"
    assert pq_delivery["video"]["color_primaries"] == "smpte432"
    assert pq_delivery["video"]["color_trc"] == "smpte2084"

    window.output_mode.setCurrentIndex(window.output_mode.findData("colorspace"))
    window.output_space.setCurrentIndex(window.output_space.findText("V-Log V-Gamut"))
    delivery = window._collect_config()

    assert delivery["color"]["output_mode"] == "colorspace"
    assert delivery["color"]["output_space"] == "V-Log V-Gamut"
    assert "color_trc" not in delivery["video"]
    window.close()
    app.processEvents()


def test_legacy_apple_edr_config_tagged_as_pq_is_repaired() -> None:
    color: dict[str, object] = {
        "output_mode": "display_view",
        "display": "Display P3 HDR - Display",
        "view": "ACES 2.0 - HDR 1000 nits (P3 D65)",
    }
    video: dict[str, object] = {"color_trc": "smpte2084"}

    assert repair_legacy_p3_pq_target(color, video)
    assert color["display"] == "ST2084-P3-D65 - Display"


def test_sdr_display_transform_is_tagged_rec709_instead_of_pq() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    window.color_mode.setCurrentIndex(window.color_mode.findData("ocio"))
    window.ocio_config.setText("vpstitch://aces-studio-v4.0.0")
    assert window._reload_ocio_spaces(quiet=True)
    window.output_mode.setCurrentIndex(window.output_mode.findData("display_view"))
    window.output_display.setCurrentIndex(
        window.output_display.findData("sRGB - Display")
    )
    window._delivery_display_changed()
    window.output_view.setCurrentIndex(
        window.output_view.findText("ACES 2.0 - SDR 100 nits (Rec.709)")
    )

    delivery = window._collect_config()

    assert delivery["video"]["color_primaries"] == "bt709"
    assert delivery["video"]["color_trc"] == "bt709"
    assert delivery["video"]["colorspace"] == "bt709"
    window.close()
    app.processEvents()


def test_playback_signature_changes_for_viewer_without_changing_delivery(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(str(source))
    window.source_table.set_paths(sources)
    delivery = window._collect_config()
    window.viewer_monitor.setCurrentIndex(window.viewer_monitor.findData("sdr-rec709"))
    rec709_key, _ = window._playback_signature()
    window.viewer_monitor.setCurrentIndex(window.viewer_monitor.findData("delivery"))
    delivery_key, _ = window._playback_signature()

    assert rec709_key != delivery_key
    assert window._collect_config() == delivery
    window.close()
    app.processEvents()


def test_cached_playback_sources_require_a_complete_ready_set(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Cached Sources" / "project.json"
    store = ProjectStore.create(project_path, name="Cached Sources")
    sources: list[str] = []
    caches: list[str] = []
    for number in range(1, 4):
        source = tmp_path / f"P{number:02d}.mov"
        cache = tmp_path / "cache" / f"P{number:02d}.mp4"
        source.touch()
        cache.parent.mkdir(exist_ok=True)
        cache.touch()
        record = store.add_media(MediaRecord.create(source, media_id=f"p{number}"))
        store.update_media(
            record.id,
            source_cache_path=cache,
            source_cache_status=MediaCacheStatus.READY,
        )
        sources.append(str(source))
        caches.append(str(cache))
    window = MainWindow(project_path)

    assert window._cached_playback_sources(sources) == caches
    window.project_store.update_media(
        "p2", source_cache_status=MediaCacheStatus.PENDING
    )
    assert window._cached_playback_sources(sources) == sources
    window.close()
    app.processEvents()


def test_source_proxy_cancel_detaches_deleted_media_safely(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Cancel Cache" / "project.json"
    store = ProjectStore.create(project_path, name="Cancel Cache")
    source = tmp_path / "P01.mov"
    source.touch()
    record = store.add_media(MediaRecord.create(source, media_id="p01"))
    window = MainWindow(project_path)
    plan = plan_source_proxy(source, window._cache_dir)
    plan.output.parent.mkdir(parents=True, exist_ok=True)
    plan.temporary.touch()
    window._source_proxy_queue = [record.id, "another"]
    window._source_proxy_current = (record.id, plan)

    window._cancel_source_proxy_items({record.id})

    assert window._source_proxy_current is None
    assert window._source_proxy_queue == ["another"]
    assert not plan.temporary.exists()
    window.close()
    app.processEvents()


def test_source_proxy_failure_retries_remaining_encoder(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Proxy Retry" / "project.json"
    store = ProjectStore.create(project_path, name="Proxy Retry")
    source = tmp_path / "P01.mov"
    source.touch()
    record = store.add_media(MediaRecord.create(source, media_id="p01"))
    window = MainWindow(project_path)
    plan = plan_source_proxy(source, window._cache_dir)
    fallback = SourceProxyCommand("libx264", "ffmpeg", ("-version",))
    window._source_proxy_current = (record.id, plan)
    window._source_proxy_backend = "h264_videotoolbox"
    window._source_proxy_attempts = [fallback]
    window._source_proxy_output.extend(b"hardware initialization failed")
    attempts: list[str] = []

    def start_fallback() -> bool:
        attempts.append(window._source_proxy_attempts.pop(0).encoder)
        return True

    monkeypatch.setattr(window, "_start_source_proxy_attempt", start_fallback)
    window._source_proxy_finished(1, None)

    assert attempts == ["libx264"]
    assert window._source_proxy_current == (record.id, plan)
    window._source_proxy_current = None
    window.close()
    app.processEvents()


def test_quick_preview_uses_one_frame_and_caps_canvas_at_2k(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Quick Preview" / "project.json"
    ProjectStore.create(project_path, name="Quick Preview")
    window = MainWindow(project_path)
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(str(source))
    window.source_table.set_paths(sources)
    captured: list[tuple[str, list[str]]] = []
    monkeypatch.setattr(
        window,
        "_run_cli",
        lambda task, arguments, success=None, failure=None: captured.append(
            (task, arguments)
        ),
    )

    window.create_preview()

    assert captured[0][0] == "EXTRACT REFERENCES"
    arguments = captured[0][1]
    assert arguments[arguments.index("--start-frame") + 1] == "0"
    reference_dir = Path(arguments[arguments.index("--output-dir") + 1])
    preview_config = json.loads(
        (reference_dir / "preview-config.json").read_text(encoding="utf-8")
    )
    assert preview_config["output"]["width"] <= 2048
    assert preview_config["output"]["height"] <= 1152
    assert "ONE SYNCHRONIZED FRAME" in window.preview._empty.text()
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
    started: list[tuple[str, str]] = []

    def run_cli(task, arguments, success=None, failure=None):
        output = arguments[arguments.index("--output") + 1]
        started.append((task, output))
        Path(output).write_bytes(b"rendered")
        if success:
            success()

    monkeypatch.setattr(window, "_run_cli", run_cli)
    window._refresh_queue_table()
    window.render_all_queue_jobs()
    for _ in range(4):
        app.processEvents()

    assert [task for task, _output in started] == [
        "QUEUE · Take 1",
        "QUEUE · Take 2",
    ]
    assert all(".vpstitch-part-" in Path(output).name for _task, output in started)
    assert (tmp_path / "take-1.mov").read_bytes() == b"rendered"
    assert (tmp_path / "take-2.mov").read_bytes() == b"rendered"
    assert all(job.status is RenderStatus.DONE for job in window.render_queue.jobs)
    assert window._queue_running is False
    window.close()
    app.processEvents()


def test_render_staging_commits_only_complete_output(tmp_path: Path) -> None:
    output = tmp_path / "master.mov"
    staging = MainWindow._render_staging_path(output, "prores-hq", "test")
    staging.write_bytes(b"complete master")

    MainWindow._commit_render_staging(staging, output)

    assert output.read_bytes() == b"complete master"
    assert not staging.exists()


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


def test_add_to_queue_dialog_selects_and_locks_render_format(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Queue Format" / "project.json"
    ProjectStore.create(project_path, name="Queue Format")
    window = MainWindow(project_path)
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(str(source))
    window.source_table.set_paths(sources)
    _cache_source_fps(window, sources, 24_000 / 1_001)
    window.output_directory.setText(str(tmp_path / "renders"))
    window.output_name.setText("Take_01")
    window._auto_workflows_enabled = True

    def choose_h264(dialog: QDialog) -> QDialog.DialogCode:
        combo = dialog.findChild(ChevronComboBox, "renderCodec")
        assert combo is not None
        combo.setCurrentIndex(combo.findData("h264-mp4-10"))
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QDialog, "exec", choose_h264)
    window.add_current_to_queue()

    assert len(window.render_queue.jobs) == 1
    job = window.render_queue.jobs[0]
    assert job.config_snapshot["video"]["output_codec"] == "h264-mp4-10"
    assert job.config_snapshot["video"]["fps"] == pytest.approx(24_000 / 1_001)
    assert job.config_snapshot["_vpstitch"]["fps_mode"] == "match_source"
    assert job.output_path.suffix == ".mp4"
    assert window.queue_table.item(0, 1).text() == "23.976"
    assert "H.264 MP4" in window.queue_table.item(0, 2).text()
    window.render_queue.remove(job.id)
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


def test_gui_blocks_mixed_plate_frame_rates_before_alignment() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    inputs = [
        {
            "path": f"P{index:02d}.mov",
            "pixel_format": "yuv420p10le",
            "bit_depth": 10,
            "fps": 24.0 if index < 5 else 24_000 / 1_001,
        }
        for index in range(1, 6)
    ]
    window.source_table.set_paths([str(item["path"]) for item in inputs])

    window._apply_source_probe_payload({"inputs": inputs, "issues": []})

    assert window._source_fps_error is not None
    assert "Plate frame rates do not match" in window._source_fps_error
    assert "FPS MISMATCH" in window.preview_note.text()
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
    assert window.timeline_tree.topLevelItemCount() == 0
    assert window.new_timeline_button.text() == "NEW TIMELINE"
    assert window.render_selected_queue_button.text() == "RENDER SELECTED"
    assert window.render_all_queue_button.text() == "RENDER ALL"
    assert window.queue_table.columnCount() == 5
    assert [
        window.queue_table.horizontalHeaderItem(column).text()
        for column in range(5)
    ] == ["TIMELINE", "FPS", "FORMAT", "FILE", "STATUS"]
    assert window.media_tree.selectionMode().name == "ExtendedSelection"
    assert window.import_button.text() == "IMPORT"
    assert window.assign_media_button.text() == "ASSIGN SELECTED"
    assert window.rig_align_button.text() == "STITCH"
    assert window.add_queue_button.objectName() == "primaryButton"
    assert window.render_button.objectName() == "secondaryButton"
    assert window.source_table.isColumnHidden(3)
    assert all(window.source_table.isColumnHidden(column) for column in (5, 6, 7, 8))
    assert "Auto Profile" in window.profile_label.text()
    assert window.rig_align_button.isEnabled() is False
    window.close()
    app.processEvents()


def test_library_sections_are_boxed_and_resizable() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.resize(1500, 900)
    window.show()
    app.processEvents()

    assert window.library_panel.objectName() == "libraryPanel"
    assert window.library_panel.minimumWidth() == 270
    assert window.library_panel.maximumWidth() == 560
    assert window.library_splitter.orientation() == Qt.Orientation.Vertical
    assert window.library_splitter.count() == 3
    assert window.library_splitter.childrenCollapsible() is False
    assert window.library_splitter.handleWidth() == 7
    assert [
        window.library_splitter.widget(index).objectName()
        for index in range(window.library_splitter.count())
    ] == ["librarySection", "librarySection", "librarySection"]
    assert window.source_table.minimumHeight() == 118
    assert window.source_table.maximumHeight() > 10_000

    initial_library_sizes = window.library_splitter.sizes()
    window.library_splitter.setSizes([180, 245, 340])
    window.workspace_splitter.setSizes([470, 700, 320])
    app.processEvents()

    assert window.library_splitter.sizes() != initial_library_sizes
    assert all(size > 0 for size in window.library_splitter.sizes())
    assert window.workspace_splitter.sizes()[0] >= 430
    window.close()
    app.processEvents()


def test_media_pool_hierarchy_drag_moves_persist_and_preserve_selection(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    errors: list[tuple[str, str]] = []
    window._error = (  # type: ignore[method-assign]
        lambda title, message: errors.append((title, message))
    )
    store = ProjectStore.create(tmp_path / "project.json", name="Folder UX")
    shoot = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    day = store.add_bin(Bin.create("Day 01", parent_id=shoot.id, bin_id="day-01"))
    selects = store.add_bin(Bin.create("Selects", bin_id="selects", order=1))
    clips = [
        store.add_media(
            MediaRecord.create(
                tmp_path / f"P{number:02d}.mov",
                bin_id=shoot.id,
                media_id=f"p{number:02d}",
                order=index,
            )
        )
        for index, number in enumerate((1, 2))
    ]
    window.project_store = store
    window._active_bin_id = shoot.id
    window._refresh_media_tree()

    iterator = QTreeWidgetItemIterator(window.media_tree)
    items: dict[tuple[str, str], QTreeWidgetItem] = {}
    while iterator.value() is not None:
        item = iterator.value()
        key = (
            str(item.data(0, Qt.ItemDataRole.UserRole) or ""),
            str(item.data(0, Qt.ItemDataRole.UserRole + 1) or ""),
        )
        items[key] = item
        iterator += 1

    assert items[("bin", day.id)].parent() is items[("bin", shoot.id)]
    assert window.media_tree.dragEnabled() is True
    assert window.media_tree.acceptDrops() is True
    assert items[("bin", shoot.id)].flags() & Qt.ItemFlag.ItemIsDropEnabled
    assert items[("media", clips[0].id)].flags() & Qt.ItemFlag.ItemIsDragEnabled

    window.media_tree.clearSelection()
    window.media_tree.setCurrentItem(items[("media", clips[0].id)])
    items[("media", clips[0].id)].setSelected(True)
    items[("media", clips[1].id)].setSelected(True)
    app.processEvents()
    assert {item["id"] for item in window.media_tree.selected_payload()} == {
        "p01",
        "p02",
    }
    drag_pixmap = window.media_tree._drag_pixmap(window.media_tree.selected_payload())
    assert drag_pixmap.size().width() == 286
    assert drag_pixmap.size().height() == 42
    window._move_media_tree_items(
        window.media_tree.selected_payload(),
        {"bin_id": day.id, "kind": None, "index": None, "label": "Day 01"},
    )

    assert [item.id for item in store.list_media(day.id)] == ["p01", "p02"]
    assert {item["id"] for item in window.media_tree.selected_payload()} == {
        "p01",
        "p02",
    }

    window._move_media_tree_items(
        [{"kind": "bin", "id": selects.id}],
        {"bin_id": day.id, "kind": None, "index": None, "label": "Day 01"},
    )
    assert errors == []
    assert store.list_bins(day.id)[0].id == selects.id
    window._move_media_tree_items(
        [{"kind": "bin", "id": shoot.id}],
        {"bin_id": day.id, "kind": None, "index": None, "label": "Day 01"},
    )
    assert errors[-1][1] == "A folder cannot be moved into itself or one of its subfolders."
    assert next(item for item in store.bins if item.id == shoot.id).parent_id is None
    loaded = ProjectStore.load(store.path)
    assert [item.id for item in loaded.list_media(day.id)] == ["p01", "p02"]
    assert loaded.list_bins(day.id)[0].id == selects.id
    window.close()
    app.processEvents()


def test_media_import_uses_selected_nested_folder_or_explicit_project_root(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._auto_workflows_enabled = False
    store = ProjectStore.create(tmp_path / "project.json", name="Import Destination")
    shoot = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    day = store.add_bin(Bin.create("Day 01", parent_id=shoot.id, bin_id="day-01"))
    window.project_store = store
    window._refresh_media_tree()

    def select_item(kind: str, item_id: str = "") -> None:
        iterator = QTreeWidgetItemIterator(window.media_tree)
        while iterator.value() is not None:
            item = iterator.value()
            if (
                item.data(0, Qt.ItemDataRole.UserRole) == kind
                and str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "") == item_id
            ):
                window.media_tree.setCurrentItem(item)
                return
            iterator += 1
        raise AssertionError(f"missing tree item: {kind} {item_id}")

    select_item("bin", day.id)
    nested = window.import_media_paths([str(tmp_path / "nested_P01.mov")])
    select_item("project")
    root = window.import_media_paths([str(tmp_path / "root_P02.mov")])

    assert nested[0].bin_id == day.id
    assert root[0].bin_id is None
    assert ProjectStore.load(store.path).list_media(day.id)[0].id == nested[0].id
    assert ProjectStore.load(store.path).list_media(None)[0].id == root[0].id
    window.close()
    app.processEvents()


def test_media_pool_new_folder_uses_selected_nested_location(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    store = ProjectStore.create(tmp_path / "project.json", name="Folder Create")
    shoot = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    day = store.add_bin(Bin.create("Day 01", parent_id=shoot.id, bin_id="day-01"))
    window.project_store = store
    window._refresh_media_tree()
    iterator = QTreeWidgetItemIterator(window.media_tree)
    while iterator.value() is not None:
        item = iterator.value()
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == day.id:
            window.media_tree.setCurrentItem(item)
            break
        iterator += 1
    monkeypatch.setattr(
        QInputDialog,
        "getText",
        lambda *args, **kwargs: ("Selects", True),
    )

    window.create_media_bin()

    created = next(item for item in store.bins if item.name == "Selects")
    assert created.parent_id == day.id
    assert "Shoot / Day 01" in window.statusBar().currentMessage()
    window.close()
    app.processEvents()


def test_combo_boxes_paint_a_visible_dropdown_chevron() -> None:
    app = QApplication.instance() or QApplication([])
    combo = ChevronComboBox()
    combo.addItem("Rec.2020 PQ 1000 nits")
    combo.resize(220, 34)
    combo.show()
    app.processEvents()

    pixmap = QPixmap(combo.size())
    combo.render(pixmap)
    image = pixmap.toImage()
    arrow_pixels = [
        image.pixelColor(x, y)
        for x in range(combo.width() - 17, combo.width() - 5)
        for y in range(combo.height() // 2 - 6, combo.height() // 2 + 7)
    ]
    assert any(
        color.red() > 150 and color.green() > 150 and color.blue() > 150
        for color in arrow_pixels
    )
    combo.close()
    app.processEvents()


def test_plate_sets_tree_scrolls_with_mouse_wheel() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    tree = window.timeline_tree
    tree.clear()
    for index in range(24):
        tree.addTopLevelItem(QTreeWidgetItem([f"Timeline {index + 1:02d}"]))
    window.resize(1500, 780)
    window.show()
    window.library_splitter.setSizes([190, 130, 300])
    app.processEvents()

    scrollbar = tree.verticalScrollBar()
    assert scrollbar.maximum() > 0
    scrollbar.setValue(0)
    event = QWheelEvent(
        QPointF(20, 20),
        QPointF(20, 20),
        QPoint(),
        QPoint(0, -120),
        Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
        Qt.ScrollPhase.ScrollUpdate,
        False,
    )
    tree.wheelEvent(event)
    assert scrollbar.value() > 0
    window.close()
    app.processEvents()


def test_render_queue_backspace_removes_selected_job(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.render_queue = RenderQueueStore(tmp_path / "render-queue.json")
    sources = [tmp_path / f"P{number:02d}.mov" for number in range(1, 6)]
    for source in sources:
        source.touch()
    window.render_queue.add(
        RenderJob.create(
            name="Queued Take",
            source_paths=sources,
            config_snapshot=json.loads(
                Path("configs/five_cam_180.sample.json").read_text()
            ),
            output_path=tmp_path / "queued-take.mov",
            in_frame=0,
            out_frame=12,
        )
    )
    window._refresh_queue_table()
    window.show()
    window.right_tabs.setCurrentIndex(1)
    app.processEvents()
    window.queue_table.selectRow(0)
    window.queue_table.setFocus()

    QTest.keyClick(window.queue_table, Qt.Key.Key_Backspace)

    assert window.render_queue.jobs == ()
    window.close()
    app.processEvents()


def test_project_and_timeline_settings_expose_resolution_and_ocio_transforms(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Settings" / "project.json"
    ProjectStore.create(project_path, name="Settings")
    window = MainWindow(project_path)
    timeline = window.project_store.add_timeline(
        TimelineRecord.create(
            name="Take 01",
            source_paths=(),
            config_snapshot=window._collect_config(),
        )
    )
    window._active_timeline_id = timeline.id
    captured: list[tuple[str, str]] = []

    def inspect_and_reject(dialog: QDialog) -> QDialog.DialogCode:
        captured.append(
            (
                dialog.windowTitle(),
                " ".join(label.text() for label in dialog.findChildren(QLabel)),
            )
        )
        return QDialog.DialogCode.Rejected

    monkeypatch.setattr(QDialog, "exec", inspect_and_reject)
    window.edit_project_settings()
    window.edit_timeline_settings()

    assert len(captured) == 2
    for _title, labels in captured:
        assert "width" in labels.lower()
        assert "height" in labels.lower()
        assert "frame rate" in labels.lower()
        assert "custom fps" in labels.lower()
        assert "Input transform" in labels
        assert "Working space" in labels
        assert "Output color space" in labels
    assert captured[1][0] == "Timeline Settings · Take 01"
    window.close()
    app.processEvents()


def test_timeline_project_settings_inheritance_can_be_overridden(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Inheritance" / "project.json"
    ProjectStore.create(
        project_path,
        name="Inheritance",
        settings_snapshot={
            "output": {"width": 18_000, "height": 5_000},
            "video": {"fps": 24.0},
            "_vpstitch": {"fps_mode": "custom"},
            "color": {
                "mode": "ocio",
                "ocio_config": "vpstitch://aces-studio-v4.0.0",
                "working_space": "ACEScg",
                "output_space": "sRGB - Display",
            },
            "cameras": [{"colorspace": "Camera Rec.709"}],
        },
    )
    window = MainWindow(project_path)
    local = window._collect_config()
    local["output"]["width"] = 12_000
    local["cameras"][0]["colorspace"] = "V-Log V-Gamut"
    timeline = TimelineRecord.create(
        name="Take 01", source_paths=(), config_snapshot=local
    )

    inherited = window._effective_timeline_config(timeline)
    assert inherited["video"]["fps"] == 24.0
    assert inherited["_vpstitch"]["fps_mode"] == "custom"
    overridden = window._effective_timeline_config(
        TimelineRecord.create(
            name="Take 02",
            source_paths=(),
            config_snapshot=local,
            inherits_project_settings=False,
        )
    )

    assert inherited["output"]["width"] == 18_000
    assert inherited["cameras"][0]["colorspace"] == "Camera Rec.709"
    assert overridden["output"]["width"] == 12_000
    assert overridden["cameras"][0]["colorspace"] == "V-Log V-Gamut"
    window.close()
    app.processEvents()


def test_reassigning_active_timeline_keeps_new_media_set(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Reassign" / "project.json"
    store = ProjectStore.create(project_path, name="Reassign")
    initial = [tmp_path / f"rear_P{number:02d}.mov" for number in range(1, 6)]
    replacement = [tmp_path / f"front_P{number:02d}.mov" for number in (8, 6, 7)]
    for path in [*initial, *replacement]:
        path.touch()
    config = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    timeline = store.add_timeline(
        TimelineRecord.create(
            name="Take 01", source_paths=initial, config_snapshot=config
        )
    )
    window = MainWindow(project_path)
    window.load_project_timeline(timeline.id)
    window.import_media_paths([str(path) for path in replacement])

    window.add_selected_media_to_timeline(timeline.id)

    updated = window._active_timeline_record()
    assert updated is not None
    assert [Path(path).name for path in updated.source_paths] == [
        "front_P06.mov", "front_P07.mov", "front_P08.mov"
    ]
    window.close()
    app.processEvents()


def test_project_default_change_invalidates_only_inherited_timeline_caches(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Invalidate" / "project.json"
    store = ProjectStore.create(project_path, name="Invalidate")
    sources = [tmp_path / f"P{number:02d}.mov" for number in range(1, 6)]
    for path in sources:
        path.touch()
    config = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    inherited = store.add_timeline(
        TimelineRecord.create(
            name="Inherited",
            source_paths=sources,
            config_snapshot=config,
            playback_cache_path=tmp_path / "inherited.mp4",
            playback_cache_status=PlaybackCacheStatus.READY,
            stitch_status=StitchStatus.READY,
        )
    )
    overridden = store.add_timeline(
        TimelineRecord.create(
            name="Override",
            source_paths=sources,
            config_snapshot=config,
            inherits_project_settings=False,
            playback_cache_path=tmp_path / "override.mp4",
            playback_cache_status=PlaybackCacheStatus.READY,
            stitch_status=StitchStatus.READY,
            order=1,
        )
    )
    window = MainWindow(project_path)

    window._invalidate_inherited_timeline_caches()

    fresh_inherited = next(item for item in window.project_store.timelines if item.id == inherited.id)
    fresh_overridden = next(item for item in window.project_store.timelines if item.id == overridden.id)
    assert fresh_inherited.playback_cache_path is None
    assert fresh_inherited.playback_cache_status is PlaybackCacheStatus.PENDING
    assert fresh_inherited.stitch_status is StitchStatus.UNSTITCHED
    assert fresh_overridden.playback_cache_path == overridden.playback_cache_path
    assert fresh_overridden.playback_cache_status is PlaybackCacheStatus.READY
    window.close()
    app.processEvents()


def test_queue_context_uses_selected_timeline_not_previous_active(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Queue Selection" / "project.json"
    store = ProjectStore.create(project_path, name="Queue Selection")
    config = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    first = store.add_timeline(
        TimelineRecord.create(name="First", source_paths=(), config_snapshot=config)
    )
    second = store.add_timeline(
        TimelineRecord.create(name="Second", source_paths=(), config_snapshot=config, order=1)
    )
    window = MainWindow(project_path)
    window._active_timeline_id = first.id
    window._refresh_media_tree()
    window.timeline_tree.setCurrentItem(window.timeline_tree.topLevelItem(1))
    calls: list[str] = []

    def load_selected(timeline_id: str) -> None:
        calls.append(f"load:{timeline_id}")
        window._active_timeline_id = timeline_id

    monkeypatch.setattr(window, "load_project_timeline", load_selected)
    monkeypatch.setattr(window, "add_current_to_queue", lambda: calls.append("queue"))

    window.add_selected_timeline_to_queue()

    assert calls == [f"load:{second.id}", "queue"]
    window.close()
    app.processEvents()


def test_queue_keeps_exact_per_job_destinations_and_rejects_failed_collision(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Queue Outputs" / "project.json"
    store = ProjectStore.create(project_path, name="Queue Outputs")
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(source)
    config = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    timeline = store.add_timeline(
        TimelineRecord.create(
            name="Hero Take",
            source_paths=sources,
            config_snapshot=config,
            inherits_project_settings=False,
        )
    )
    window = MainWindow(project_path)
    window.load_project_timeline(timeline.id)
    _cache_source_fps(window, sources, 24.0)
    window.output_directory.setText(str(tmp_path / "renders"))

    window.output_name.setText("Hero_A.mov.mov")
    window.add_current_to_queue()
    window.output_name.setText("Hero_B")
    window.add_current_to_queue()

    assert [str(job.output_path) for job in window.render_queue.jobs] == [
        str(tmp_path / "renders" / "Hero_A.mov"),
        str(tmp_path / "renders" / "Hero_B.mov"),
    ]
    first = window.render_queue.jobs[0]
    window.render_queue.update(first.id, status=RenderStatus.FAILED)
    errors: list[str] = []
    monkeypatch.setattr(window, "_error", lambda _title, message: errors.append(message))
    window.output_name.setText("Hero_A.MOV")
    window.add_current_to_queue()

    assert len(window.render_queue.jobs) == 2
    assert errors == ["Another queued timeline already uses this output path"]
    window.close()
    app.processEvents()


def test_queued_render_uses_locked_full_resolution_fine_tune_after_ui_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Locked Queue" / "project.json"
    store = ProjectStore.create(project_path, name="Locked Queue")
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(source)
    base = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    timeline = store.add_timeline(
        TimelineRecord.create(
            name="Locked Take",
            source_paths=sources,
            config_snapshot=base,
            inherits_project_settings=False,
        )
    )
    window = MainWindow(project_path)
    window.load_project_timeline(timeline.id)
    _cache_source_fps(window, sources, 24_000 / 1_001)
    window.source_table.selectRow(0)
    window._source_selection_changed()
    window.apply_aces_preset()
    for camera in window.config_data["cameras"]:
        camera["color_gain"] = [1.03, 1.0, 0.97]
        camera["color_match_confidence"] = 0.92
    window.color_match_enabled.setChecked(True)
    window.color_match_strength.setValue(80)
    window.plate_position_x.setValue(-71.25)
    window.plate_position_y.setValue(2.5)
    window.plate_scale.setValue(104.0)
    window.plate_crop_left.setValue(3.0)
    window.plate_feather_right.setValue(5.5)
    window.canvas_width.setValue(12_800)
    window.canvas_height.setValue(3_520)
    window.center_yaw.setValue(4.5)
    window.seam_feather.setValue(6.25)
    window.output_directory.setText(str(tmp_path / "renders"))
    window.output_name.setText("locked-A")

    window.add_current_to_queue()

    queued = window.render_queue.jobs[0]
    locked = json.loads(json.dumps(queued.config_snapshot))
    assert locked["output"]["width"] == 12_800
    assert locked["output"]["height"] == 3_520
    assert locked["output"]["center_yaw_deg"] == 4.5
    assert locked["output"]["seam_feather_deg"] == 6.25
    assert locked["cameras"][0]["yaw_deg"] == -71.25
    assert locked["cameras"][0]["pitch_deg"] == 2.5
    assert locked["cameras"][0]["scale"] == 1.04
    assert locked["cameras"][0]["crop_left"] == 0.03
    assert locked["cameras"][0]["feather_right_deg"] == 5.5
    assert locked["cameras"][0]["color_gain"] == [1.03, 1.0, 0.97]
    assert locked["color"]["match_enabled"] is True
    assert locked["color"]["match_strength"] == 0.8
    assert locked["video"]["fps"] == pytest.approx(24_000 / 1_001)
    assert locked["_vpstitch"]["source_fps"] == pytest.approx(24_000 / 1_001)

    window.fps_mode.setCurrentIndex(window.fps_mode.findData("custom"))
    window.fps.setValue(24.0)

    for camera in window.config_data["cameras"]:
        camera["color_gain"] = [0.97, 1.0, 1.03]
    window.color_match_strength.setValue(20)
    window.plate_position_x.setValue(-60.0)
    window.plate_position_y.setValue(-4.0)
    window.plate_scale.setValue(92.0)
    window.plate_crop_left.setValue(8.0)
    window.plate_feather_right.setValue(2.0)
    window.canvas_width.setValue(8_192)
    window.canvas_height.setValue(2_048)
    window.center_yaw.setValue(-8.0)
    window.seam_feather.setValue(2.5)
    current = window._collect_config()
    assert current["cameras"][0]["yaw_deg"] == -60.0
    assert current["cameras"][0]["color_gain"] == [0.97, 1.0, 1.03]
    assert current["output"]["width"] == 8_192
    assert current["color"]["match_strength"] == 0.2

    captured: dict[str, object] = {}

    def capture_render(
        task: str,
        arguments: list[str],
        _success=None,
        _failure=None,
        **_kwargs: object,
    ) -> None:
        config_path = Path(arguments[arguments.index("--config") + 1])
        captured["task"] = task
        captured["arguments"] = arguments
        captured["config"] = json.loads(config_path.read_text(encoding="utf-8"))

    monkeypatch.setattr(window, "_run_cli", capture_render)
    window._live_preview_timer.stop()
    window._start_queue_job(queued)

    render_config = captured["config"]
    assert captured["task"] == "QUEUE · Locked Take"
    assert "preview-config.json" not in str(captured["arguments"])
    assert render_config == locked
    assert render_config["output"]["width"] == 12_800
    assert render_config["cameras"][0]["yaw_deg"] == -71.25
    assert render_config["cameras"][0]["color_gain"] == [1.03, 1.0, 0.97]
    assert render_config["color"]["match_strength"] == 0.8
    window.close()
    app.processEvents()


def test_import_media_populates_media_pool_without_creating_timeline(
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

    added = window.import_media_paths([str(clip) for clip in clips])

    assert len(added) == 3
    assert len(window._selected_media_records()) == 3
    assert len(window.project_store.timelines) == 0
    assert [Path(item.path).name for item in window.project_store.media] == [
        "take_07_P06.mov",
        "take_07_P07.mov",
        "take_07_P08.mov",
    ]
    assert window.timeline_tree.topLevelItemCount() == 0
    folder = window.media_tree.topLevelItem(0).child(0)
    assert [folder.child(index).data(0, Qt.ItemDataRole.UserRole) for index in range(3)] == [
        "media", "media", "media"
    ]
    window.close()
    app.processEvents()


def test_create_timeline_uses_selected_camera_layout_before_media_is_added(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Production" / "project.json"
    store = ProjectStore.create(project_path, name="Production")
    store.add_bin(Bin.create("Front"))
    window = MainWindow(project_path)
    monkeypatch.setattr(
        window,
        "_request_new_timeline",
        lambda _default: ("Front Take", 3, False),
    )

    window.create_timeline()

    timeline = window.project_store.timelines[0]
    assert timeline.name == "Front Take"
    assert timeline.source_paths == ()
    assert len(timeline.config_snapshot["cameras"]) == 3
    assert window.source_table.camera_count() == 3
    assert "3-CAM · P06–P08 · EMPTY" in window.timeline_tree.topLevelItem(0).text(0)
    window.close()
    app.processEvents()


def test_three_camera_profile_uses_adjacent_center_positions() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))

    profile = window._profile_for_count(3)

    assert [camera["name"] for camera in profile["cameras"]] == ["cam0", "cam1", "cam2"]
    assert [round(float(camera["yaw_deg"]), 1) for camera in profile["cameras"]] == [
        -38.2,
        0.0,
        39.0,
    ]
    window.close()
    app.processEvents()


def test_backspace_removes_media_but_keeps_files_and_timeline_references(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Production" / "project.json"
    store = ProjectStore.create(project_path, name="Production")
    folder = store.add_bin(Bin.create("Front"))
    clips = [tmp_path / f"front_P{number:02d}.mov" for number in (6, 7, 8)]
    for clip in clips:
        clip.touch()
    timeline = store.add_timeline(
        TimelineRecord.create(
            name="Front Take",
            source_paths=clips,
            config_snapshot=json.loads(
                Path("configs/drive_5cam_180.prores-hq.json").read_text()
            ),
            bin_id=folder.id,
        )
    )
    window = MainWindow(project_path)
    window.import_media_paths([str(path) for path in clips])
    monkeypatch.setattr(
        window,
        "_show_message",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.show()
    app.processEvents()
    window.media_tree.setFocus()
    QTest.keyClick(window.media_tree, Qt.Key.Key_Backspace)

    assert window.project_store.media == ()
    assert all(path.is_file() for path in clips)
    assert window.project_store.timelines[0].id == timeline.id
    assert window.project_store.timelines[0].source_paths == tuple(clips)
    window.close()
    app.processEvents()


def test_backspace_deletes_selected_plate_set_timeline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Production" / "project.json"
    store = ProjectStore.create(project_path, name="Production")
    store.add_timeline(
        TimelineRecord.create(
            name="Delete Me",
            source_paths=(),
            config_snapshot=json.loads(
                Path("configs/drive_5cam_180.prores-hq.json").read_text()
            ),
        )
    )
    window = MainWindow(project_path)
    window.timeline_tree.setCurrentItem(window.timeline_tree.topLevelItem(0))
    monkeypatch.setattr(
        window,
        "_show_message",
        lambda *_args, **_kwargs: QMessageBox.StandardButton.Yes,
    )

    window.show()
    app.processEvents()
    window.timeline_tree.setFocus()
    QTest.keyClick(window.timeline_tree, Qt.Key.Key_Backspace)

    assert window.project_store.timelines == ()
    window.close()
    app.processEvents()


def test_named_timeline_receives_selected_media_and_updates_workspace_context(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Production" / "project.json"
    store = ProjectStore.create(project_path, name="Production")
    store.add_bin(Bin.create("Bridge"))
    window = MainWindow(project_path)
    clips = [tmp_path / f"hero_P{number:02d}.mov" for number in (5, 2, 4, 1, 3)]
    for clip in clips:
        clip.touch()
    monkeypatch.setattr(
        window,
        "_request_new_timeline",
        lambda _default: ("Bridge Hero", 5, False),
    )

    window.create_timeline()
    timeline = window.project_store.timelines[0]
    window.import_media_paths([str(clip) for clip in clips])
    window.add_selected_media_to_timeline(timeline.id)

    assert len(window.project_store.timelines) == 1
    timeline = window.project_store.timelines[0]
    assert timeline.name == "Bridge Hero"
    assert len(timeline.source_paths) == 5
    assert window._active_timeline_id == timeline.id
    assert window.active_plates_title.text() == "ACTIVE TIMELINE · Bridge Hero · 5 PLATES"
    assert window.preview_context.text() == "Production / Bridge / Bridge Hero"
    assert window.timing_title.text() == "TIMELINE RANGE · Bridge Hero"
    timeline_item = window.timeline_tree.topLevelItem(0)
    assert timeline_item.text(0).startswith("Bridge Hero\n")
    assert timeline_item.background(0).color().name() == "#28282c"
    assert "P01–P05" in timeline_item.text(0)
    assert "EMPTY" not in timeline_item.text(0)
    assert timeline_item.childCount() == 0
    assert window.media_tree.topLevelItem(0).child(0).childCount() == 5
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
                "ocio_config": "vpstitch://aces-studio-v4.0.0",
                "working_space": "ACEScg",
                "output_space": "sRGB - Display",
            },
            "cameras": [{"colorspace": "Camera Rec.709"}],
        },
    )

    window = MainWindow(project_path)

    assert window.canvas_width.value() == 18_000
    assert window.canvas_height.value() == 5_000
    assert window.color_mode.currentData() == "ocio"
    assert window.ocio_config.text() == "vpstitch://aces-studio-v4.0.0"
    assert window.working_space.currentText() == "ACEScg"
    assert window.output_space.currentText() == "sRGB - Display"
    assert window.input_space.currentText() == "Camera Rec.709"
    window.close()
    app.processEvents()


def test_invalid_saved_ocio_output_is_recovered_before_proxy_or_render(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "OCIO Recovery" / "project.json"
    ProjectStore.create(
        project_path,
        name="OCIO Recovery",
        settings_snapshot={
            "color": {
                "mode": "ocio",
                "ocio_config": "vpstitch://aces-studio-v4.0.0",
                "working_space": "ACEScg",
                "output_space": "sRGB - Displayㄴ",
            },
            "cameras": [{"colorspace": "Camera Rec.709"}],
        },
    )

    window = MainWindow(project_path)
    collected = window._collect_config()

    assert window.output_space.isEditable() is False
    assert window.output_space.currentText() == "sRGB - Display"
    assert collected["color"]["output_space"] == "sRGB - Display"
    assert "corrected" in window.ocio_space_status.text()
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
    monkeypatch.setattr(window, "play_forward", lambda: commands.append("L"))
    monkeypatch.setattr(window, "stop_playback", lambda: commands.append("K"))
    monkeypatch.setattr(window, "step_playback", lambda value: commands.append(str(value)))

    window.preview.setFocus()
    for key in (
        Qt.Key.Key_P,
        Qt.Key.Key_Space,
        Qt.Key.Key_J,
        Qt.Key.Key_K,
        Qt.Key.Key_L,
        Qt.Key.Key_Left,
        Qt.Key.Key_Right,
    ):
        QTest.keyClick(window.preview.viewport(), key)

    assert commands == ["P", "SPACE", "J", "K", "L", "-1", "1"]
    window.close()
    app.processEvents()


def test_fullscreen_shortcut_is_window_wide_and_transport_respects_focus(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.show()
    app.processEvents()
    commands: list[str] = []
    monkeypatch.setattr(
        window, "toggle_preview_fullscreen", lambda: commands.append("P")
    )
    monkeypatch.setattr(window, "play_forward", lambda: commands.append("L"))

    window.log.setFocus()
    QTest.keyClick(window, Qt.Key.Key_P)
    QTest.keyClick(window, Qt.Key.Key_L)
    app.processEvents()
    assert commands == ["P"]

    window.preview.setFocus()
    QTest.keyClick(window, Qt.Key.Key_L)
    app.processEvents()
    assert commands == ["P", "L"]
    window.close()
    app.processEvents()


def test_space_resumes_loaded_proxy_without_resetting_position(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    proxy = tmp_path / "proxy.mp4"
    proxy.touch()

    class PausedPlayer:
        def __init__(self) -> None:
            self.play_calls = 0
            self.position_value = 420

        def playbackState(self):
            return QMediaPlayer.PlaybackState.PausedState

        def duration(self) -> int:
            return 1000

        def position(self) -> int:
            return self.position_value

        def setPosition(self, value: int) -> None:
            self.position_value = value

        def play(self) -> None:
            self.play_calls += 1

        def stop(self) -> None:
            pass

    player = PausedPlayer()
    window.media_player = player  # type: ignore[assignment]
    window._playback_path = proxy
    window._playback_key = "same-key"
    monkeypatch.setattr(window, "_playback_signature", lambda: ("same-key", []))

    window.toggle_playback()

    assert player.play_calls == 1
    assert player.position_value == 420
    window.close()
    app.processEvents()


def test_play_button_works_even_when_the_button_has_focus(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    proxy = tmp_path / "proxy.mp4"
    proxy.touch()

    class PausedPlayer:
        play_calls = 0

        def playbackState(self):
            return QMediaPlayer.PlaybackState.PausedState

        def duration(self) -> int:
            return 1000

        def position(self) -> int:
            return 200

        def play(self) -> None:
            self.play_calls += 1

        def stop(self) -> None:
            pass

    player = PausedPlayer()
    window.media_player = player  # type: ignore[assignment]
    window._playback_path = proxy
    window._playback_key = "same-key"
    monkeypatch.setattr(window, "_playback_signature", lambda: ("same-key", []))
    window.playback_button.setFocus()

    window.toggle_playback()

    assert player.play_calls == 1
    window.close()
    app.processEvents()


def test_stopping_playback_for_fine_tune_preserves_selected_frame() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {"fps": 24.0}
    window._timeline_maximum = 100
    window.timeline_in.setRange(0, 99)
    window.timeline_out.setRange(1, 100)
    window.timeline_playhead.setRange(0, 99)
    window.timeline_bar.set_frame_range(100, 0, 100, 42)
    window._set_playhead(42)

    class ResettingPlayer:
        blocked = False

        def blockSignals(self, value: bool) -> None:
            self.blocked = value

        def stop(self) -> None:
            if not self.blocked:
                window._set_playhead(0)

        def setSource(self, _source) -> None:  # type: ignore[no-untyped-def]
            pass

    window.media_player = ResettingPlayer()  # type: ignore[assignment]

    window._stop_playback(clear=True)

    assert window.timeline_playhead.value() == 42
    window.close()
    app.processEvents()


def test_fine_tune_at_new_playhead_extracts_that_frame_before_restitch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {"fps": 24.0}
    window._timeline_maximum = 100
    window.timeline_in.setRange(0, 99)
    window.timeline_out.setRange(1, 100)
    window.timeline_playhead.setRange(0, 99)
    window.timeline_bar.set_frame_range(100, 0, 100, 37)
    window._set_playhead(37)
    window._reference_frame_index = 0
    window._live_preview_pending = True
    calls: list[int] = []
    monkeypatch.setattr(window, "_save_active_timeline", lambda: True)
    monkeypatch.setattr(
        window,
        "create_preview",
        lambda **_kwargs: calls.append(window.timeline_playhead.value()),
    )

    window._run_live_preview()

    assert calls == [37]
    assert "frame 37" in window.preview_note.text()
    window.close()
    app.processEvents()


def test_scrubbable_value_uses_shift_for_ten_times_finer_control() -> None:
    app = QApplication.instance() or QApplication([])
    control = ScrubbableDoubleSpinBox()
    control.setSingleStep(0.2)

    normal = control._drag_increment(20)
    precise = control._drag_increment(
        20, Qt.KeyboardModifier.ShiftModifier
    )

    assert normal == pytest.approx(1.0)
    assert precise == pytest.approx(0.1)
    control.close()
    app.processEvents()


def test_j_and_l_run_continuous_reverse_and_forward_transport(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    proxy = tmp_path / "proxy.mp4"
    proxy.touch()

    class TransportPlayer:
        position_value = 250
        play_calls = 0

        def pause(self) -> None:
            pass

        def play(self) -> None:
            self.play_calls += 1

        def stop(self) -> None:
            pass

        def position(self) -> int:
            return self.position_value

        def duration(self) -> int:
            return 1000

        def setPosition(self, value: int) -> None:
            self.position_value = value

    player = TransportPlayer()
    window.media_player = player  # type: ignore[assignment]
    window._playback_path = proxy
    window._playback_key = "same-key"
    window._tc_alignment = {"fps": 24.0}
    monkeypatch.setattr(window, "_playback_signature", lambda: ("same-key", []))

    window.play_reverse()
    window._reverse_tick()
    assert window._reverse_timer.isActive()
    assert player.position_value < 250

    window.play_forward()
    assert not window._reverse_timer.isActive()
    assert player.play_calls == 1
    window.close()
    app.processEvents()


def test_playback_is_queued_while_preview_frame_is_loading(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {"fps": 24.0}
    window.process = object()  # type: ignore[assignment]

    window.toggle_playback()

    assert window._pending_playback_request is True
    assert window._auto_cache_requested is True
    assert "Playback queued" in window.preview_note.text()
    window.process = None
    window.close()
    app.processEvents()


def test_queued_playback_autoplays_and_clears_request(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._cache_dir = tmp_path
    window._tc_alignment = {"fps": 24.0}
    window._pending_playback_request = True
    window._auto_cache_requested = True
    monkeypatch.setattr(window, "_playback_signature", lambda: ("new-rig", ["P01.mov"]))
    captured: list[bool] = []
    monkeypatch.setattr(
        window,
        "_build_playback",
        lambda _path, _key, _sources, *, autoplay=True: captured.append(autoplay),
    )

    window._warm_playback_cache()

    assert captured == [True]
    assert window._pending_playback_request is False
    assert window._auto_cache_requested is False
    window.close()
    app.processEvents()


def test_fullscreen_preview_rescales_after_actual_screen_resize() -> None:
    app = QApplication.instance() or QApplication([])
    source = QPixmap(400, 100)
    source.fill(Qt.GlobalColor.white)
    label = FullscreenPreviewLabel(source)
    label.resize(1000, 700)
    label.show()
    app.processEvents()

    assert label.pixmap() is not None
    assert label.pixmap().width() == 1000
    assert label.pixmap().height() == 250
    label.close()
    app.processEvents()


def test_video_fullscreen_uses_top_level_host_and_restores_video_output(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    proxy = tmp_path / "proxy.mp4"
    proxy.touch()
    window._playback_path = proxy
    window._playback_key = "current"
    window._playback_signature = lambda: ("current", [])  # type: ignore[method-assign]

    window.toggle_preview_fullscreen()
    app.processEvents()

    assert window._fullscreen_preview is not None
    assert window._fullscreen_preview.isFullScreen()
    assert window._fullscreen_preview.parent() is None
    assert window._fullscreen_preview.windowFlags() & Qt.WindowType.FramelessWindowHint
    assert window._fullscreen_video is not None
    window._fullscreen_preview.close()
    app.processEvents()
    assert window._fullscreen_preview is None
    assert window._fullscreen_video is None
    window.close()
    app.processEvents()


def test_stale_playback_fullscreen_uses_current_interactive_frame(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    proxy = tmp_path / "stale.mp4"
    proxy.touch()
    window._playback_path = proxy
    window._playback_key = None

    window.toggle_preview_fullscreen()
    app.processEvents()

    assert window._fullscreen_video is None
    assert window._fullscreen_live_label is not None
    window._fullscreen_preview.close()
    window.close()
    app.processEvents()


def test_toggle_playback_prefers_ready_live_source_proxy() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._tc_alignment = {"fps": 24.0}
    requested: list[tuple[int, bool, int]] = []

    def request(frame, _message, *, playing=False, direction=1):  # type: ignore[no-untyped-def]
        requested.append((frame, playing, direction))
        return True

    window._request_live_proxy_frame = request  # type: ignore[method-assign]

    window.toggle_playback()

    assert requested == [(window.timeline_playhead.value(), True, 1)]
    assert window.playback_button.text() == "Ⅱ  PAUSE"
    window.close()
    app.processEvents()


def test_color_tab_collects_camera_match_and_hdr_display_view() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    window.color_mode.setCurrentIndex(window.color_mode.findData("ocio"))
    window.ocio_config.setText("vpstitch://aces-studio-v4.0.0")
    window.input_space.setEditText("Camera Rec.709")
    window.working_space.setEditText("ACEScg")
    assert window._reload_ocio_spaces(quiet=True)
    window.output_mode.setCurrentIndex(
        window.output_mode.findData("display_view")
    )
    window.output_display.setCurrentIndex(
        window.output_display.findData("Rec.2100-PQ - Display")
    )
    window._delivery_display_changed()
    window.output_view.setCurrentIndex(
        window.output_view.findText("ACES 2.0 - HDR 1000 nits (Rec.2020)")
    )
    cameras = window.config_data["cameras"]
    for index, camera in enumerate(cameras):
        camera["color_gain"] = [1.0 + index * 0.001, 1.0, 1.0 - index * 0.001]
        camera["color_match_confidence"] = 0.9
    window.color_match_enabled.blockSignals(True)
    window.color_match_enabled.setChecked(True)
    window.color_match_enabled.blockSignals(False)
    window.color_match_strength.setValue(80)

    collected = window._collect_config()

    assert collected["color"]["output_mode"] == "display_view"
    assert collected["color"]["display"] == "Rec.2100-PQ - Display"
    assert collected["color"]["view"] == "ACES 2.0 - HDR 1000 nits (Rec.2020)"
    assert collected["color"]["match_enabled"] is True
    assert collected["color"]["match_strength"] == 0.8
    assert collected["cameras"][4]["color_gain"] == [1.004, 1.0, 0.996]
    assert collected["video"]["color_primaries"] == "bt2020"
    assert collected["video"]["color_trc"] == "smpte2084"
    assert collected["video"]["colorspace"] == "bt2020nc"
    window.close()
    app.processEvents()


def test_autosave_snapshot_skips_unchanged_project(tmp_path: Path) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Autosave" / "project.json"
    store = ProjectStore.create(project_path, name="Autosave")
    store.add_bin(Bin.create("Master"))
    config = json.loads(Path("configs/drive_5cam_180.prores-hq.json").read_text())
    empty = store.add_timeline(
        TimelineRecord.create(
            name="Empty Timeline", source_paths=(), config_snapshot=config
        )
    )
    window = MainWindow(project_path)
    window.load_project_timeline(empty.id)
    recovery = project_path.with_name("project.autosave.json")

    assert window._autosave_project_snapshot() is True
    first_mtime = recovery.stat().st_mtime_ns
    assert window._autosave_project_snapshot() is False
    assert recovery.stat().st_mtime_ns == first_mtime

    window.project_store.update_settings(name="Autosave Updated")
    assert window._autosave_project_snapshot() is True
    assert ProjectStore.load(recovery, autosave=False).settings.name == "Autosave Updated"
    window.close()
    app.processEvents()


def test_plate_inspector_persists_transform_crop_and_feather_per_timeline(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    project_path = tmp_path / "Plate Inspector" / "project.json"
    store = ProjectStore.create(project_path, name="Plate Inspector")
    folder = store.add_bin(Bin.create("Shoot"))
    sources = []
    for number in range(1, 6):
        source = tmp_path / f"P{number:02d}.mov"
        source.touch()
        sources.append(source)
    config = json.loads(Path("configs/five_cam_180.sample.json").read_text())
    timeline = store.add_timeline(
        TimelineRecord.create(
            name="Fine Tune",
            bin_id=folder.id,
            source_paths=sources,
            config_snapshot=config,
            inherits_project_settings=False,
        )
    )
    window = MainWindow(project_path)
    window.load_project_timeline(timeline.id)
    window.source_table.selectRow(1)
    window._source_selection_changed()

    window.plate_position_x.setValue(-35.25)
    window.plate_position_y.setValue(1.5)
    window.plate_scale.setValue(107.5)
    window.plate_crop_left.setValue(4.0)
    window.plate_crop_right.setValue(2.0)
    window.plate_feather_left.setValue(2.5)
    window.plate_feather_right.setValue(6.0)
    window.plate_warp_controls[0].setValue(0.0125)
    window.plate_warp_controls[1].setValue(-0.001)
    window._live_preview_timer.stop()
    window._run_live_preview()

    camera = window.project_store.timelines[0].config_snapshot["cameras"][1]
    assert camera["yaw_deg"] == -35.25
    assert camera["pitch_deg"] == 1.5
    assert camera["scale"] == 1.075
    assert camera["crop_left"] == 0.04
    assert camera["crop_right"] == 0.02
    assert camera["feather_left_deg"] == 2.5
    assert camera["feather_right_deg"] == 6.0
    assert camera["lens"]["distortion"][:2] == [0.0125, -0.001]
    assert window.settings_tabs.tabText(window.settings_tabs.currentIndex()) == "PLATE"

    window._reset_selected_plate()
    window._live_preview_timer.stop()
    window._run_live_preview()
    reset_camera = window.project_store.timelines[0].config_snapshot["cameras"][1]
    assert reset_camera["yaw_deg"] == config["cameras"][1]["yaw_deg"]
    assert "scale" not in reset_camera
    assert "crop_left" not in reset_camera
    assert reset_camera["lens"]["distortion"] == config["cameras"][1]["lens"]["distortion"]
    window.close()
    app.processEvents()


def test_auto_stitch_uses_clean_profile_geometry_and_resets_old_fine_tune(
    tmp_path: Path,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "Auto Stitch" / "project.json")
    profile_path = Path("configs/drive_5cam_180.prores-hq.json")
    window.load_config(profile_path)
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    window.config_data["cameras"][0].update(
        {
            "yaw_deg": -75.0,
            "scale": 0.94,
            "crop_left": 0.03,
            "feather_right_deg": 2.0,
        }
    )

    reference = tmp_path / "reference-auto-stitch"
    reference.mkdir()
    preview = json.loads(json.dumps(window.config_data))
    preview_path = reference / "preview-config.json"
    preview_path.write_text(json.dumps(preview), encoding="utf-8")
    for camera in preview["cameras"]:
        (reference / f"{camera['name']}.png").touch()
    window._last_reference_dir = reference
    window._last_reference_config_path = preview_path
    window.create_preview = lambda *args, **kwargs: None  # type: ignore[method-assign]

    def fake_run_cli(
        task: str,
        arguments: list[str],
        success=None,
        _failure=None,
        **_kwargs: object,
    ) -> None:
        assert task == "AUTO ALIGN"
        calibration_path = Path(arguments[arguments.index("--config") + 1])
        calibration = json.loads(calibration_path.read_text(encoding="utf-8"))
        assert calibration["cameras"][0]["yaw_deg"] == pytest.approx(
            profile["cameras"][0]["yaw_deg"]
        )
        assert "scale" not in calibration["cameras"][0]
        assert "crop_left" not in calibration["cameras"][0]
        assert "feather_right_deg" not in calibration["cameras"][0]

        solved = json.loads(json.dumps(calibration))
        solved["cameras"][0]["yaw_deg"] = -76.25
        output_path = Path(arguments[arguments.index("--output") + 1])
        output_path.write_text(json.dumps(solved), encoding="utf-8")
        report_path = Path(arguments[arguments.index("--report") + 1])
        report_path.write_text(
            json.dumps(
                {
                    "pairs": [
                        {
                            "left_camera": f"cam{index}",
                            "right_camera": f"cam{index + 1}",
                            "matches": 160,
                            "inliers": 150,
                            "inlier_ratio": 0.9375,
                            "rms_angular_error_deg": 0.4,
                            "correction_from_initial_deg": 0.1,
                        }
                        for index in range(4)
                    ]
                }
            ),
            encoding="utf-8",
        )
        if success is not None:
            success()

    window._run_cli = fake_run_cli  # type: ignore[method-assign]
    window.auto_align()

    camera = window.config_data["cameras"][0]
    assert camera["yaw_deg"] == -76.25
    assert "scale" not in camera
    assert "crop_left" not in camera
    assert "feather_right_deg" not in camera
    assert camera["auto_stitch_base"]["yaw_deg"] == -76.25
    assert camera["fine_tune"]["active"] is False
    assert "scale" not in window._plate_reset_cameras[0]
    assert window._plate_reset_cameras[0]["yaw_deg"] == -76.25
    window.close()
    app.processEvents()


def test_plate_fine_tune_reuses_reference_images_and_runs_latest_live_preview(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    reference = tmp_path / "reference-live"
    reference.mkdir()
    full_config = window._write_working_config()
    preview_config = reference / "preview-config.json"
    width, height = preview_dimensions(
        window.canvas_width.value(),
        window.canvas_height.value(),
        max_width=2048,
        max_height=1152,
    )
    window._write_preview_config(full_config, preview_config, width, height)
    raw = json.loads(preview_config.read_text(encoding="utf-8"))
    for camera in raw["cameras"]:
        (reference / f"{camera['name']}.png").touch()

    calls: list[tuple[str, list[str], dict[str, object]]] = []
    interactive_requests: list[object] = []

    def fake_run_cli(
        task: str,
        arguments: list[str],
        _success=None,
        _failure=None,
        **kwargs: object,
    ) -> None:
        calls.append((task, arguments, kwargs))

    monkeypatch.setattr(window, "_run_cli", fake_run_cli)
    monkeypatch.setattr(
        window,
        "_start_interactive_preview",
        lambda: interactive_requests.append(window._interactive_request),
    )
    window._last_reference_dir = reference
    window._last_reference_config_path = preview_config
    window._preview_ready = True

    window.plate_position_x.setValue(-72.0)
    window.plate_position_x.setValue(-70.25)

    assert reference.is_dir()
    assert window._last_reference_dir == reference
    assert window._live_preview_pending is True
    window._live_preview_timer.stop()
    window._run_live_preview()

    refreshed = json.loads(preview_config.read_text(encoding="utf-8"))
    assert refreshed["cameras"][0]["yaw_deg"] == -70.25
    assert calls == []
    assert len(interactive_requests) == 1
    assert interactive_requests[0] is not None
    assert window._live_preview_pending is False
    window.close()
    app.processEvents()


def test_plate_adjustment_does_not_automatically_rebuild_playback_cache() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    window._tc_alignment = {"fps": 24.0}
    window._auto_cache_requested = False

    window.plate_position_x.setValue(-70.0)

    assert window._live_preview_pending is True
    assert window._auto_cache_requested is False
    assert window._playback_key is None
    window._live_preview_timer.stop()
    window.close()
    app.processEvents()


def test_plate_adjustment_cancels_an_in_progress_stale_playback_cache() -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))

    class CacheProcess:
        killed = False

        def kill(self) -> None:
            self.killed = True

    process = CacheProcess()
    window.process = process  # type: ignore[assignment]
    window._process_task_name = "BUILD PLAYBACK PROXY"
    window._auto_cache_in_progress = True
    window._auto_cache_requested = True

    window.plate_position_x.setValue(-70.0)

    assert process.killed is True
    assert window._playback_cache_cancelled_for_interaction is True
    assert window._auto_cache_requested is False
    window.process = None
    window._live_preview_timer.stop()
    window.close()
    app.processEvents()


def test_color_basis_change_keeps_cached_preview_ready_and_schedules_refresh(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    messages: list[str] = []
    monkeypatch.setattr(
        window,
        "_schedule_live_preview",
        lambda message, **_kwargs: messages.append(message),
    )
    window._preview_ready = True

    window._color_basis_changed()

    assert window._preview_ready is True
    assert messages == ["Color pipeline updated"]
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
    assert first_dialog.windowTitle() == "Import Media · Select Camera Clips"
    assert window._import_dialog is first_dialog
    window.close()
    app.processEvents()


def test_import_button_locks_selected_folder_before_dialog_opens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window._auto_workflows_enabled = False
    store = ProjectStore.create(tmp_path / "project.json", name="Button Import")
    shoot = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    day = store.add_bin(Bin.create("Day 01", parent_id=shoot.id, bin_id="day-01"))
    window.project_store = store
    window._refresh_media_tree()
    iterator = QTreeWidgetItemIterator(window.media_tree)
    while iterator.value() is not None:
        item = iterator.value()
        if item.data(0, Qt.ItemDataRole.UserRole + 1) == day.id:
            window.media_tree.setCurrentItem(item)
            break
        iterator += 1
    source = tmp_path / "button_P01.mov"

    def accept_after_focus_change(_dialog: QFileDialog) -> QDialog.DialogCode:
        root = window.media_tree.topLevelItem(0)
        window.media_tree.setCurrentItem(root)
        window._active_bin_id = None
        return QDialog.DialogCode.Accepted

    monkeypatch.setattr(QFileDialog, "exec", accept_after_focus_change)
    monkeypatch.setattr(QFileDialog, "selectedFiles", lambda _dialog: [str(source)])

    window.import_button.click()

    imported = store.list_media(day.id)
    assert len(imported) == 1
    assert imported[0].path == source
    assert store.list_media(None) == ()
    assert "Shoot / Day 01" in window.statusBar().currentMessage()
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


def test_plate_input_color_space_live_refreshes_but_video_range_reextracts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app = QApplication.instance() or QApplication([])
    window = MainWindow()
    window.load_config(Path("configs/drive_5cam_180.prores-hq.json"))
    paths = [str(tmp_path / f"P{number:02d}.mov") for number in range(1, 6)]
    for path in paths:
        Path(path).touch()
    window.source_table.set_paths(paths)
    reference = tmp_path / "reference-input-color"
    reference.mkdir()
    preview_config = reference / "preview-config.json"
    preview_config.touch()
    window._last_reference_dir = reference
    window._last_reference_config_path = preview_config
    window._preview_ready = True
    messages: list[str] = []
    monkeypatch.setattr(
        window,
        "_schedule_live_preview",
        lambda message, **_kwargs: messages.append(message),
    )
    monkeypatch.setattr(
        InputSettingsDialog,
        "exec",
        lambda _dialog: QDialog.DialogCode.Accepted,
    )
    monkeypatch.setattr(
        InputSettingsDialog,
        "values",
        lambda _dialog: {
            "input_color_space": "bt709",
            "input_video_range": None,
        },
    )

    window._open_input_settings([0])

    assert window._last_reference_dir == reference
    assert messages == ["Plate input color space updated"]

    monkeypatch.setattr(
        InputSettingsDialog,
        "values",
        lambda _dialog: {
            "input_color_space": "bt709",
            "input_video_range": "tv",
        },
    )
    window._open_input_settings([0])

    assert window._last_reference_dir is None
    assert window._last_reference_config_path is None
    assert window._preview_ready is False
    assert messages == ["Plate input color space updated"]
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
    assert window._active_timeline_record() is None
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
    assert window._active_timeline_record() is None
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
    assert window.input_space.currentText() == "Camera Rec.709"
    assert window.working_space.currentText() == "ACEScg"
    assert window.input_space.isEditable() is False
    assert window.input_space.count() > 50
    assert window.input_space.count() == window.working_space.count()
    assert "OCIO spaces loaded" in window.ocio_space_status.text()
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


def test_color_match_basis_ignores_delivery_transform() -> None:
    base = {
        "cameras": [{"colorspace": "Camera Rec.709"}],
        "color": {
            "mode": "ocio",
            "ocio_config": "vpstitch://aces-studio-v4.0.0",
            "working_space": "ACEScg",
            "output_mode": "colorspace",
            "output_space": "Gamma 2.4 Encoded Rec.709",
        },
    }
    hdr = json.loads(json.dumps(base))
    hdr["color"].update(
        {
            "output_mode": "display_view",
            "display": "Rec.2100-PQ - Display",
            "view": "ACES 2.0 - HDR 1000 nits (Rec.2020)",
        }
    )
    assert MainWindow._color_match_basis(base) == MainWindow._color_match_basis(hdr)


def test_clear_color_match_snapshot_preserves_reference_and_strength() -> None:
    config = {
        "cameras": [
            {
                "name": "cam0",
                "color_gain": [0.95, 1.0, 1.05],
                "color_match_confidence": 0.8,
            }
        ],
        "color": {
            "mode": "ocio",
            "match_enabled": True,
            "match_reference": "cam0",
            "match_strength": 0.75,
        },
    }
    MainWindow._clear_color_match_snapshot(config)
    assert config["color"]["match_enabled"] is False
    assert config["color"]["match_reference"] == "cam0"
    assert config["color"]["match_strength"] == pytest.approx(0.75)
    assert config["cameras"][0]["color_gain"] == [1.0, 1.0, 1.0]
    assert "color_match_confidence" not in config["cameras"][0]
