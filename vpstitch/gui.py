from __future__ import annotations

import json
import os
import re
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

import numpy as np
import tifffile
from PySide6.QtCore import QProcess, QSettings, QStandardPaths, Qt, Signal
from PySide6.QtGui import QColor, QCloseEvent, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGraphicsPixmapItem,
    QGraphicsScene,
    QGraphicsView,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSpinBox,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from .config import MAX_CANVAS_HEIGHT, MAX_CANVAS_WIDTH


APP_NAME = "VP Stitch"
BUILTIN_ACES_STUDIO = "ocio://studio-config-v4.0.0_aces-v2.0_ocio-v2.5"
VIDEO_FILTER = "Video files (*.mov *.mp4 *.mkv *.avi *.mxf);;All files (*.*)"


def _runtime_root() -> Path:
    """Return the directory that contains bundled read-only resources."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent.parent / "Resources"))
    return Path(__file__).resolve().parent.parent


def _user_data_root() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(location) if location else Path.home() / "Library" / "Application Support" / "VP-LAB" / APP_NAME


def preview_dimensions(width: int, height: int, max_width: int = 2048, max_height: int = 900) -> tuple[int, int]:
    scale = min(1.0, max_width / width, max_height / height)
    return max(32, int(round(width * scale))), max(32, int(round(height * scale)))


def _display_image(array: np.ndarray) -> QImage:
    image = np.asarray(array[..., :3])
    if image.dtype == np.uint16:
        rgb8 = np.right_shift(image, 8).astype(np.uint8)
    elif image.dtype == np.uint8:
        rgb8 = image
    else:
        values = np.nan_to_num(image.astype(np.float32), nan=0.0, posinf=1.0, neginf=0.0)
        # Display-only exposure mapping. It never touches the render pipeline.
        values = np.clip(values, 0.0, None)
        values = values / (1.0 + values)
        rgb8 = np.rint(np.clip(values, 0.0, 1.0) * 255.0).astype(np.uint8)
    rgb8 = np.ascontiguousarray(rgb8)
    height, width, _ = rgb8.shape
    return QImage(rgb8.data, width, height, rgb8.strides[0], QImage.Format.Format_RGB888).copy()


class PreviewView(QGraphicsView):
    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self._item: QGraphicsPixmapItem | None = None
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor("#090c11"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty = QLabel("5개 영상을 넣고  PREVIEW  를 누르세요")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color:#7e8793; font-size:13px; letter-spacing:.5px;")
        self._empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._empty.setParent(self.viewport())

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._empty.setGeometry(self.viewport().rect())
        if self._item is not None:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def set_array(self, array: np.ndarray) -> None:
        pixmap = QPixmap.fromImage(_display_image(array))
        self.scene().clear()
        self._item = self.scene().addPixmap(pixmap)
        self.scene().setSceneRect(self._item.boundingRect())
        self._empty.hide()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def show_message(self, message: str) -> None:
        self.scene().clear()
        self._item = None
        self._empty.setText(message)
        self._empty.show()


class TrimRangeBar(QWidget):
    """Compact dual-handle frame range control for the aligned timeline."""

    rangeChanged = Signal(int, int)

    def __init__(self) -> None:
        super().__init__()
        self._maximum = 1
        self._lower = 0
        self._upper = 1
        self._active: str | None = None
        self.setMinimumHeight(32)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_frame_range(self, maximum: int, lower: int = 0, upper: int | None = None) -> None:
        self._maximum = max(1, int(maximum))
        self._lower = max(0, min(int(lower), self._maximum - 1))
        requested_upper = self._maximum if upper is None else int(upper)
        self._upper = max(self._lower + 1, min(requested_upper, self._maximum))
        self.update()

    def values(self) -> tuple[int, int]:
        return self._lower, self._upper

    def _track_bounds(self) -> tuple[float, float, float]:
        left = 14.0
        right = max(left + 1.0, float(self.width()) - 14.0)
        return left, right, float(self.height()) / 2.0

    def _position(self, value: int) -> float:
        left, right, _ = self._track_bounds()
        return left + (right - left) * value / self._maximum

    def _value(self, position: float) -> int:
        left, right, _ = self._track_bounds()
        ratio = min(1.0, max(0.0, (position - left) / (right - left)))
        return int(round(ratio * self._maximum))

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        left, right, center = self._track_bounds()
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#343b45") if self.isEnabled() else QColor("#454c56"))
        painter.drawRoundedRect(left, center - 3.0, right - left, 6.0, 3.0, 3.0)
        lower_x = self._position(self._lower)
        upper_x = self._position(self._upper)
        painter.setBrush(QColor("#5f5b83") if self.isEnabled() else QColor("#4a4957"))
        painter.drawRoundedRect(lower_x, center - 4.0, upper_x - lower_x, 8.0, 4.0, 4.0)
        for position in (lower_x, upper_x):
            painter.setBrush(QColor("#f3efff") if self.isEnabled() else QColor("#8a919b"))
            painter.drawEllipse(position - 7.0, center - 7.0, 14.0, 14.0)
            painter.setBrush(QColor("#5f5b83") if self.isEnabled() else QColor("#525761"))
            painter.drawEllipse(position - 3.0, center - 3.0, 6.0, 6.0)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self.isEnabled():
            return
        position = event.position().x()
        self._active = (
            "lower"
            if abs(position - self._position(self._lower))
            <= abs(position - self._position(self._upper))
            else "upper"
        )
        self._move_active(position)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._active:
            self._move_active(event.position().x())

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        self._active = None

    def _move_active(self, position: float) -> None:
        value = self._value(position)
        if self._active == "lower":
            self._lower = min(max(0, value), self._upper - 1)
        elif self._active == "upper":
            self._upper = max(self._lower + 1, min(value, self._maximum))
        self.update()
        self.rangeChanged.emit(self._lower, self._upper)


class SourceTable(QTableWidget):
    def __init__(self) -> None:
        super().__init__(5, 9)
        self.setHorizontalHeaderLabels(
            ["CAM", "CLIP", "TC IN", "FRAMES", "STATUS", "YAW", "PITCH", "ROLL", "OFFSET"]
        )
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setAlternatingRowColors(False)
        self.setShowGrid(False)
        self.setMinimumHeight(212)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        header = self.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.ResizeToContents)
        for column in (3, 5, 6, 7, 8):
            self.setColumnHidden(column, True)
        self.setColumnWidth(1, 150)
        self.verticalHeader().setDefaultSectionSize(32)

    def set_rig(self, cameras: list[dict[str, object]], paths: list[str] | None = None) -> None:
        paths = paths or [""] * len(cameras)
        self.setRowCount(len(cameras))
        for row, camera in enumerate(cameras):
            values = [
                str(camera.get("name", f"cam{row}")),
                paths[row] if row < len(paths) else "",
                "—",
                "—",
                "—",
                str(camera.get("yaw_deg", 0.0)),
                str(camera.get("pitch_deg", 0.0)),
                str(camera.get("roll_deg", 0.0)),
                str(camera.get("frame_offset", 0)),
            ]
            for column, value in enumerate(values):
                display_value = Path(value).name if column == 1 and value else value
                item = QTableWidgetItem(display_value)
                if column in {0, 1, 2, 3, 4}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 1:
                    item.setToolTip(value)
                    item.setData(Qt.ItemDataRole.UserRole, value)
                self.setItem(row, column, item)

    def paths(self) -> list[str]:
        paths: list[str] = []
        for row in range(self.rowCount()):
            item = self.item(row, 1)
            if item is None:
                paths.append("")
                continue
            stored = item.data(Qt.ItemDataRole.UserRole)
            paths.append(str(stored).strip() if stored is not None else item.text().strip())
        return paths

    def set_paths(self, paths: list[str]) -> None:
        for row, path in enumerate(paths[: self.rowCount()]):
            item = self.item(row, 1)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, 1, item)
            item.setText(Path(path).name if path else "")
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)

    def clear_timing(self) -> None:
        for row in range(self.rowCount()):
            for column in (2, 3, 4):
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(row, column, item)
                item.setText("—")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def set_timing(self, inputs: list[dict[str, object]]) -> None:
        if len(inputs) != self.rowCount():
            raise ValueError("timecode result does not match the source count")
        for row, timing in enumerate(inputs):
            for column, value in (
                (2, timing.get("timecode", "—")),
                (3, timing.get("frame_count", "—")),
                (
                    4,
                    "SYNCED"
                    if int(timing.get("skip_frames", 0)) == 0
                    else f"SKIP {timing.get('skip_frames')}",
                ),
            ):
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(row, column, item)
                item.setText(str(value))
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def offsets(self) -> list[int]:
        try:
            return [int(self.item(row, 8).text()) for row in range(self.rowCount())]
        except (AttributeError, ValueError) as error:
            raise ValueError("camera OFFSET values must be whole frames") from error

    def apply_to_cameras(self, cameras: list[dict[str, object]]) -> None:
        for row, camera in enumerate(cameras):
            try:
                camera["yaw_deg"] = float(self.item(row, 5).text())
                camera["pitch_deg"] = float(self.item(row, 6).text())
                camera["roll_deg"] = float(self.item(row, 7).text())
                camera["frame_offset"] = int(self.item(row, 8).text())
            except (AttributeError, ValueError) as error:
                raise ValueError(f"Camera {row + 1} orientation/offset is invalid") from error


class MainWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  —  5-Camera 180°")
        self.resize(1600, 960)
        self.setMinimumSize(1180, 720)
        self.setAcceptDrops(True)
        self.settings = QSettings("VP-LAB", APP_NAME)
        self.runtime_root = _runtime_root()
        self.project_root = self.runtime_root if getattr(sys, "frozen", False) else Path.cwd()
        self.user_data_root = _user_data_root() if getattr(sys, "frozen", False) else self.project_root / ".vpstitch-ui"
        self.user_data_root.mkdir(parents=True, exist_ok=True)
        self.config_path: Path | None = None
        self.config_data: dict[str, object] = {}
        self.process: QProcess | None = None
        self._process_success: Callable[[], None] | None = None
        self._process_output = ""
        self._last_reference_dir: Path | None = None
        self._tc_alignment: dict[str, object] | None = None
        self._tc_alignment_path: Path | None = None
        self._timeline_maximum = 1
        self._configured_frame_limit = 0
        self._timeline_updating = False
        self._working_dir = self.user_data_root / "work"
        self._cache_dir = self.user_data_root / "cache"
        self._output_root = self.user_data_root / "renders"
        self._working_dir.mkdir(parents=True, exist_ok=True)
        self._cache_dir.mkdir(parents=True, exist_ok=True)
        self._output_root.mkdir(parents=True, exist_ok=True)
        self._build_ui()
        self._apply_style()
        initial = Path(str(self.settings.value("lastConfig", "")))
        if not initial.is_file():
            initial = self.project_root / "configs" / "drive_5cam_180.prores-hq.json"
        if not initial.is_file():
            initial = self.project_root / "configs" / "five_cam_180.sample.json"
        if initial.is_file():
            self.load_config(initial)
        self.statusBar().showMessage("Ready · preview is display-only; final render stays high bit depth")

    def _build_ui(self) -> None:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar.setFixedHeight(46)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(14, 0, 10, 0)
        top_layout.setSpacing(8)
        app_title = QLabel("VP Stitch")
        app_title.setObjectName("appTitle")
        top_layout.addWidget(app_title)
        app_subtitle = QLabel("5-CAMERA 180° PANORAMA")
        app_subtitle.setObjectName("appSubtitle")
        top_layout.addWidget(app_subtitle)
        top_layout.addStretch()
        self.profile_label = QLabel("Rig Profile · Loading…")
        self.profile_label.setObjectName("profileLabel")
        self.profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self.profile_label)
        top_layout.addStretch()
        self.status_pill = QLabel("READY")
        self.status_pill.setObjectName("statusPill")
        self.status_pill.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self.status_pill)
        profile_button = QPushButton("PROFILE…")
        profile_button.setObjectName("topButton")
        profile_button.setToolTip("Open another calibrated rig profile")
        profile_button.clicked.connect(self.choose_config)
        top_layout.addWidget(profile_button)
        self.inspector_toggle = QPushButton("INSPECTOR")
        self.inspector_toggle.setObjectName("topButton")
        self.inspector_toggle.setCheckable(True)
        self.inspector_toggle.setChecked(True)
        self.inspector_toggle.clicked.connect(self._toggle_inspector)
        top_layout.addWidget(self.inspector_toggle)
        self.jobs_toggle = QPushButton("JOBS")
        self.jobs_toggle.setObjectName("topButton")
        self.jobs_toggle.setCheckable(True)
        self.jobs_toggle.clicked.connect(self._toggle_log)
        top_layout.addWidget(self.jobs_toggle)

        self.source_table = SourceTable()
        self.source_table.itemChanged.connect(self._source_item_changed)
        source_group = QFrame()
        source_group.setObjectName("mediaPanel")
        source_group.setMinimumWidth(250)
        source_group.setMaximumWidth(330)
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(12, 10, 12, 10)
        source_layout.setSpacing(7)
        media_title = QLabel("MEDIA POOL")
        media_title.setProperty("sectionTitle", True)
        source_layout.addWidget(media_title)
        media_hint = QLabel("Import five camera plates in left → right order")
        media_hint.setWordWrap(True)
        media_hint.setProperty("muted", True)
        source_layout.addWidget(media_hint)
        source_layout.addWidget(self.source_table)
        source_buttons = QHBoxLayout()
        choose = QPushButton("IMPORT 5 PLATES")
        choose.setObjectName("primaryButton")
        choose.clicked.connect(self.choose_videos)
        clear = QPushButton("CLEAR")
        clear.setObjectName("secondaryButton")
        clear.clicked.connect(self.clear_sources)
        source_buttons.addWidget(choose, 1)
        source_buttons.addWidget(clear)
        source_layout.addLayout(source_buttons)
        source_layout.addStretch()
        self.source_status = QLabel("Drop five clips here · No clips loaded")
        self.source_status.setObjectName("sourceStatus")
        self.source_status.setWordWrap(True)
        source_layout.addWidget(self.source_status)

        self.preview = PreviewView()
        preview_box = QFrame()
        preview_box.setObjectName("previewPanel")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(12, 10, 12, 7)
        preview_layout.setSpacing(7)
        preview_header = QHBoxLayout()
        title = QLabel("PANORAMA PREVIEW")
        title.setProperty("sectionTitle", True)
        self.preview_time = QDoubleSpinBox()
        self.preview_time.setRange(0.0, 86400.0)
        self.preview_time.setDecimals(3)
        self.preview_time.setSuffix(" sec")
        self.preview_time.setValue(0.0)
        preview_header.addWidget(title)
        preview_header.addStretch()
        preview_header.addWidget(QLabel("REFERENCE TIME"))
        preview_header.addWidget(self.preview_time)
        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.preview, 1)
        self.preview_note = QLabel("Display preview · master render remains high bit depth")
        self.preview_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_note.setProperty("muted", True)
        preview_layout.addWidget(self.preview_note)

        settings_scroll = QScrollArea()
        settings_scroll.setObjectName("settingsPanel")
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumWidth(286)
        settings_scroll.setMaximumWidth(350)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        tabs = QTabWidget()
        tabs.setMinimumWidth(0)
        tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        tabs.addTab(self._stitch_settings(), "RIG")
        tabs.addTab(self._color_settings(), "COLOR")
        tabs.addTab(self._output_settings(), "DELIVER")
        settings_scroll.setWidget(tabs)
        self.inspector_panel = QFrame()
        self.inspector_panel.setObjectName("inspectorPanel")
        self.inspector_panel.setMinimumWidth(310)
        self.inspector_panel.setMaximumWidth(350)
        inspector_layout = QVBoxLayout(self.inspector_panel)
        inspector_layout.setContentsMargins(10, 9, 10, 9)
        inspector_layout.setSpacing(6)
        inspector_title = QLabel("INSPECTOR")
        inspector_title.setProperty("sectionTitle", True)
        inspector_layout.addWidget(inspector_title)
        inspector_layout.addWidget(settings_scroll, 1)

        upper = QSplitter(Qt.Orientation.Horizontal)
        upper.setObjectName("workspaceSplitter")
        upper.setChildrenCollapsible(False)
        upper.setHandleWidth(1)
        upper.addWidget(source_group)
        upper.addWidget(preview_box)
        upper.addWidget(self.inspector_panel)
        upper.setSizes([280, 960, 310])

        timing_panel = QFrame()
        timing_panel.setObjectName("timingPanel")
        timing_layout = QVBoxLayout(timing_panel)
        timing_layout.setContentsMargins(12, 7, 12, 7)
        timing_layout.setSpacing(2)
        timing_header = QHBoxLayout()
        timing_title = QLabel("SHARED TIMELINE")
        timing_title.setProperty("sectionTitle", True)
        self.timing_status = QLabel("TC Align finds the shortest common range across all five cameras")
        self.timing_status.setProperty("muted", True)
        timing_header.addWidget(timing_title)
        timing_header.addSpacing(12)
        timing_header.addWidget(self.timing_status)
        timing_header.addStretch()
        self.timeline_duration = QLabel("0 frames")
        self.timeline_duration.setObjectName("durationBadge")
        timing_header.addWidget(self.timeline_duration)
        timing_layout.addLayout(timing_header)
        self.timeline_bar = TrimRangeBar()
        self.timeline_bar.setEnabled(False)
        self.timeline_bar.rangeChanged.connect(self._timeline_bar_changed)
        timing_layout.addWidget(self.timeline_bar)
        timing_values = QHBoxLayout()
        self.timeline_in = QSpinBox()
        self.timeline_out = QSpinBox()
        for widget in (self.timeline_in, self.timeline_out):
            widget.setRange(0, 10_000_000)
            widget.setEnabled(False)
            widget.valueChanged.connect(self._timeline_spin_changed)
        reset_timeline = QPushButton("RESET RANGE")
        reset_timeline.setObjectName("quietButton")
        reset_timeline.clicked.connect(self._reset_timeline_range)
        timing_values.addWidget(QLabel("IN"))
        timing_values.addWidget(self.timeline_in)
        timing_values.addSpacing(10)
        timing_values.addWidget(QLabel("OUT"))
        timing_values.addWidget(self.timeline_out)
        timing_values.addStretch()
        timing_values.addWidget(reset_timeline)
        timing_layout.addLayout(timing_values)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(10, 6, 10, 6)
        action_layout.setSpacing(6)

        def workflow_button(text: str, callback: Callable[[], None], step: str) -> QPushButton:
            button = QPushButton(f"{step}   {text}")
            button.setObjectName("workflowButton")
            button.setMinimumSize(128, 34)
            button.setMaximumWidth(168)
            button.clicked.connect(callback)
            action_layout.addWidget(button)
            return button

        self.tc_align_button = workflow_button("TC ALIGN", self.align_timecode, "1")
        self.preview_button = workflow_button("PREVIEW", self.create_preview, "2")
        self.rig_align_button = workflow_button("RIG ALIGN", self.auto_align, "3")
        self.rig_align_button.setEnabled(False)
        self.rig_align_button.setToolTip("Create a preview first. Rig Align then adjusts camera geometry and refreshes it.")
        action_layout.addStretch()
        self.render_button = workflow_button("RENDER", self.render, "4")
        self.render_button.setObjectName("renderButton")
        self.render_button.setMinimumWidth(156)
        self.cancel_button = QPushButton("CANCEL")
        self.cancel_button.setObjectName("cancelButton")
        self.cancel_button.clicked.connect(self.cancel_task)
        self.cancel_button.setVisible(False)
        action_layout.addWidget(self.cancel_button)

        self.log = QPlainTextEdit()
        self.log.setReadOnly(True)
        self.log.setMaximumBlockCount(5000)
        self.log.setMinimumHeight(130)
        self.log.setFont(QFont("Cascadia Mono", 9))
        self.log_box = QFrame()
        self.log_box.setObjectName("logPanel")
        log_layout = QVBoxLayout(self.log_box)
        log_layout.setContentsMargins(14, 10, 14, 10)
        log_title = QLabel("JOBS / TASK LOG")
        log_title.setProperty("sectionTitle", True)
        log_layout.addWidget(log_title)
        log_layout.addWidget(self.log)
        self.log_box.setVisible(False)

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(8, 8, 8, 7)
        workspace_layout.setSpacing(6)
        workspace_layout.addWidget(upper, 1)
        workspace_layout.addWidget(timing_panel)
        workspace_layout.addWidget(action_bar)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(workspace, 1)
        central_layout.addWidget(self.log_box)
        self.setCentralWidget(central)

        status = QStatusBar()
        self.task_label = QLabel("IDLE")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(180)
        status.addPermanentWidget(self.task_label)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)

    def _toggle_inspector(self, checked: bool) -> None:
        self.inspector_panel.setVisible(checked)
        self.inspector_toggle.setText("INSPECTOR" if checked else "SHOW INSPECTOR")

    def _toggle_log(self, checked: bool) -> None:
        self.log_box.setVisible(checked)
        self.jobs_toggle.setText("HIDE JOBS" if checked else "JOBS")

    def _update_source_status(self) -> None:
        loaded = sum(bool(path) for path in self.source_table.paths())
        if loaded == self.source_table.rowCount():
            self.source_status.setText("●  5 of 5 plates ready · ordered left → right")
        elif loaded:
            self.source_status.setText(f"●  {loaded} of 5 plates loaded")
        else:
            self.source_status.setText("Drop five clips here · No clips loaded")

    def _stitch_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        profile = QFrame()
        profile.setObjectName("inspectorSection")
        profile_layout = QVBoxLayout(profile)
        profile_layout.setContentsMargins(0, 4, 0, 9)
        profile_layout.setSpacing(5)
        profile_title = QLabel("RIG PROFILE")
        profile_title.setProperty("inspectorTitle", True)
        profile_layout.addWidget(profile_title)
        profile_note = QLabel(
            "Drive 5-Cam loads automatically. It stores lens calibration, camera angles, "
            "and the 180° output layout."
        )
        profile_note.setWordWrap(True)
        profile_note.setProperty("muted", True)
        profile_layout.addWidget(profile_note)
        align_note = QLabel(
            "Rig Align corrects camera rotation only, then refreshes Preview."
        )
        align_note.setWordWrap(True)
        profile_layout.addWidget(align_note)
        profile_actions = QHBoxLayout()
        open_profile = QPushButton("OPEN…")
        open_profile.setObjectName("secondaryButton")
        open_profile.setToolTip("Open another calibrated rig profile")
        open_profile.clicked.connect(self.choose_config)
        save_profile = QPushButton("SAVE AS…")
        save_profile.setObjectName("secondaryButton")
        save_profile.setToolTip("Save the current rig profile")
        save_profile.clicked.connect(self.save_as)
        profile_actions.addWidget(open_profile)
        profile_actions.addWidget(save_profile)
        profile_layout.addLayout(profile_actions)
        layout.addWidget(profile)
        canvas = QGroupBox("CANVAS")
        form = QFormLayout(canvas)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(5)
        self.canvas_width = QSpinBox()
        self.canvas_width.setRange(32, MAX_CANVAS_WIDTH)
        self.canvas_width.setSingleStep(256)
        self.canvas_height = QSpinBox()
        self.canvas_height.setRange(32, MAX_CANVAS_HEIGHT)
        self.canvas_height.setSingleStep(128)
        self.h_fov = QDoubleSpinBox()
        self.h_fov.setRange(1.0, 360.0)
        self.h_fov.setSuffix("°")
        self.v_fov = QDoubleSpinBox()
        self.v_fov.setRange(1.0, 179.0)
        self.v_fov.setSuffix("°")
        self.center_yaw = QDoubleSpinBox()
        self.center_yaw.setRange(-180.0, 180.0)
        self.center_yaw.setSuffix("°")
        self.center_pitch = QDoubleSpinBox()
        self.center_pitch.setRange(-89.0, 89.0)
        self.center_pitch.setSuffix("°")
        self.seam_feather = QDoubleSpinBox()
        self.seam_feather.setRange(0.1, 30.0)
        self.seam_feather.setDecimals(2)
        self.seam_feather.setSuffix("°")
        for label, widget in [
            ("Width", self.canvas_width),
            ("Height", self.canvas_height),
            ("Horizontal FOV", self.h_fov),
            ("Vertical FOV", self.v_fov),
            ("Center yaw", self.center_yaw),
            ("Center pitch", self.center_pitch),
            ("Seam feather", self.seam_feather),
        ]:
            form.addRow(label, widget)
        layout.addWidget(canvas)

        flow = QGroupBox("PARALLAX REFINEMENT")
        flow_form = QFormLayout(flow)
        flow_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        flow_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        flow_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        flow_form.setHorizontalSpacing(8)
        flow_form.setVerticalSpacing(5)
        self.flow_enabled = QCheckBox("Enable DIS optical flow")
        self.flow_preset = QComboBox()
        self.flow_preset.addItems(["ultrafast", "fast", "medium"])
        self.flow_max = QDoubleSpinBox()
        self.flow_max.setRange(1.0, 256.0)
        self.flow_max.setSuffix(" px")
        flow_form.addRow(self.flow_enabled)
        flow_form.addRow("Quality", self.flow_preset)
        flow_form.addRow("Max displacement", self.flow_max)
        layout.addWidget(flow)

        analyze = QPushButton("ANALYZE COVERAGE / CROP")
        analyze.setObjectName("secondaryButton")
        analyze.clicked.connect(self.analyze_coverage)
        layout.addWidget(analyze)
        layout.addStretch()
        return panel

    def _color_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        color = QGroupBox("COLOR PIPELINE")
        form = QFormLayout(color)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(5)
        self.color_mode = QComboBox()
        self.color_mode.addItem("Passthrough original code space", "passthrough")
        self.color_mode.addItem("OCIO managed", "ocio")
        self.color_mode.currentIndexChanged.connect(self._update_color_controls)
        self.ocio_config = QLineEdit()
        ocio_row = QWidget()
        ocio_layout = QHBoxLayout(ocio_row)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.addWidget(self.ocio_config)
        ocio_button = QPushButton("…")
        ocio_button.setObjectName("iconButton")
        ocio_button.setFixedWidth(34)
        ocio_button.clicked.connect(self.choose_ocio)
        ocio_layout.addWidget(ocio_button)
        aces_button = QPushButton("USE BUILT-IN ACES 2.0 / REC.709")
        aces_button.setObjectName("secondaryButton")
        aces_button.clicked.connect(self.apply_aces_preset)
        self.input_space = QLineEdit()
        self.working_space = QLineEdit()
        self.output_space = QLineEdit()
        self.integer_dither = QCheckBox("TPDF dither when writing integer masters")
        form.addRow("Mode", self.color_mode)
        form.addRow("OCIO config", ocio_row)
        form.addRow("All camera inputs", self.input_space)
        form.addRow("Working space", self.working_space)
        form.addRow("Output space", self.output_space)
        form.addRow(self.integer_dither)
        form.addRow(aces_button)
        layout.addWidget(color)
        note = QLabel(
            "OCIO 모드는 리샘플링 전에 모든 카메라를 scene-linear 작업공간으로 변환합니다. "
            "ACEScg 출력은 EXR half-float를 선택하세요."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        layout.addStretch()
        return panel

    def _output_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        output = QGroupBox("MASTER OUTPUT")
        form = QFormLayout(output)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(5)
        self.output_codec = QComboBox()
        self.output_codec.addItem("ProRes HQ · 10-bit 4:2:2", "prores-hq")
        self.output_codec.addItem("DPX · 12-bit RGB sequence", "dpx12-sequence")
        self.output_codec.addItem("H.264 MP4 · 10-bit 4:2:0", "h264-mp4-10")
        self.output_codec.addItem("ProRes 4444 · 10-bit YUV", "prores-4444")
        self.output_codec.addItem("FFV1 · 16-bit RGB lossless", "ffv1-16")
        self.output_codec.addItem("OpenEXR · half-float sequence", "exr-half-sequence")
        self.output_codec.addItem("BigTIFF · 16-bit RGB sequence", "tiff16-sequence")
        self.output_codec.addItem("HEVC · 10-bit 4:4:4", "hevc-444-10")
        self.output_codec.currentIndexChanged.connect(self._update_output_hint)
        self.output_path = QLineEdit()
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(0, 0, 0, 0)
        path_layout.addWidget(self.output_path)
        path_button = QPushButton("…")
        path_button.setObjectName("iconButton")
        path_button.setFixedWidth(34)
        path_button.clicked.connect(self.choose_output)
        path_layout.addWidget(path_button)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(1.0, 240.0)
        self.fps.setDecimals(3)
        self.frame_limit = QSpinBox()
        self.frame_limit.setRange(0, 10_000_000)
        self.frame_limit.setSpecialValueText("FULL CLIP")
        form.addRow("Codec", self.output_codec)
        form.addRow("Destination", path_row)
        form.addRow("FPS", self.fps)
        form.addRow("Frame limit", self.frame_limit)
        layout.addWidget(output)
        self.output_hint = QLabel()
        self.output_hint.setWordWrap(True)
        self.output_hint.setProperty("muted", True)
        layout.addWidget(self.output_hint)
        resources = QPushButton("ESTIMATE 20K RESOURCES")
        resources.setObjectName("secondaryButton")
        resources.clicked.connect(self.estimate_resources)
        layout.addWidget(resources)
        layout.addStretch()
        return panel

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background:#0d1014;
                color:#e6e9ee;
                font-family:'-apple-system','SF Pro Text','Malgun Gothic','Segoe UI';
                font-size:11px;
            }
            QLabel { background:transparent; }
            QFrame#topBar { background:#15191e; border-bottom:1px solid #2a3038; }
            QLabel#appTitle { color:#f3f4f6; font-size:15px; font-weight:750; }
            QLabel#appSubtitle { color:#747c86; font-size:9px; letter-spacing:.8px; }
            QLabel#profileLabel { color:#aeb5be; font-size:10px; }
            QLabel#statusPill {
                color:#74d89a;
                border:1px solid #315b41;
                border-radius:4px;
                padding:3px 7px;
                font-size:8px;
                font-weight:800;
                letter-spacing:1px;
            }
            QFrame#mediaPanel, QFrame#inspectorPanel, QFrame#previewPanel,
            QFrame#timingPanel, QFrame#actionBar, QFrame#logPanel {
                background:#15191e;
                border:1px solid #2a3038;
                border-radius:4px;
            }
            QFrame#previewPanel { background:#111419; }
            QFrame#inspectorSection { background:transparent; border:0; border-bottom:1px solid #2a3038; }
            QGroupBox {
                background:transparent;
                border:0;
                border-top:1px solid #2a3038;
                border-radius:0;
                margin-top:10px;
                padding:12px 2px 5px;
                color:#aeb5bf;
                font-weight:650;
            }
            QGroupBox::title { subcontrol-origin:margin; left:2px; padding:0 4px; color:#aeb5bf; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTableWidget {
                background:#101318;
                border:1px solid #303740;
                border-radius:3px;
                padding:4px;
                selection-background-color:#5b577f;
                selection-color:#ffffff;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color:#777199; }
            QPushButton {
                background:#20252c;
                color:#d1d6dd;
                border:1px solid #373e48;
                border-radius:3px;
                padding:5px 8px;
                font-weight:650;
            }
            QPushButton:hover { background:#292f37; border-color:#666f7d; color:#ffffff; }
            QPushButton:pressed { background:#181c21; }
            QPushButton:checked { color:#ffffff; border-color:#6c678e; background:#24232e; }
            QPushButton:disabled { color:#626a75; background:#171b20; border-color:#282e35; }
            QPushButton#topButton { background:transparent; padding:4px 7px; color:#aab1bb; }
            QPushButton#primaryButton { background:#5f5b83; color:#ffffff; border-color:#777199; }
            QPushButton#primaryButton:hover { background:#6b668f; }
            QPushButton#secondaryButton, QPushButton#quietButton { background:#1c2127; color:#b9c0ca; }
            QPushButton#quietButton { padding:6px 10px; }
            QPushButton#iconButton { padding:5px 9px; min-width:28px; }
            QPushButton#workflowButton {
                background:#191d23;
                color:#cbd0d7;
                border-color:#343b44;
                font-size:10px;
                letter-spacing:.3px;
            }
            QPushButton#workflowButton:hover { border-color:#6d688c; background:#23232b; }
            QPushButton#renderButton { background:#5f5b83; color:#ffffff; border-color:#777199; }
            QPushButton#renderButton:hover { background:#6b668f; }
            QPushButton#cancelButton { color:#e4a2b9; border-color:#694050; max-width:90px; }
            QHeaderView::section {
                background:#15191e;
                color:#858d98;
                border:0;
                border-bottom:1px solid #2c323a;
                padding:5px 4px;
                font-size:9px;
                font-weight:700;
            }
            QTableWidget { border:0; background:#15191e; }
            QTableWidget::item { border-bottom:1px solid #252b32; padding:3px; }
            QTableWidget::item:selected { background:#292833; color:#ffffff; }
            QTabWidget::pane { border:0; }
            QTabBar::tab {
                background:#15191e;
                color:#777f8b;
                border:0;
                border-bottom:2px solid transparent;
                padding:7px 11px;
                font-weight:700;
            }
            QTabBar::tab:selected { color:#efedf7; border-bottom:2px solid #6d688c; }
            QLabel#durationBadge { color:#aaa5c8; padding:2px 5px; font-weight:700; }
            QLabel#sourceStatus { color:#858d98; font-size:10px; }
            QScrollArea { border:0; background:transparent; }
            QSplitter#workspaceSplitter::handle { background:#2a3038; }
            QProgressBar {
                border:1px solid #303740;
                border-radius:4px;
                background:#101318;
                text-align:center;
                color:#aeb5bf;
            }
            QProgressBar::chunk { background:#5f5b83; border-radius:3px; }
            QLabel[muted='true'] { color:#7d8590; }
            QLabel[sectionTitle='true'] { color:#eef0f3; font-size:11px; font-weight:800; letter-spacing:.7px; }
            QLabel[inspectorTitle='true'] { color:#b9c0c9; font-size:10px; font-weight:800; letter-spacing:.6px; }
            QStatusBar { background:#15191e; border-top:1px solid #2a3038; color:#858d98; }
            """
        )

    def load_config(self, path: Path) -> None:
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            cameras = raw.get("cameras")
            if not isinstance(cameras, list) or not cameras:
                raise ValueError("config has no cameras")
        except Exception as error:
            self._error("Rig profile error", str(error))
            return
        self.config_path = path
        self.config_data = raw
        self._tc_alignment = None
        self._tc_alignment_path = None
        self.source_table.blockSignals(True)
        self.source_table.set_rig(cameras)
        self.source_table.blockSignals(False)
        output = raw.setdefault("output", {})
        self.canvas_width.setValue(int(output.get("width", 15360)))
        self.canvas_height.setValue(int(output.get("height", 3968)))
        self.h_fov.setValue(float(output.get("horizontal_fov_deg", 180.0)))
        self.v_fov.setValue(float(output.get("vertical_fov_deg", 52.0)))
        self.center_yaw.setValue(float(output.get("center_yaw_deg", 0.0)))
        self.center_pitch.setValue(float(output.get("center_pitch_deg", 0.0)))
        self.seam_feather.setValue(float(output.get("seam_feather_deg", 4.0)))
        flow = raw.setdefault("flow", {})
        self.flow_enabled.setChecked(bool(flow.get("enabled", False)))
        self.flow_preset.setCurrentText(str(flow.get("preset", "medium")))
        self.flow_max.setValue(float(flow.get("max_displacement_px", 32.0)))
        color = raw.setdefault("color", {})
        mode = str(color.get("mode", "passthrough"))
        self.color_mode.setCurrentIndex(max(0, self.color_mode.findData(mode)))
        self.ocio_config.setText(str(color.get("ocio_config") or ""))
        camera_space = next((str(camera.get("colorspace")) for camera in cameras if camera.get("colorspace")), "")
        self.input_space.setText(camera_space)
        self.working_space.setText(str(color.get("working_space") or ""))
        self.output_space.setText(str(color.get("output_space") or ""))
        self.integer_dither.setChecked(bool(color.get("integer_dither", True)))
        video = raw.setdefault("video", {"fps": 29.97})
        codec = str(video.get("output_codec", "ffv1-16"))
        self.output_codec.setCurrentIndex(max(0, self.output_codec.findData(codec)))
        self.fps.setValue(float(video.get("fps", 29.97)))
        self._configured_frame_limit = int(video.get("frames") or 0)
        self.frame_limit.setValue(self._configured_frame_limit)
        self._reset_timing()
        self.settings.setValue("lastConfig", str(path))
        is_builtin = path.name.startswith("drive_5cam_180") or path.parent == self.project_root / "configs"
        profile_name = "Drive 5-Cam · Auto Profile" if is_builtin else f"{path.stem} · Custom Profile"
        self.profile_label.setText(profile_name)
        self.profile_label.setToolTip(str(path))
        self.setWindowTitle(f"{APP_NAME}  —  5-Camera 180°")
        self._update_color_controls()
        self._update_output_hint()
        self._append_log(f"Loaded rig profile: {path}")

    def _collect_config(self) -> dict[str, object]:
        if not self.config_data:
            raise ValueError("Load a rig profile first")
        raw = json.loads(json.dumps(self.config_data))
        cameras = raw["cameras"]
        self.source_table.apply_to_cameras(cameras)
        output = raw.setdefault("output", {})
        output.update(
            {
                "width": self.canvas_width.value(),
                "height": self.canvas_height.value(),
                "horizontal_fov_deg": self.h_fov.value(),
                "vertical_fov_deg": self.v_fov.value(),
                "center_yaw_deg": self.center_yaw.value(),
                "center_pitch_deg": self.center_pitch.value(),
                "seam_feather_deg": self.seam_feather.value(),
            }
        )
        flow = raw.setdefault("flow", {})
        flow.update(
            {
                "enabled": self.flow_enabled.isChecked(),
                "algorithm": "dis",
                "preset": self.flow_preset.currentText(),
                "max_displacement_px": self.flow_max.value(),
            }
        )
        mode = str(self.color_mode.currentData())
        if mode == "passthrough":
            raw["color"] = {
                "mode": "passthrough",
                "integer_dither": self.integer_dither.isChecked(),
                "dither_seed": int(raw.get("color", {}).get("dither_seed", 7349)),
            }
            for camera in cameras:
                camera.pop("colorspace", None)
        else:
            required = [self.ocio_config.text(), self.input_space.text(), self.working_space.text(), self.output_space.text()]
            if not all(value.strip() for value in required):
                raise ValueError("OCIO config and all three colorspace fields are required")
            raw["color"] = {
                "mode": "ocio",
                "ocio_config": self.ocio_config.text().strip(),
                "working_space": self.working_space.text().strip(),
                "output_space": self.output_space.text().strip(),
                "integer_dither": self.integer_dither.isChecked(),
                "dither_seed": int(raw.get("color", {}).get("dither_seed", 7349)),
            }
            for camera in cameras:
                camera["colorspace"] = self.input_space.text().strip()
        video = raw.setdefault("video", {})
        video.update(
            {
                "fps": self.fps.value(),
                "frames": self.frame_limit.value() or None,
                "output_codec": str(self.output_codec.currentData()),
            }
        )
        return raw

    def _write_working_config(self) -> Path:
        raw = self._collect_config()
        path = self._working_dir / "working-config.json"
        path.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return path

    def choose_config(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open rig profile", str(self.project_root), "JSON (*.json)")
        if path:
            self.load_config(Path(path))

    def save_as(self) -> None:
        try:
            raw = self._collect_config()
        except Exception as error:
            self._error("Cannot save", str(error))
            return
        path, _ = QFileDialog.getSaveFileName(
            self,
            "Save rig profile",
            str(self.config_path or self.project_root / "configs" / "rig.json"),
            "JSON (*.json)",
        )
        if path:
            destination = Path(path)
            destination.write_text(json.dumps(raw, indent=2), encoding="utf-8")
            self.config_data = raw
            self.config_path = destination
            self.settings.setValue("lastConfig", str(destination))
            self._append_log(f"Saved rig profile: {destination}")

    def choose_videos(self) -> None:
        files, _ = QFileDialog.getOpenFileNames(self, "Select camera videos left to right", str(self.project_root), VIDEO_FILTER)
        if files:
            if len(files) != self.source_table.rowCount():
                self._error("Need five videos", f"Expected {self.source_table.rowCount()} files, selected {len(files)}")
                return
            self.source_table.set_paths(files)
            self._reset_timing()

    def clear_sources(self) -> None:
        self.source_table.set_paths([""] * self.source_table.rowCount())
        self._reset_timing()

    def _reset_timing(self) -> None:
        self._tc_alignment = None
        self._tc_alignment_path = None
        self._last_reference_dir = None
        self._timeline_maximum = 1
        self.source_table.clear_timing()
        self.timeline_bar.setEnabled(False)
        self.timeline_bar.set_frame_range(1)
        self._timeline_updating = True
        self.timeline_in.setRange(0, 1)
        self.timeline_out.setRange(0, 1)
        self.timeline_in.setValue(0)
        self.timeline_out.setValue(1)
        self.timeline_in.setEnabled(False)
        self.timeline_out.setEnabled(False)
        self.frame_limit.setEnabled(True)
        self.frame_limit.setValue(self._configured_frame_limit)
        self.timeline_duration.setText("0 frames")
        self.timing_status.setText("TC Align finds the shortest common range across all five cameras")
        self.rig_align_button.setEnabled(False)
        self.preview_note.setText("Display preview · master render remains high bit depth")
        self._update_source_status()
        self._timeline_updating = False

    def _set_timeline_range(self, lower: int, upper: int) -> None:
        if not self._tc_alignment:
            return
        maximum = self._timeline_maximum
        lower = max(0, min(int(lower), maximum - 1))
        upper = max(lower + 1, min(int(upper), maximum))
        self._timeline_updating = True
        self.timeline_bar.set_frame_range(maximum, lower, upper)
        self.timeline_in.setValue(lower)
        self.timeline_out.setValue(upper)
        duration = upper - lower
        self.frame_limit.setValue(duration)
        fps = float(self._tc_alignment["fps"])
        self.timeline_duration.setText(
            f"{duration:,} frames  ·  {duration / fps:,.2f} sec"
        )
        self._timeline_updating = False

    def _timeline_bar_changed(self, lower: int, upper: int) -> None:
        if not self._timeline_updating:
            self._set_timeline_range(lower, upper)

    def _timeline_spin_changed(self) -> None:
        if not self._timeline_updating:
            self._set_timeline_range(self.timeline_in.value(), self.timeline_out.value())

    def _reset_timeline_range(self) -> None:
        if self._tc_alignment:
            self._set_timeline_range(0, self._timeline_maximum)

    def _effective_common_frames(self, payload: dict[str, object]) -> int:
        inputs = payload.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != self.source_table.rowCount():
            raise ValueError("timecode result does not match the source count")
        offsets = self.source_table.offsets()
        starts = [
            int(item["skip_frames"]) + offset
            for item, offset in zip(inputs, offsets, strict=True)
        ]
        normalization = -min(0, min(starts))
        available = min(
            int(item["frame_count"]) - (start + normalization)
            for item, start in zip(inputs, starts, strict=True)
        )
        if available < 1:
            raise ValueError("camera OFFSET values leave no common aligned range")
        return available

    def _source_item_changed(self, item: QTableWidgetItem) -> None:
        if item.column() != 8 or not self._tc_alignment or self._timeline_updating:
            return
        try:
            lower, upper = self.timeline_bar.values()
            self._timeline_maximum = self._effective_common_frames(self._tc_alignment)
            self._timeline_updating = True
            self.timeline_in.setRange(0, self._timeline_maximum - 1)
            self.timeline_out.setRange(1, self._timeline_maximum)
            self._timeline_updating = False
            self._set_timeline_range(
                min(lower, self._timeline_maximum - 1),
                min(upper, self._timeline_maximum),
            )
        except Exception as error:
            self._timeline_updating = False
            self.timing_status.setText(f"OFFSET ERROR · {error}")

    def _apply_alignment_payload(
        self,
        payload: dict[str, object],
        lower: int = 0,
        upper: int | None = None,
    ) -> None:
        inputs = payload["inputs"]
        common_frames = int(payload["common_frames"])
        if not isinstance(inputs, list) or common_frames < 1:
            raise ValueError("invalid timecode alignment report")
        self._tc_alignment = payload
        self.source_table.set_timing(inputs)
        self._timeline_maximum = self._effective_common_frames(payload)
        self.fps.setValue(float(payload["fps"]))
        self.timeline_in.setRange(0, self._timeline_maximum - 1)
        self.timeline_out.setRange(1, self._timeline_maximum)
        self.timeline_in.setEnabled(True)
        self.timeline_out.setEnabled(True)
        self.timeline_bar.setEnabled(True)
        self.frame_limit.setEnabled(False)
        self._set_timeline_range(
            0 if lower < 0 else lower,
            self._timeline_maximum if upper is None else upper,
        )
        timeline_tc = str(payload["timeline_timecode"])
        self.timing_status.setText(f"START {timeline_tc}  ·  SHORTEST PLATE LOCK")

    def align_timecode(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
        except Exception as error:
            self._error("TC align", str(error))
            return
        report = self._working_dir / "timecode-alignment.json"
        report.unlink(missing_ok=True)

        def apply_alignment() -> None:
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
                self._tc_alignment_path = report
                self._apply_alignment_payload(payload)
                timeline_tc = str(payload["timeline_timecode"])
                common_frames = self._timeline_maximum
                self.timing_status.setText(
                    f"START {timeline_tc}  ·  SHORTEST PLATE LOCK"
                )
                self.statusBar().showMessage(
                    f"TC aligned at {timeline_tc} · {common_frames:,} common frames",
                    15000,
                )
            except Exception as error:
                self._error("TC align", str(error))

        self._run_cli(
            "TC ALIGN",
            [
                "align-timecode",
                "--config",
                str(config),
                "--output",
                str(report),
                *sources,
            ],
            apply_alignment,
        )

    def choose_ocio(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Open OCIO config", str(self.project_root), "OCIO config (*.ocio);;All files (*.*)")
        if path:
            self.ocio_config.setText(path)

    def apply_aces_preset(self) -> None:
        self.color_mode.setCurrentIndex(self.color_mode.findData("ocio"))
        self.ocio_config.setText(BUILTIN_ACES_STUDIO)
        self.input_space.setText("Camera Rec.709")
        self.working_space.setText("ACEScg")
        self.output_space.setText("Gamma 2.4 Encoded Rec.709")
        self._update_color_controls()
        self._append_log("Applied built-in ACES 2.0 Studio preset: Camera Rec.709 → ACEScg → Gamma 2.4 Rec.709")

    def choose_output(self) -> None:
        codec = str(self.output_codec.currentData())
        if codec.endswith("sequence"):
            path = QFileDialog.getExistingDirectory(self, "Select empty output directory", str(self._output_root))
        else:
            suffix = ".mov" if codec.startswith("prores") else ".mp4" if codec == "h264-mp4-10" else ".mkv"
            path, _ = QFileDialog.getSaveFileName(self, "Select output", str(self._output_root / f"stitched{suffix}"), "All files (*.*)")
        if path:
            self.output_path.setText(path)

    def _validate_sources(self) -> list[str]:
        paths = self.source_table.paths()
        if len(paths) != 5 or any(not path for path in paths):
            raise ValueError("Select all five camera videos")
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise ValueError("Missing video: " + missing[0])
        return paths

    def check_inputs(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
        except Exception as error:
            self._error("Input check", str(error))
            return
        self._run_cli("CHECK INPUTS", ["probe-inputs", "--config", str(config), *sources])

    def create_preview(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
            if self._tc_alignment:
                selected_frames = self.timeline_out.value() - self.timeline_in.value()
                reference_frame = int(round(self.preview_time.value() * self.fps.value()))
                if reference_frame >= selected_frames:
                    raise ValueError(
                        "Reference time is outside the selected SHARED TIMELINE range"
                    )
        except Exception as error:
            self._error("Preview", str(error))
            return
        stamp = f"{time.time_ns()}-{os.getpid()}"
        reference = self._working_dir / f"reference-{stamp}"
        self._last_reference_dir = reference
        self.preview.show_message("EXTRACTING SYNCHRONIZED REFERENCE FRAMES …")
        timeline_start = self.timeline_in.value() if self._tc_alignment else 0
        reference_time = self.preview_time.value()

        def stitch_reference() -> None:
            raw = json.loads(config.read_text(encoding="utf-8"))
            names = [camera["name"] for camera in raw["cameras"]]
            images = [str(reference / f"{name}.tif") for name in names]
            width, height = preview_dimensions(self.canvas_width.value(), self.canvas_height.value())
            preview_path = reference / "stitched-preview.tif"

            def load_preview() -> None:
                try:
                    self.preview.set_array(tifffile.imread(preview_path))
                    self.rig_align_button.setEnabled(True)
                    self.preview_note.setText(
                        "Rig Align is ready · it adjusts camera geometry, then refreshes this preview"
                    )
                    self.statusBar().showMessage(f"Preview ready: {width}×{height} (display only)", 10000)
                except Exception as error:
                    self._error("Preview load", str(error))

            self.preview.show_message("STITCHING 16-BIT PREVIEW …")
            self._run_cli(
                "STITCH PREVIEW",
                [
                    "stitch-frame",
                    "--config",
                    str(config),
                    "--output",
                    str(preview_path),
                    "--canvas",
                    f"{width}x{height}",
                    *images,
                ],
                load_preview,
            )

        arguments = [
            "extract-reference",
            "--config",
            str(config),
            "--time",
            str(reference_time),
            "--start-frame",
            str(timeline_start),
            "--output-dir",
            str(reference),
        ]
        if self._tc_alignment_path:
            arguments.extend(["--alignment-plan", str(self._tc_alignment_path)])
        arguments.extend(sources)
        self._run_cli("EXTRACT REFERENCES", arguments, stitch_reference)

    def auto_align(self) -> None:
        if self._last_reference_dir is None:
            self._error("Auto align", "Create a preview/reference frame first")
            return
        try:
            config = self._write_working_config()
            raw = json.loads(config.read_text(encoding="utf-8"))
            images = [str(self._last_reference_dir / f"{camera['name']}.tif") for camera in raw["cameras"]]
            if any(not Path(path).is_file() for path in images):
                raise ValueError("Reference frames are missing; create preview again")
        except Exception as error:
            self._error("Auto align", str(error))
            return
        output = self._working_dir / "calibrated-rig.json"
        report = self._working_dir / "alignment-report.json"

        def load_alignment() -> None:
            current_paths = self.source_table.paths()
            tc_alignment = self._tc_alignment
            tc_alignment_path = self._tc_alignment_path
            timeline_range = self.timeline_bar.values()
            self.load_config(output)
            self.source_table.set_paths(current_paths)
            if tc_alignment:
                self._tc_alignment_path = tc_alignment_path
                self._apply_alignment_payload(
                    tc_alignment,
                    timeline_range[0],
                    timeline_range[1],
                )
            self.statusBar().showMessage("Rig alignment applied · refreshing preview…", 15000)
            self.preview_note.setText("Rig aligned · refreshing corrected panorama preview…")
            self.create_preview()

        self._run_cli(
            "AUTO ALIGN",
            [
                "calibrate-rig",
                "--config",
                str(config),
                "--output",
                str(output),
                "--report",
                str(report),
                *images,
            ],
            load_alignment,
        )

    def analyze_coverage(self) -> None:
        try:
            config = self._write_working_config()
        except Exception as error:
            self._error("Coverage", str(error))
            return
        mask = self._working_dir / "coverage.png"
        self._run_cli(
            "ANALYZE COVERAGE",
            ["analyze-canvas", "--config", str(config), "--mask", str(mask)],
        )

    def estimate_resources(self) -> None:
        try:
            config = self._write_working_config()
        except Exception as error:
            self._error("Resources", str(error))
            return
        self._run_cli("ESTIMATE RESOURCES", ["estimate-resources", "--config", str(config)])

    def render(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
            output = self.output_path.text().strip()
            if not output:
                raise ValueError("Choose a render destination")
            codec = str(self.output_codec.currentData())
            if codec.endswith("sequence") and Path(output).exists() and any(Path(output).iterdir()):
                raise ValueError("Sequence output directory must be empty")
        except Exception as error:
            self._error("Render", str(error))
            return
        message = (
            f"Start {self.canvas_width.value()}×{self.canvas_height.value()} render?\n\n"
            f"Codec: {self.output_codec.currentText()}\nOutput: {output}"
        )
        if QMessageBox.question(self, "Start render", message) != QMessageBox.StandardButton.Yes:
            return
        arguments = [
            "stitch-video",
            "--config",
            str(config),
            "--output",
            output,
            "--map-cache",
            str(self._cache_dir),
            "--start-frame",
            str(self.timeline_in.value() if self._tc_alignment else 0),
        ]
        if self._tc_alignment_path:
            arguments.extend(["--alignment-plan", str(self._tc_alignment_path)])
        arguments.extend(sources)
        self._run_cli(
            "FINAL RENDER",
            arguments,
            lambda: QMessageBox.information(self, "Render complete", f"Output written to:\n{output}"),
        )

    def _run_cli(self, task: str, arguments: list[str], success: Callable[[], None] | None = None) -> None:
        if self.process is not None:
            self._error("Busy", "Another task is already running")
            return
        self._process_success = success
        self._process_output = ""
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self._working_dir))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process)
        self.process.finished.connect(self._process_finished)
        self.task_label.setText(task)
        self.status_pill.setText(task)
        self.cancel_button.setVisible(True)
        self.progress.setRange(0, 0)
        self._append_log("\n▶ " + task)
        self._append_log("  vpstitch " + " ".join(f'"{arg}"' if " " in arg else arg for arg in arguments))
        if getattr(sys, "frozen", False):
            helper_name = "vpstitch-cli.exe" if os.name == "nt" else "vpstitch-cli"
            cli_program = Path(sys.executable).with_name(helper_name)
            if not cli_program.is_file():
                self.process = None
                self._error("Packaged CLI missing", f"Expected bundled helper at:\n{cli_program}")
                return
            self.process.start(str(cli_program), arguments)
        else:
            self.process.start(sys.executable, ["-m", "vpstitch.cli", *arguments])

    def _read_process(self) -> None:
        if self.process is None:
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        self._process_output += text
        normalized = text.replace("\r", "\n")
        for line in normalized.splitlines():
            if line.strip():
                self._append_log(line)
            match = re.search(r"tiles\s+(\d+)/(\d+)", line)
            if match:
                done, total = int(match.group(1)), int(match.group(2))
                self.progress.setRange(0, total)
                self.progress.setValue(done)
            frame = re.search(r"frame\s+(\d+)", line)
            if frame:
                self.task_label.setText(f"FRAME {frame.group(1)}")

    def _process_finished(self, exit_code: int, _status) -> None:  # type: ignore[no-untyped-def]
        callback = self._process_success
        process = self.process
        task = self.task_label.text()
        self.process = None
        self._process_success = None
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if exit_code == 0 else 0)
        self.task_label.setText("IDLE" if exit_code == 0 else "FAILED")
        self.status_pill.setText("READY" if exit_code == 0 else "FAILED")
        self.cancel_button.setVisible(False)
        if process is not None:
            process.deleteLater()
        if exit_code == 0:
            self._append_log(f"✓ {task} complete")
            if callback:
                callback()
        else:
            self._append_log(f"✕ {task} failed with exit code {exit_code}")
            self.statusBar().showMessage("Task failed — see Task Log", 10000)

    def cancel_task(self) -> None:
        if self.process is not None:
            self._append_log("Cancelling task …")
            self.status_pill.setText("CANCELLING")
            self.process.kill()

    def _append_log(self, text: str) -> None:
        self.log.appendPlainText(text)
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _update_color_controls(self) -> None:
        enabled = self.color_mode.currentData() == "ocio"
        for widget in (self.ocio_config, self.input_space, self.working_space, self.output_space):
            widget.setEnabled(enabled)

    def _update_output_hint(self) -> None:
        codec = str(self.output_codec.currentData())
        hints = {
            "ffv1-16": "권장 장편 마스터: 무손실 16-bit RGB MKV. 파일 크기는 영상 내용에 따라 달라집니다.",
            "exr-half-sequence": "OCIO scene-linear 권장: 음수와 1 초과 값을 보존합니다. 출력 폴더가 필요합니다.",
            "dpx12-sequence": "VFX/Resolve 교환용 12-bit RGB DPX 시퀀스. 출력 폴더가 필요합니다.",
            "tiff16-sequence": "정수 RGB 품질 기준용. 20K/29.97fps는 무압축 기준 분당 약 1.18TiB입니다.",
            "prores-4444": "편집 호환용 10-bit YUV. 16-bit RGB 보존 마스터는 아닙니다.",
            "prores-hq": "편집용 10-bit 4:2:2. 크로마 해상도가 줄어듭니다.",
            "h264-mp4-10": "검수용 10-bit H.264 MP4. 15K 마스터 대신 축소 프리뷰에 권장합니다.",
            "hevc-444-10": "납품/검수용. 15K 이상의 표준 HEVC picture level 한계에 주의하십시오.",
        }
        self.output_hint.setText(hints.get(codec, ""))

    def _error(self, title: str, message: str) -> None:
        self._append_log(f"ERROR: {message}")
        QMessageBox.critical(self, title, message)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        configs = [path for path in paths if Path(path).suffix.lower() == ".json"]
        videos = [path for path in paths if Path(path).suffix.lower() in {".mov", ".mp4", ".mkv", ".avi", ".mxf"}]
        if len(configs) == 1:
            self.load_config(Path(configs[0]))
        if videos:
            if len(videos) == self.source_table.rowCount():
                self.source_table.set_paths(videos)
                self._reset_timing()
            else:
                self._error("Drop videos", f"Drop exactly {self.source_table.rowCount()} videos at once")
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.process is not None:
            answer = QMessageBox.question(self, "Task running", "Cancel the running task and close?")
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self.process.kill()
            self.process.waitForFinished(3000)
        event.accept()


def main() -> int:
    try:
        app = QApplication.instance() or QApplication(sys.argv)
        app.setApplicationName(APP_NAME)
        app.setOrganizationName("VP-LAB")
        window = MainWindow()
        window.show()
        return app.exec()
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
