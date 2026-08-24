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
