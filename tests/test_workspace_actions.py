from __future__ import annotations

import os
from types import SimpleNamespace
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtGui import QImage
from PySide6.QtWidgets import QApplication

from vpstitch.gui import MainWindow, PreviewView
from vpstitch.renderqueue import RenderJob, RenderStatus
from vpstitch.project import Bin, MediaRecord, TimelineRecord


def test_queue_actions_follow_selection_and_running_task(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "project.json")
    assert not window.render_all_queue_button.isEnabled()
    assert not window.open_queue_output_button.isEnabled()
    assert not window.playback_button.isEnabled()
    assert not window.clear_button.isEnabled()
    job = window.render_queue.add(RenderJob.create(
        name="Take", source_paths=["a.mov", "b.mov", "c.mov"],
        config_snapshot=window.config_data, output_path=tmp_path / "take.mov",
        in_frame=0, out_frame=10,
    ))
    window._refresh_queue_table()
    window.queue_table.selectRow(0)
    assert window.render_selected_queue_button.isEnabled()
    assert window.render_all_queue_button.isEnabled()
    assert window.open_queue_output_button.isEnabled()
    window.process = object()
    window._set_busy_ui(True)
    assert not window.render_selected_queue_button.isEnabled()
    assert not window.render_all_queue_button.isEnabled()
    window.process = None
    window.render_queue.update(job.id, status=RenderStatus.DONE)
    window._refresh_queue_table()
    assert not window.render_all_queue_button.isEnabled()
    assert window.render_selected_queue_button.text() == "Render again"
    window.remove_queue_button.click()
    assert not window.render_queue.jobs
    assert not window.remove_queue_button.isEnabled()
    window.close()
    app.processEvents()


def test_viewer_keeps_scene_item_and_view_transform_between_frames():
    app = QApplication.instance() or QApplication([])
    view = PreviewView()
    view.resize(600, 400)
    image = QImage(320, 180, QImage.Format.Format_RGB32)
    image.fill(0xff253750)
    view.set_image(image)
    item = view._item
    view.scale(1.4, 1.4)
    transform = view.transform()
    image.fill(0xff758392)
    view.set_image(image)
    assert view._item is item
    assert view.transform() == transform
    assert len(view.scene().items()) == 1
    assert view._item.pixmap().toImage().pixel(0, 0) == image.pixel(0, 0)
    view.close()
    app.processEvents()


def test_open_timeline_enables_workflow_without_reselecting_media(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "project.json")
    sources = [tmp_path / f"P{i:02d}.mov" for i in range(1, 6)]
    for source in sources:
        source.touch()
    timeline = window.project_store.add_timeline(TimelineRecord.create(
        name="Take", bin_id=None, source_paths=sources,
        config_snapshot=window.config_data, inherits_project_settings=False,
    ))
    window.load_project_timeline(timeline.id)
    for button in (window.tc_align_button, window.preview_button,
                   window.add_queue_button, window.render_button):
        assert button.isEnabled(), button.text()
    window.close()
    app.processEvents()


def test_media_search_preserves_folder_context_and_clears_hidden_selection(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "project.json")
    folder = window.project_store.add_bin(Bin.create("Bridge"))
    for name in ("P01_bridge.mov", "P02_tunnel.mov"):
        window.project_store.add_media(MediaRecord.create(path=tmp_path / name, bin_id=folder.id))
    window._refresh_media_tree()
    root = window.media_tree.topLevelItem(0)
    parent = next(root.child(i) for i in range(root.childCount()) if root.child(i).text(0) == "Bridge")
    hidden = next(parent.child(i) for i in range(parent.childCount()) if "tunnel" in parent.child(i).text(0))
    hidden.setSelected(True)
    window.media_search.setText("P01")
    window._filter_media_tree()
    assert not parent.isHidden()
    assert parent.isExpanded()
    assert hidden.isHidden()
    assert not hidden.isSelected()
    assert window.media_hint.text() == "1 matching clip"
    window.media_search.setText("nothing matches")
    window._filter_media_tree()
    assert window.media_hint.text() == "0 matching clips"
    window.media_search.clear()
    window._filter_media_tree()
    assert not hidden.isHidden()
    assert len(window.project_store.media) == 2
    window.close()
    app.processEvents()


def test_split_process_output_retains_progress_and_unicode(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = MainWindow(tmp_path / "project.json")
    chunks = iter([b"progress fra", b"mes 2/10\n" + "촬영".encode()[:2], "촬영".encode()[2:]])
    window.process = SimpleNamespace(readAllStandardOutput=lambda: next(chunks))
    window._read_process()
    window._read_process()
    assert window.progress.maximum() == 10
    assert window.progress.value() == 2
    window._read_process(final=True)
    window._flush_log()
    assert "촬영" in window.log.toPlainText()
    assert "�" not in window.log.toPlainText()
    window.process = None
    window.close()
    app.processEvents()
