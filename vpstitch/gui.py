from __future__ import annotations

import ctypes
import json
import os
import re
import shutil
import sys
import time
import traceback
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import (
    QCoreApplication,
    QProcess,
    QSettings,
    QStandardPaths,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QCloseEvent, QFont, QImage, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
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
    QMenu,
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

from .imageio import read_image

from .config import MAX_CANVAS_HEIGHT, MAX_CANVAS_WIDTH


APP_NAME = "VP Stitch"
BUILTIN_ACES_STUDIO = "ocio://studio-config-v4.0.0_aces-v2.0_ocio-v2.5"
VIDEO_FILTER = "Video files (*.mov *.mp4 *.mkv *.avi *.mxf);;All files (*.*)"
SUPPORTED_CAMERA_COUNTS = (3, 5)
_MACOS_AX_SHIM: ctypes.CDLL | None = None
GUI_MASTER_BIT_DEPTHS = {
    "prores-hq": 10,
    "prores-4444": 10,
    "h264-mp4-10": 10,
    "hevc-444-10": 10,
    "dpx12-sequence": 12,
}
INPUT_COLOR_SPACES = (
    ("Auto", None),
    ("Rec.709", "bt709"),
    ("Rec.2020", "bt2020nc"),
    ("Rec.601", "smpte170m"),
)
INPUT_VIDEO_RANGES = (
    ("Auto", None),
    ("Video (Limited)", "tv"),
    ("Full", "pc"),
)
_EXPLICIT_PLATE_NUMBER = re.compile(
    r"(?:^|[^a-z0-9])(?:p(?:late)?|cam(?:era)?)[ ._-]*0?([1-5])(?=$|[^0-9])",
    re.IGNORECASE,
)
_BARE_PLATE_NUMBER = re.compile(r"(?:^|[^0-9])0([1-5])(?=$|[^0-9])")


def plate_number(path: str | Path) -> int | None:
    """Read a one-based P01-P05 camera number from a clip or parent folder name."""
    source = Path(path)
    components = [source.stem, *(parent.name for parent in source.parents[:3])]
    for pattern in (_EXPLICIT_PLATE_NUMBER, _BARE_PLATE_NUMBER):
        for component in components:
            match = pattern.search(component)
            if match:
                return int(match.group(1))
    return None


def order_camera_plates(paths: list[str]) -> tuple[list[str], list[int] | None]:
    """Validate 3/5-plate imports and order recognized P01-P05 names."""
    if len(paths) not in SUPPORTED_CAMERA_COUNTS:
        raise ValueError("Select either 3 plates (P01-P03) or 5 plates (P01-P05)")

    detected = [plate_number(path) for path in paths]
    if all(number is None for number in detected):
        natural = lambda value: [
            int(token) if token.isdigit() else token.casefold()
            for token in re.split(r"(\d+)", Path(value).name)
        ]
        return sorted(paths, key=natural), None
    if any(number is None for number in detected):
        raise ValueError("Some plate numbers are missing. Name every clip P01-P03 or P01-P05")

    numbers = [int(number) for number in detected if number is not None]
    expected = list(range(1, len(paths) + 1))
    if sorted(numbers) != expected:
        expected_text = ", ".join(f"P{number:02d}" for number in expected)
        raise ValueError(f"Plate names must contain each of {expected_text} exactly once")
    ordered = sorted(zip(numbers, paths, strict=True), key=lambda item: item[0])
    return [path for _, path in ordered], [number for number, _ in ordered]


def _runtime_root() -> Path:
    """Return the directory that contains bundled read-only resources."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent.parent / "Resources"))
    return Path(__file__).resolve().parent.parent


def _user_data_root() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(location) if location else Path.home() / "Library" / "Application Support" / "VP-LAB" / APP_NAME


def preview_dimensions(
    width: int,
    height: int,
    max_width: int = 3840,
    max_height: int = 2160,
) -> tuple[int, int]:
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def _stabilize_macos_accessibility_bridge() -> bool:
    """Suppress Qt's unstable Cocoa child hierarchy during AX enumeration."""
    global _MACOS_AX_SHIM
    if sys.platform != "darwin" or os.environ.get("VPSTITCH_ENABLE_ACCESSIBILITY") == "1":
        return False
    try:
        shim_name = "libvpstitch_macos_ax.dylib"
        candidates = (
            _runtime_root() / shim_name,
            Path(__file__).resolve().parent.parent / ".build" / "macos" / shim_name,
        )
        shim_path = next((path for path in candidates if path.is_file()), None)
        if shim_path is None:
            return False
        shim = ctypes.CDLL(str(shim_path))
        shim.vpstitch_no_accessible_children.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        shim.vpstitch_no_accessible_children.restype = ctypes.c_void_p
        objc = ctypes.CDLL("/usr/lib/libobjc.A.dylib")
        objc.objc_getClass.argtypes = [ctypes.c_char_p]
        objc.objc_getClass.restype = ctypes.c_void_p
        objc.sel_registerName.argtypes = [ctypes.c_char_p]
        objc.sel_registerName.restype = ctypes.c_void_p
        objc.class_getInstanceMethod.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        objc.class_getInstanceMethod.restype = ctypes.c_void_p
        objc.method_getTypeEncoding.argtypes = [ctypes.c_void_p]
        objc.method_getTypeEncoding.restype = ctypes.c_char_p
        objc.class_replaceMethod.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_char_p,
        ]
        objc.class_replaceMethod.restype = ctypes.c_void_p
        qns_view = objc.objc_getClass(b"QNSView")
        if not qns_view:
            return False

        selector = objc.sel_registerName(b"accessibilityChildren")
        method = objc.class_getInstanceMethod(qns_view, selector)
        encoding = objc.method_getTypeEncoding(method) if method else b"@@:"
        objc.class_replaceMethod(
            qns_view,
            selector,
            ctypes.cast(shim.vpstitch_no_accessible_children, ctypes.c_void_p),
            encoding,
        )
        _MACOS_AX_SHIM = shim
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


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
        self._empty = QLabel("P01–P03 또는 P01–P05를 넣고  PREVIEW  를 누르세요")
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
    """Trim range plus a clearly draggable preview playhead."""

    rangeChanged = Signal(int, int)
    playheadChanged = Signal(int)
    playheadReleased = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self._maximum = 1
        self._lower = 0
        self._upper = 1
        self._playhead = 0
        self._active: str | None = None
        self.setMinimumHeight(42)
        self.setToolTip(
            "Drag the edge caps to trim. Drag the bright playhead to scrub the stitched preview."
        )
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_frame_range(
        self,
        maximum: int,
        lower: int = 0,
        upper: int | None = None,
        playhead: int | None = None,
    ) -> None:
        self._maximum = max(1, int(maximum))
        self._lower = max(0, min(int(lower), self._maximum - 1))
        requested_upper = self._maximum if upper is None else int(upper)
        self._upper = max(self._lower + 1, min(requested_upper, self._maximum))
        requested_playhead = self._playhead if playhead is None else int(playhead)
        self._playhead = max(self._lower, min(requested_playhead, self._upper - 1))
        self.update()

    def values(self) -> tuple[int, int]:
        return self._lower, self._upper

    def playhead(self) -> int:
        return self._playhead

    def set_playhead(self, value: int) -> None:
        value = max(self._lower, min(int(value), self._upper - 1))
        if value != self._playhead:
            self._playhead = value
            self.update()

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
        painter.setBrush(QColor("#30363f") if self.isEnabled() else QColor("#292e35"))
        painter.drawRoundedRect(left, center - 5.0, right - left, 10.0, 5.0, 5.0)
        lower_x = self._position(self._lower)
        upper_x = self._position(self._upper)
        painter.setBrush(QColor("#55516f") if self.isEnabled() else QColor("#3e3d49"))
        painter.drawRoundedRect(lower_x, center - 6.0, upper_x - lower_x, 12.0, 5.0, 5.0)
        for position in (lower_x, upper_x):
            painter.setBrush(QColor("#9b96b8") if self.isEnabled() else QColor("#5d626b"))
            painter.drawRoundedRect(position - 4.0, center - 14.0, 8.0, 28.0, 3.0, 3.0)
        playhead_x = self._position(self._playhead)
        painter.setPen(QColor("#f0eef7") if self.isEnabled() else QColor("#707680"))
        painter.drawLine(
            int(playhead_x),
            int(center - 15.0),
            int(playhead_x),
            int(center + 15.0),
        )
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor("#f0eef7") if self.isEnabled() else QColor("#707680"))
        painter.drawEllipse(playhead_x - 4.0, center - 18.0, 8.0, 8.0)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self.isEnabled():
            return
        position = event.position().x()
        distances = {
            "lower": abs(position - self._position(self._lower)),
            "upper": abs(position - self._position(self._upper)),
            "playhead": abs(position - self._position(self._playhead)),
        }
        nearest = min(distances, key=distances.get)
        self._active = nearest if distances[nearest] <= 11.0 else "playhead"
        self._move_active(position)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self._active:
            self._move_active(event.position().x())

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        del event
        if self._active == "playhead":
            self.playheadReleased.emit(self._playhead)
        self._active = None

    def _move_active(self, position: float) -> None:
        value = self._value(position)
        if self._active == "lower":
            self._lower = min(max(0, value), self._upper - 1)
            self._playhead = max(self._lower, self._playhead)
        elif self._active == "upper":
            self._upper = max(self._lower + 1, min(value, self._maximum))
            self._playhead = min(self._playhead, self._upper - 1)
        elif self._active == "playhead":
            self._playhead = max(self._lower, min(value, self._upper - 1))
            self.playheadChanged.emit(self._playhead)
        self.update()
        if self._active in {"lower", "upper"}:
            self.rangeChanged.emit(self._lower, self._upper)


def _format_bit_rate(value: object) -> str:
    try:
        bits_per_second = int(value)
    except (TypeError, ValueError):
        return "—"
    if bits_per_second >= 1_000_000:
        return f"{bits_per_second / 1_000_000:.1f} Mb/s"
    if bits_per_second >= 1_000:
        return f"{bits_per_second / 1_000:.0f} kb/s"
    return f"{bits_per_second} b/s"


class InputSettingsDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        paths: list[str],
        probes: list[dict[str, object] | None],
        overrides: list[dict[str, str | None]],
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Clip Input Settings")
        self.setModal(True)
        self.setMinimumWidth(440)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(18, 16, 18, 14)
        layout.setSpacing(12)

        title = QLabel(
            Path(paths[0]).name if len(paths) == 1 else f"{len(paths)} clips selected"
        )
        title.setProperty("sectionTitle", True)
        layout.addWidget(title)

        info = QFormLayout()
        info.setLabelAlignment(Qt.AlignmentFlag.AlignLeft)
        info.setHorizontalSpacing(24)
        info.setVerticalSpacing(6)
        populated = [probe for probe in probes if probe is not None]

        def common(key: str, fallback: str = "—") -> object:
            values = {str(probe.get(key) or fallback) for probe in populated}
            if not values:
                return fallback
            return next(iter(values)) if len(values) == 1 else "Mixed"

        resolution = "—"
        if populated:
            sizes = {
                f"{probe.get('width', '—')}×{probe.get('height', '—')} · "
                f"{float(probe.get('fps', 0.0)):.3f} fps"
                for probe in populated
            }
            resolution = next(iter(sizes)) if len(sizes) == 1 else "Mixed"
        depth = "—"
        if populated:
            depths = {
                f"{probe.get('pixel_format', 'unknown')} · {probe.get('bit_depth', '—')}-bit"
                for probe in populated
            }
            depth = next(iter(depths)) if len(depths) == 1 else "Mixed"
        rates = {_format_bit_rate(probe.get("bit_rate")) for probe in populated}
        bit_rate = next(iter(rates)) if len(rates) == 1 else "Mixed"
        detected_color = common("colorspace")
        detected_range = {
            "tv": "Video (Limited)",
            "pc": "Full",
            "—": "—",
        }.get(str(common("color_range")), str(common("color_range")))
        for label, value in (
            ("Codec", common("codec")),
            ("Resolution", resolution),
            ("Pixel format", depth),
            ("Bitrate", bit_rate),
            ("Detected color", detected_color),
            ("Detected range", detected_range),
        ):
            value_label = QLabel(str(value))
            value_label.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
            info.addRow(label, value_label)
        layout.addLayout(info)

        note = QLabel(
            "Bit depth and bitrate are intrinsic source properties. Input settings only "
            "change how pixels are interpreted; preview and final render use the same setting."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        interpretation = QFormLayout()
        interpretation.setHorizontalSpacing(24)
        interpretation.setVerticalSpacing(8)
        self.color_space = QComboBox()
        for label, value in INPUT_COLOR_SPACES:
            self.color_space.addItem(label, value)
        self.video_range = QComboBox()
        for label, value in INPUT_VIDEO_RANGES:
            self.video_range.addItem(label, value)

        def shared(key: str) -> str | None:
            values = {override.get(key) for override in overrides}
            return next(iter(values)) if len(values) == 1 else None

        self.color_space.setCurrentIndex(
            max(0, self.color_space.findData(shared("input_color_space")))
        )
        self.video_range.setCurrentIndex(
            max(0, self.video_range.findData(shared("input_video_range")))
        )
        interpretation.addRow("Input Color Space", self.color_space)
        interpretation.addRow("Video Range", self.video_range)
        layout.addLayout(interpretation)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        apply_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        apply_button.setText("APPLY")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self) -> dict[str, str | None]:
        return {
            "input_color_space": self.color_space.currentData(),
            "input_video_range": self.video_range.currentData(),
        }


class SourceTable(QTableWidget):
    inputSettingsRequested = Signal(list)

    def __init__(self) -> None:
        super().__init__(5, 9)
        self._active_count = 5
        self.setHorizontalHeaderLabels(
            ["CAM", "CLIP", "TC IN", "FRAMES", "STATUS", "YAW", "PITCH", "ROLL", "OFFSET"]
        )
        self.verticalHeader().hide()
        self.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.customContextMenuRequested.connect(self._show_context_menu)
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

    def _show_context_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        item = self.itemAt(position)
        if item is None:
            return
        row = item.row()
        selected = {index.row() for index in self.selectionModel().selectedRows()}
        if row not in selected:
            self.clearSelection()
            self.selectRow(row)
            selected = {row}
        menu = QMenu(self)
        action = menu.addAction("INPUT SETTINGS…")
        chosen = menu.exec(self.viewport().mapToGlobal(position))
        if chosen is action:
            self.inputSettingsRequested.emit(sorted(selected))

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() in {Qt.Key.Key_Menu, Qt.Key.Key_F10}:
            rows = sorted(index.row() for index in self.selectionModel().selectedRows())
            if rows:
                self.inputSettingsRequested.emit(rows)
            return
        super().keyPressEvent(event)

    def set_rig(self, cameras: list[dict[str, object]], paths: list[str] | None = None) -> None:
        if not 1 <= len(cameras) <= self.rowCount():
            raise ValueError("camera count exceeds the source table capacity")
        paths = paths or [""] * len(cameras)
        self._active_count = len(cameras)
        table_height = 34 + len(cameras) * self.verticalHeader().defaultSectionSize()
        self.setMinimumHeight(table_height)
        self.setMaximumHeight(table_height)
        for row in range(self.rowCount()):
            active = row < self._active_count
            self.setRowHidden(row, not active)
            if not active:
                continue
            camera = cameras[row]
            values = [
                f"CAM {row + 1}",
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
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(row, column, item)
                item.setText(display_value)
                if column in {0, 1, 2, 3, 4}:
                    item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)
                if column == 1:
                    item.setToolTip(value)
                    item.setData(Qt.ItemDataRole.UserRole, value)

    def camera_count(self) -> int:
        return self._active_count

    def paths(self) -> list[str]:
        paths: list[str] = []
        for row in range(self.camera_count()):
            item = self.item(row, 1)
            if item is None:
                paths.append("")
                continue
            stored = item.data(Qt.ItemDataRole.UserRole)
            paths.append(str(stored).strip() if stored is not None else item.text().strip())
        return paths

    def set_paths(self, paths: list[str]) -> None:
        for row, path in enumerate(paths[: self.camera_count()]):
            item = self.item(row, 1)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, 1, item)
            item.setText(Path(path).name if path else "")
            item.setToolTip(path)
            item.setData(Qt.ItemDataRole.UserRole, path)

    def clear_timing(self) -> None:
        for row in range(self.camera_count()):
            for column in (2, 3, 4):
                item = self.item(row, column)
                if item is None:
                    item = QTableWidgetItem()
                    self.setItem(row, column, item)
                item.setText("—")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def set_timing(self, inputs: list[dict[str, object]]) -> None:
        if len(inputs) != self.camera_count():
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

    def set_probe_data(
        self,
        inputs: list[dict[str, object]],
        overrides: dict[str, dict[str, str | None]] | None = None,
    ) -> None:
        if len(inputs) != self.camera_count():
            raise ValueError("source probe result does not match the source count")
        for row, probe in enumerate(inputs):
            bit_depth = int(probe.get("bit_depth", 0))
            pixel_format = str(probe.get("pixel_format") or "unknown")
            path = self.paths()[row]
            interpretation = (overrides or {}).get(path, {})
            color_space = interpretation.get("input_color_space")
            video_range = interpretation.get("input_video_range")
            color_label = {
                "bt709": "709",
                "bt2020nc": "2020",
                "smpte170m": "601",
            }.get(color_space)
            range_label = {"tv": "VIDEO", "pc": "FULL"}.get(video_range)
            suffix = "/".join(value for value in (color_label, range_label) if value)
            item = self.item(row, 4)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, 4, item)
            item.setText(f"{bit_depth}b" + (f" {suffix}" if suffix else " AUTO"))
            item.setToolTip(
                f"Detected: {pixel_format}, {bit_depth}-bit, "
                f"{_format_bit_rate(probe.get('bit_rate'))}\n"
                f"Input Color Space: {color_label or 'Auto'}\n"
                f"Video Range: {range_label or 'Auto'}"
            )
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def offsets(self) -> list[int]:
        try:
            return [int(self.item(row, 8).text()) for row in range(self.camera_count())]
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
        self._rig_profiles: dict[int, dict[str, object]] = {}
        self._plate_numbers: list[int] | None = None
        self._source_probes: list[dict[str, object]] | None = None
        self._source_overrides: dict[str, dict[str, str | None]] = {}
        self._closing = False
        self._import_dialog: QFileDialog | None = None
        self._message_box: QMessageBox | None = None
        self._pending_log_lines: list[str] = []
        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(50)
        self._log_flush_timer.setSingleShot(True)
        self._log_flush_timer.timeout.connect(self._flush_log)
        self.process: QProcess | None = None
        self._process_success: Callable[[], None] | None = None
        self._process_failure: Callable[[], None] | None = None
        self._last_reference_dir: Path | None = None
        self._last_reference_config_path: Path | None = None
        self._preview_ready = False
        self._preview_in_progress = False
        self._pending_scrub_frame: int | None = None
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
        self.statusBar().showMessage("Ready · preview fits within 4K; final render stays full resolution")

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
        self.app_subtitle = QLabel("5-CAMERA 180° PANORAMA")
        self.app_subtitle.setObjectName("appSubtitle")
        top_layout.addWidget(self.app_subtitle)
        top_layout.addStretch()
        self.profile_label = QLabel("Rig Profile · Loading…")
        self.profile_label.setObjectName("profileLabel")
        self.profile_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self.profile_label)
        top_layout.addStretch()
        self.status_pill = QLabel("READY")
        self.status_pill.hide()
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
        self.source_table.inputSettingsRequested.connect(self._open_input_settings)
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
        self.media_hint = QLabel("Import P01–P03 or P01–P05 · auto ordered")
        self.media_hint.setWordWrap(True)
        self.media_hint.setProperty("muted", True)
        source_layout.addWidget(self.media_hint)
        source_layout.addWidget(self.source_table)
        source_buttons = QHBoxLayout()
        self.import_button = QPushButton("IMPORT PLATES")
        self.import_button.setObjectName("primaryButton")
        self.import_button.clicked.connect(self.choose_videos)
        self.clear_button = QPushButton("CLEAR")
        self.clear_button.setObjectName("secondaryButton")
        self.clear_button.clicked.connect(self.clear_sources)
        source_buttons.addWidget(self.import_button, 1)
        source_buttons.addWidget(self.clear_button)
        source_layout.addLayout(source_buttons)
        source_layout.addStretch()
        self.source_status = QLabel("Drop P01–P03 or P01–P05 clips here")
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
        preview_limit = QLabel("UHD 4K MAX  ·  FIT / NO CROP")
        preview_limit.setObjectName("previewLimit")
        preview_header.addWidget(title)
        preview_header.addStretch()
        preview_header.addWidget(preview_limit)
        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.preview, 1)
        self.preview_note = QLabel("Fitted preview · UHD 4K max · master render stays full resolution")
        self.preview_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_note.setProperty("muted", True)
        preview_layout.addWidget(self.preview_note)

        settings_scroll = QScrollArea()
        settings_scroll.setObjectName("settingsPanel")
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumWidth(286)
        settings_scroll.setMaximumWidth(350)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setMinimumWidth(0)
        self.settings_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings_tabs.addTab(self._stitch_settings(), "RIG")
        self.settings_tabs.addTab(self._color_settings(), "COLOR")
        self.settings_tabs.addTab(self._output_settings(), "DELIVER")
        settings_scroll.setWidget(self.settings_tabs)
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
        self.timing_status = QLabel("TC Align finds the shortest common range across every camera")
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
        self.timeline_bar.playheadChanged.connect(self._timeline_playhead_changed)
        self.timeline_bar.playheadReleased.connect(self._timeline_playhead_released)
        timing_layout.addWidget(self.timeline_bar)
        timing_values = QHBoxLayout()
        self.timeline_in = QSpinBox()
        self.timeline_out = QSpinBox()
        for widget in (self.timeline_in, self.timeline_out):
            widget.setRange(0, 10_000_000)
            widget.setEnabled(False)
            widget.valueChanged.connect(self._timeline_spin_changed)
        self.timeline_playhead = QSpinBox()
        self.timeline_playhead.setRange(0, 10_000_000)
        self.timeline_playhead.setEnabled(False)
        self.timeline_playhead.valueChanged.connect(self._timeline_playhead_spin_changed)
        self.timeline_playhead.editingFinished.connect(self._scrub_preview)
        self.playhead_time = QLabel("00:00:00.000")
        self.playhead_time.setObjectName("playheadTime")
        self.reset_timeline_button = QPushButton("RESET RANGE")
        self.reset_timeline_button.setObjectName("quietButton")
        self.reset_timeline_button.clicked.connect(self._reset_timeline_range)
        timing_values.addWidget(QLabel("IN"))
        timing_values.addWidget(self.timeline_in)
        timing_values.addSpacing(10)
        timing_values.addWidget(QLabel("OUT"))
        timing_values.addWidget(self.timeline_out)
        timing_values.addSpacing(14)
        timing_values.addWidget(QLabel("PLAYHEAD"))
        timing_values.addWidget(self.timeline_playhead)
        timing_values.addWidget(self.playhead_time)
        timing_values.addStretch()
        timing_values.addWidget(self.reset_timeline_button)
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

    def _profile_for_count(self, count: int) -> dict[str, object]:
        if count in self._rig_profiles:
            return json.loads(json.dumps(self._rig_profiles[count]))
        if count == 5:
            for name in ("drive_5cam_180.prores-hq.json", "five_cam_180.sample.json"):
                path = self.project_root / "configs" / name
                if not path.is_file():
                    continue
                profile = json.loads(path.read_text(encoding="utf-8"))
                cameras = profile.get("cameras")
                if isinstance(cameras, list) and len(cameras) == 5:
                    self._rig_profiles[5] = json.loads(json.dumps(profile))
                    return profile
        if count == 3 and 5 in self._rig_profiles:
            profile = json.loads(json.dumps(self._rig_profiles[5]))
            cameras = profile["cameras"]
            profile["cameras"] = [cameras[0], cameras[len(cameras) // 2], cameras[-1]]
            self._rig_profiles[3] = json.loads(json.dumps(profile))
            return profile
        raise ValueError(f"Load a calibrated {count}-camera Rig Profile first")

    def _activate_camera_count(self, count: int) -> None:
        profile = self._profile_for_count(count)
        cameras = profile.get("cameras")
        if not isinstance(cameras, list) or len(cameras) != count:
            raise ValueError(f"The active Rig Profile does not contain {count} cameras")
        self.config_data["cameras"] = json.loads(json.dumps(cameras))
        self.source_table.blockSignals(True)
        self.source_table.set_rig(self.config_data["cameras"])
        self.source_table.blockSignals(False)
        self.app_subtitle.setText(f"{count}-CAMERA 180° PANORAMA")
        profile_kind = "Auto Profile" if self.config_path and self.config_path.parent == self.project_root / "configs" else "Custom Profile"
        self.profile_label.setText(f"Drive {count}-Cam · {profile_kind}")
        self.setWindowTitle(f"{APP_NAME}  —  {count}-Camera 180°")

    def _set_video_sources(self, files: list[str]) -> None:
        ordered, numbers = order_camera_plates(files)
        self._activate_camera_count(len(ordered))
        self._plate_numbers = numbers
        self._source_probes = None
        cameras = self.config_data["cameras"]
        self._source_overrides = {
            path: {
                "input_color_space": camera.get("input_color_space"),
                "input_video_range": camera.get("input_video_range"),
            }
            for path, camera in zip(ordered, cameras, strict=True)
        }
        self.source_table.set_paths(ordered)
        self._reset_timing()
        order_note = (
            f"P{numbers[0]:02d} → P{numbers[-1]:02d}"
            if numbers
            else "natural filename order"
        )
        self._append_log(f"Imported {len(ordered)} plates · {order_note}")

    def _update_source_status(self) -> None:
        loaded = sum(bool(path) for path in self.source_table.paths())
        expected = self.source_table.camera_count()
        if loaded == self.source_table.camera_count():
            order_note = (
                f"P{self._plate_numbers[0]:02d} → P{self._plate_numbers[-1]:02d}"
                if self._plate_numbers
                else "filename order"
            )
            if self._source_probes:
                depths = sorted({int(probe["bit_depth"]) for probe in self._source_probes})
                source_depth = "/".join(str(depth) for depth in depths)
                self.source_status.setText(
                    f"●  {loaded} plates · {order_note}\n"
                    f"SOURCE {source_depth}-bit → MASTER 10/12-bit"
                    + (
                        " · INPUT OVERRIDE"
                        if any(
                            any(value for value in override.values())
                            for override in self._source_overrides.values()
                        )
                        else ""
                    )
                )
            else:
                self.source_status.setText(
                    f"●  {loaded} of {expected} plates ready · detecting source depth…"
                )
        elif loaded:
            self.source_status.setText(f"●  {loaded} of {expected} plates loaded")
        else:
            self.source_status.setText("Drop P01–P03 or P01–P05 clips here")

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
            "Drive Rig loads automatically for 3 or 5 plates. It stores lens calibration, "
            "camera angles, and the 180° output layout."
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
            "GUI 마스터 출력은 10/12-bit로 제한됩니다."
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
            QLabel#previewLimit {
                color:#8f96a1;
                border:1px solid #303740;
                border-radius:3px;
                padding:3px 7px;
                font-size:9px;
                font-weight:700;
            }
            QLabel#playheadTime {
                color:#d8d5e6;
                padding:2px 5px;
                font-family:'Cascadia Mono','SF Mono','Menlo';
                font-size:10px;
            }
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
            if len(cameras) not in SUPPORTED_CAMERA_COUNTS:
                raise ValueError("GUI Rig Profiles must contain either 3 or 5 cameras")
        except Exception as error:
            self._error("Rig profile error", str(error))
            return
        self.config_path = path
        self.config_data = raw
        if len(cameras) == 5:
            self._rig_profiles.pop(3, None)
        self._rig_profiles[len(cameras)] = json.loads(json.dumps(raw))
        self._plate_numbers = None
        self._source_probes = None
        self._source_overrides = {}
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
        codec = str(video.get("output_codec", "prores-hq"))
        codec_index = self.output_codec.findData(codec)
        self.output_codec.setCurrentIndex(
            self.output_codec.findData("prores-hq") if codec_index < 0 else codec_index
        )
        self.fps.setValue(float(video.get("fps", 29.97)))
        self._configured_frame_limit = int(video.get("frames") or 0)
        self.frame_limit.setValue(self._configured_frame_limit)
        self._reset_timing()
        self.settings.setValue("lastConfig", str(path))
        is_builtin = path.name.startswith("drive_5cam_180") or path.parent == self.project_root / "configs"
        count = len(cameras)
        profile_name = f"Drive {count}-Cam · Auto Profile" if is_builtin else f"{path.stem} · Custom Profile"
        self.profile_label.setText(profile_name)
        self.profile_label.setToolTip(str(path))
        self.app_subtitle.setText(f"{count}-CAMERA 180° PANORAMA")
        self.setWindowTitle(f"{APP_NAME}  —  {count}-Camera 180°")
        self._update_color_controls()
        self._update_output_hint()
        self._append_log(f"Loaded rig profile: {path}")

    def _collect_config(self) -> dict[str, object]:
        if not self.config_data:
            raise ValueError("Load a rig profile first")
        raw = json.loads(json.dumps(self.config_data))
        cameras = raw["cameras"]
        self.source_table.apply_to_cameras(cameras)
        for path, camera in zip(self.source_table.paths(), cameras, strict=True):
            override = self._source_overrides.get(path, {})
            for key in ("input_color_space", "input_video_range"):
                value = override.get(key)
                if value:
                    camera[key] = value
                else:
                    camera.pop(key, None)
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
        codec = str(self.output_codec.currentData())
        if codec not in GUI_MASTER_BIT_DEPTHS:
            raise ValueError("Master output must use a supported 10-bit or 12-bit codec")
        video.update(
            {
                "fps": self.fps.value(),
                "frames": self.frame_limit.value() or None,
                "output_codec": codec,
            }
        )
        return raw

    def _write_working_config(self) -> Path:
        if self.process is not None:
            raise ValueError("Another task is already running")
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
        if self._import_dialog is None:
            dialog = QFileDialog(self)
            dialog.setWindowTitle("Select P01-P03 or P01-P05 camera plates")
            dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            dialog.setNameFilter(VIDEO_FILTER)
            dialog.setOption(QFileDialog.Option.DontUseNativeDialog, True)
            self._import_dialog = dialog
        initial_dir = str(self.settings.value("lastImportDir", ""))
        if not Path(initial_dir).is_dir():
            initial_dir = QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation
            )
        if initial_dir:
            self._import_dialog.setDirectory(initial_dir)
        if self._import_dialog.exec() != QDialog.DialogCode.Accepted:
            self._import_dialog.hide()
            return
        files = self._import_dialog.selectedFiles()
        self._import_dialog.hide()
        if files:
            self.settings.setValue("lastImportDir", str(Path(files[0]).parent))
            try:
                self._set_video_sources(files)
                self._analyze_imported_sources()
            except Exception as error:
                self._error("Import plates", str(error))

    def clear_sources(self) -> None:
        self.source_table.set_paths([""] * self.source_table.camera_count())
        self._plate_numbers = None
        self._source_probes = None
        self._source_overrides = {}
        self._reset_timing()

    def _cleanup_reference_dir(self, path: Path | None) -> None:
        if path is None:
            return
        try:
            resolved = path.resolve()
            if (
                resolved.parent == self._working_dir.resolve()
                and resolved.name.startswith("reference-")
            ):
                shutil.rmtree(resolved, ignore_errors=True)
        except OSError:
            pass

    def _reset_timing(self) -> None:
        self._cleanup_reference_dir(self._last_reference_dir)
        self._tc_alignment = None
        self._tc_alignment_path = None
        self._last_reference_dir = None
        self._last_reference_config_path = None
        self._preview_ready = False
        self._preview_in_progress = False
        self._pending_scrub_frame = None
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
        self.timeline_playhead.setRange(0, 0)
        self.timeline_playhead.setValue(0)
        self.timeline_playhead.setEnabled(False)
        self.playhead_time.setText("00:00:00.000")
        self.frame_limit.setEnabled(True)
        self.frame_limit.setValue(self._configured_frame_limit)
        self.timeline_duration.setText("0 frames")
        self.timing_status.setText("TC Align finds the shortest common range across every camera")
        self.rig_align_button.setEnabled(False)
        self.preview_note.setText("Fitted preview · UHD 4K max · master render stays full resolution")
        self._update_source_status()
        self._timeline_updating = False

    def _set_timeline_range(self, lower: int, upper: int) -> None:
        if not self._tc_alignment:
            return
        maximum = self._timeline_maximum
        lower = max(0, min(int(lower), maximum - 1))
        upper = max(lower + 1, min(int(upper), maximum))
        playhead = max(lower, min(self.timeline_playhead.value(), upper - 1))
        self._timeline_updating = True
        self.timeline_bar.set_frame_range(maximum, lower, upper, playhead)
        self.timeline_in.setValue(lower)
        self.timeline_out.setValue(upper)
        self.timeline_playhead.setRange(lower, upper - 1)
        self.timeline_playhead.setValue(playhead)
        duration = upper - lower
        self.frame_limit.setValue(duration)
        fps = float(self._tc_alignment["fps"])
        self.timeline_duration.setText(
            f"{duration:,} frames  ·  {duration / fps:,.2f} sec"
        )
        self._update_playhead_time(playhead)
        self._timeline_updating = False

    def _timeline_bar_changed(self, lower: int, upper: int) -> None:
        if not self._timeline_updating:
            self._set_timeline_range(lower, upper)

    def _timeline_spin_changed(self) -> None:
        if not self._timeline_updating:
            self._set_timeline_range(self.timeline_in.value(), self.timeline_out.value())

    def _update_playhead_time(self, frame: int) -> None:
        fps = float(self._tc_alignment["fps"]) if self._tc_alignment else self.fps.value()
        seconds = frame / max(fps, 0.001)
        hours = int(seconds // 3600)
        minutes = int(seconds % 3600 // 60)
        remaining = seconds % 60
        self.playhead_time.setText(f"{hours:02d}:{minutes:02d}:{remaining:06.3f}")

    def _set_playhead(self, frame: int) -> None:
        if not self._tc_alignment:
            return
        lower, upper = self.timeline_bar.values()
        frame = max(lower, min(int(frame), upper - 1))
        self._timeline_updating = True
        self.timeline_playhead.setValue(frame)
        self.timeline_bar.set_playhead(frame)
        self._update_playhead_time(frame)
        self._timeline_updating = False

    def _timeline_playhead_changed(self, frame: int) -> None:
        if not self._timeline_updating:
            self._set_playhead(frame)

    def _timeline_playhead_spin_changed(self, frame: int) -> None:
        if not self._timeline_updating:
            self._set_playhead(frame)

    def _timeline_playhead_released(self, frame: int) -> None:
        self._set_playhead(frame)
        self._scrub_preview()

    def _scrub_preview(self) -> None:
        if not self._tc_alignment:
            return
        if self._preview_in_progress:
            self._pending_scrub_frame = self.timeline_playhead.value()
            self.preview_note.setText("Preview queued for the latest playhead frame")
            return
        if self._preview_ready and self.process is None:
            self.create_preview()

    def _finish_preview_frame(self, rendered_frame: int) -> None:
        self._preview_in_progress = False
        pending = self._pending_scrub_frame
        self._pending_scrub_frame = None
        if pending is not None and pending != rendered_frame:
            self._set_playhead(pending)
            self.create_preview()

    def _reset_timeline_range(self) -> None:
        if self._tc_alignment:
            self._set_timeline_range(0, self._timeline_maximum)

    def _effective_common_frames(self, payload: dict[str, object]) -> int:
        inputs = payload.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != self.source_table.camera_count():
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
        self.timeline_playhead.setEnabled(True)
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
        if len(paths) not in SUPPORTED_CAMERA_COUNTS or any(not path for path in paths):
            raise ValueError("Select all 3 or all 5 camera plates")
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise ValueError("Missing video: " + missing[0])
        return paths

    def check_inputs(self) -> None:
        self._analyze_imported_sources()

    def _analyze_imported_sources(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
        except Exception as error:
            self._error("Input check", str(error))
            return
        report = self._working_dir / "input-probe.json"
        report.unlink(missing_ok=True)

        def apply_probe() -> None:
            try:
                self._apply_source_probe_payload(
                    json.loads(report.read_text(encoding="utf-8"))
                )
            except Exception as error:
                self._error("Input analysis", str(error))

        self._run_cli(
            "ANALYZE INPUTS",
            [
                "probe-inputs",
                "--allow-low-bit-depth",
                "--config",
                str(config),
                "--output",
                str(report),
                *sources,
            ],
            apply_probe,
            lambda: self.source_status.setText("Source analysis failed · open Jobs"),
        )

    def _apply_source_probe_payload(self, payload: dict[str, object]) -> None:
        inputs = payload.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != self.source_table.camera_count():
            raise ValueError("invalid source probe report")
        probes = [dict(item) for item in inputs if isinstance(item, dict)]
        if len(probes) != len(inputs):
            raise ValueError("invalid source probe entries")
        self._source_probes = probes
        self.source_table.set_probe_data(probes, self._source_overrides)
        self._update_source_status()
        minimum = min(int(probe["bit_depth"]) for probe in probes)
        if minimum < 10:
            self.preview_note.setText(
                f"SOURCE {minimum}-bit · preview allowed · master encoded at 10/12-bit"
            )
        else:
            self.preview_note.setText(
                f"SOURCE {minimum}-bit detected · 10/12-bit master pipeline ready"
            )

    def _open_input_settings(self, rows: list[int]) -> None:
        valid_rows = sorted(
            row for row in rows if 0 <= row < self.source_table.camera_count()
        )
        if not valid_rows:
            return
        paths = self.source_table.paths()
        selected_paths = [paths[row] for row in valid_rows if paths[row]]
        if not selected_paths:
            self._error("Input settings", "Import a plate first")
            return
        probes = [
            self._source_probes[row]
            if self._source_probes is not None and row < len(self._source_probes)
            else None
            for row in valid_rows
            if paths[row]
        ]
        overrides = [
            self._source_overrides.get(
                path,
                {"input_color_space": None, "input_video_range": None},
            )
            for path in selected_paths
        ]
        dialog = InputSettingsDialog(self, selected_paths, probes, overrides)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        values = dialog.values()
        for path in selected_paths:
            self._source_overrides[path] = dict(values)
        if self._source_probes:
            self.source_table.set_probe_data(
                self._source_probes, self._source_overrides
            )
        self._cleanup_reference_dir(self._last_reference_dir)
        self._last_reference_dir = None
        self._last_reference_config_path = None
        self._preview_ready = False
        self._preview_in_progress = False
        self._pending_scrub_frame = None
        self.rig_align_button.setEnabled(False)
        self._update_source_status()
        self.preview_note.setText(
            "Input interpretation updated · create preview to refresh stitched image"
        )
        self.statusBar().showMessage(
            f"Input settings applied to {len(selected_paths)} clip(s)", 8000
        )

    def _write_preview_config(
        self,
        source: Path,
        destination: Path,
        width: int,
        height: int,
    ) -> float:
        raw = json.loads(source.read_text(encoding="utf-8"))
        output = raw["output"]
        camera_scales = [
            min(3840 / int(camera["width"]), 2160 / int(camera["height"]))
            for camera in raw["cameras"]
        ]
        scale = min(
            1.0,
            width / int(output["width"]),
            height / int(output["height"]),
            *camera_scales,
        )
        output["width"] = width
        output["height"] = height
        output["tile_width"] = min(int(output.get("tile_width", 1024)), width)
        output["tile_height"] = min(int(output.get("tile_height", 512)), height)
        for camera in raw["cameras"]:
            camera["width"] = max(1, int(round(int(camera["width"]) * scale)))
            camera["height"] = max(1, int(round(int(camera["height"]) * scale)))
            lens = camera["lens"]
            for key in ("fx", "fy", "cx", "cy"):
                lens[key] = float(lens[key]) * scale
            if lens.get("circle_radius") is not None:
                lens["circle_radius"] = float(lens["circle_radius"]) * scale
        flow = raw.get("flow")
        if isinstance(flow, dict) and flow.get("max_displacement_px") is not None:
            flow["max_displacement_px"] = max(
                1.0, float(flow["max_displacement_px"]) * scale
            )
        destination.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        return scale

    def create_preview(self) -> None:
        try:
            config = self._write_working_config()
            sources = self._validate_sources()
            if self._tc_alignment:
                reference_frame = self.timeline_playhead.value()
                if not self.timeline_in.value() <= reference_frame < self.timeline_out.value():
                    raise ValueError("Playhead is outside the selected SHARED TIMELINE range")
        except Exception as error:
            self._error("Preview", str(error))
            return
        stamp = f"{time.time_ns()}-{os.getpid()}"
        reference = self._working_dir / f"reference-{stamp}"
        try:
            reference.mkdir(parents=True, exist_ok=False)
            width, height = preview_dimensions(
                self.canvas_width.value(), self.canvas_height.value()
            )
            preview_config = reference / "preview-config.json"
            preview_scale = self._write_preview_config(
                config,
                preview_config,
                width,
                height,
            )
        except Exception as error:
            self._cleanup_reference_dir(reference)
            self._error("Preview setup", str(error))
            return
        previous_reference = self._last_reference_dir
        self._preview_in_progress = True
        self.preview.show_message("EXTRACTING SYNCHRONIZED REFERENCE FRAMES …")
        timeline_start = self.timeline_playhead.value() if self._tc_alignment else 0
        reference_time = 0.0

        def preview_failed() -> None:
            self._preview_in_progress = False
            self._pending_scrub_frame = None
            self._cleanup_reference_dir(reference)
            previous_preview = (
                previous_reference / "stitched-preview.png"
                if previous_reference is not None
                else None
            )
            if previous_preview is not None and previous_preview.is_file():
                try:
                    self.preview.set_array(read_image(previous_preview))
                    self.preview_note.setText("Previous preview restored · check Jobs for the error")
                    return
                except Exception:
                    pass
            self.preview.show_message("PREVIEW FAILED · OPEN JOBS FOR DETAILS")

        def stitch_reference() -> None:
            raw = json.loads(preview_config.read_text(encoding="utf-8"))
            names = [camera["name"] for camera in raw["cameras"]]
            images = [str(reference / f"{name}.png") for name in names]
            preview_path = reference / "stitched-preview.png"

            def load_preview() -> None:
                try:
                    self.preview.set_array(read_image(preview_path))
                    self._last_reference_dir = reference
                    self._last_reference_config_path = preview_config
                    self._preview_ready = True
                    self._cleanup_reference_dir(previous_reference)
                    self.rig_align_button.setEnabled(True)
                    self.preview_note.setText(
                        "Release the playhead to refresh this fitted 4K preview · no crop"
                    )
                    self.statusBar().showMessage(
                        f"Preview ready: {width}×{height} · fitted, no crop",
                        10000,
                    )
                    self._finish_preview_frame(timeline_start)
                except Exception as error:
                    preview_failed()
                    self._error("Preview load", str(error))

            self.preview.show_message("STITCHING 16-BIT PREVIEW …")
            self._run_cli(
                "STITCH PREVIEW",
                [
                    "stitch-frame",
                    "--config",
                    str(preview_config),
                    "--output",
                    str(preview_path),
                    *images,
                ],
                load_preview,
                preview_failed,
            )

        arguments = [
            "extract-reference",
            "--allow-low-bit-depth",
            "--config",
            str(config),
            "--time",
            str(reference_time),
            "--start-frame",
            str(timeline_start),
            "--scale",
            f"{preview_scale:.9f}",
            "--output-dir",
            str(reference),
        ]
        if self._tc_alignment_path:
            arguments.extend(["--alignment-plan", str(self._tc_alignment_path)])
        arguments.extend(sources)
        self._run_cli("EXTRACT REFERENCES", arguments, stitch_reference, preview_failed)

    def auto_align(self) -> None:
        if self._last_reference_dir is None or self._last_reference_config_path is None:
            self._error("Auto align", "Create a preview/reference frame first")
            return
        try:
            config = self._write_working_config()
            calibration_config = self._last_reference_config_path
            raw = json.loads(calibration_config.read_text(encoding="utf-8"))
            images = [
                str(self._last_reference_dir / f"{camera['name']}.png")
                for camera in raw["cameras"]
            ]
            if any(not Path(path).is_file() for path in images):
                raise ValueError("Reference frames are missing; create preview again")
        except Exception as error:
            self._error("Auto align", str(error))
            return
        calibration_output = self._working_dir / "calibrated-preview-rig.json"
        output = self._working_dir / "calibrated-rig.json"
        report = self._working_dir / "alignment-report.json"

        def load_alignment() -> None:
            full_raw = json.loads(config.read_text(encoding="utf-8"))
            solved_raw = json.loads(calibration_output.read_text(encoding="utf-8"))
            solved = {camera["name"]: camera for camera in solved_raw["cameras"]}
            for camera in full_raw["cameras"]:
                rotation = solved[camera["name"]]
                for key in ("yaw_deg", "pitch_deg", "roll_deg"):
                    camera[key] = rotation[key]
            output.write_text(json.dumps(full_raw, indent=2), encoding="utf-8")
            current_paths = self.source_table.paths()
            plate_numbers = self._plate_numbers
            source_probes = self._source_probes
            source_overrides = self._source_overrides
            tc_alignment = self._tc_alignment
            tc_alignment_path = self._tc_alignment_path
            timeline_range = self.timeline_bar.values()
            self.load_config(output)
            self.source_table.set_paths(current_paths)
            self._plate_numbers = plate_numbers
            self._source_probes = source_probes
            self._source_overrides = source_overrides
            if source_probes:
                self.source_table.set_probe_data(source_probes, source_overrides)
            self._update_source_status()
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
                str(calibration_config),
                "--output",
                str(calibration_output),
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
            if codec not in GUI_MASTER_BIT_DEPTHS:
                raise ValueError("Choose a 10-bit or 12-bit master codec")
            if codec.endswith("sequence") and Path(output).exists() and any(Path(output).iterdir()):
                raise ValueError("Sequence output directory must be empty")
        except Exception as error:
            self._error("Render", str(error))
            return
        message = (
            f"Start {self.canvas_width.value()}×{self.canvas_height.value()} render?\n\n"
            f"Codec: {self.output_codec.currentText()}\nOutput: {output}"
        )
        if self._source_probes:
            minimum = min(int(probe["bit_depth"]) for probe in self._source_probes)
            message += (
                f"\n\nDetected source: {minimum}-bit → "
                f"master: {GUI_MASTER_BIT_DEPTHS[codec]}-bit"
            )
            if minimum < 10:
                message += (
                    "\nSource detail remains 8-bit; the master codec cannot recreate "
                    "missing precision."
                )
        if self._show_message(
            QMessageBox.Icon.Question,
            "Start render",
            message,
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        arguments = [
            "stitch-video",
            "--allow-low-bit-depth",
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
            lambda: self._show_message(
                QMessageBox.Icon.Information,
                "Render complete",
                f"Output written to:\n{output}",
            ),
        )

    def _run_cli(
        self,
        task: str,
        arguments: list[str],
        success: Callable[[], None] | None = None,
        failure: Callable[[], None] | None = None,
    ) -> None:
        if self._closing:
            return
        if self.process is not None:
            self._error("Busy", "Another task is already running")
            return
        self._process_success = success
        self._process_failure = failure
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self._working_dir))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process)
        self.process.finished.connect(self._process_finished)
        self.task_label.setText(task)
        self.status_pill.setText(task)
        self.cancel_button.setVisible(True)
        self._set_busy_ui(True)
        self.progress.setRange(0, 0)
        self._append_log("\n▶ " + task)
        self._append_log("  vpstitch " + " ".join(f'"{arg}"' if " " in arg else arg for arg in arguments))
        if getattr(sys, "frozen", False):
            helper_name = "vpstitch-cli.exe" if os.name == "nt" else "vpstitch-cli"
            cli_program = Path(sys.executable).with_name(helper_name)
            if not cli_program.is_file():
                self.process = None
                self._process_failure = None
                self._set_busy_ui(False)
                self._error("Packaged CLI missing", f"Expected bundled helper at:\n{cli_program}")
                if failure:
                    failure()
                return
            self.process.start(str(cli_program), arguments)
        else:
            self.process.start(sys.executable, ["-m", "vpstitch.cli", *arguments])

    def _set_busy_ui(self, busy: bool) -> None:
        self.import_button.setEnabled(not busy)
        self.clear_button.setEnabled(not busy)
        self.source_table.setEnabled(not busy)
        self.settings_tabs.setEnabled(not busy)
        self.tc_align_button.setEnabled(not busy)
        self.preview_button.setEnabled(not busy)
        self.render_button.setEnabled(not busy)
        self.rig_align_button.setEnabled(not busy and self._preview_ready)
        self.timeline_in.setEnabled(not busy and self._tc_alignment is not None)
        self.timeline_out.setEnabled(not busy and self._tc_alignment is not None)
        self.reset_timeline_button.setEnabled(not busy and self._tc_alignment is not None)

    def _read_process(self) -> None:
        process = self.sender()
        if self.process is None or (process is not None and process is not self.process):
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        normalized = text.replace("\r", "\n")
        progress_value: tuple[int, int] | None = None
        frame_value: str | None = None
        for line in normalized.splitlines():
            if line.strip():
                self._append_log(line)
            match = re.search(r"tiles\s+(\d+)/(\d+)", line)
            if match:
                progress_value = int(match.group(1)), int(match.group(2))
            frame = re.search(r"frame\s+(\d+)", line)
            if frame:
                frame_value = frame.group(1)
        if progress_value is not None:
            done, total = progress_value
            self.progress.setRange(0, total)
            self.progress.setValue(done)
        if frame_value is not None:
            self.task_label.setText(f"FRAME {frame_value}")

    def _process_finished(self, exit_code: int, _status) -> None:  # type: ignore[no-untyped-def]
        sender = self.sender()
        if self.process is None or (sender is not None and sender is not self.process):
            return
        callback = self._process_success
        failure = self._process_failure
        process = self.process
        task = self.task_label.text()
        self.process = None
        self._process_success = None
        self._process_failure = None
        try:
            process.readyReadStandardOutput.disconnect(self._read_process)
            process.finished.disconnect(self._process_finished)
        except (RuntimeError, TypeError):
            pass
        if self._closing:
            process.deleteLater()
            return
        self.progress.setRange(0, 100)
        self.progress.setValue(100 if exit_code == 0 else 0)
        self.task_label.setText("IDLE" if exit_code == 0 else "FAILED")
        self.status_pill.setText("READY" if exit_code == 0 else "FAILED")
        self.cancel_button.setVisible(False)
        self._set_busy_ui(False)
        if process is not None:
            process.deleteLater()
        if exit_code == 0:
            self._append_log(f"✓ {task} complete")
            if callback:
                callback()
        else:
            self._append_log(f"✕ {task} failed with exit code {exit_code}")
            self.statusBar().showMessage("Task failed — see Task Log", 10000)
            if failure:
                failure()

    def cancel_task(self) -> None:
        if self.process is not None:
            self._append_log("Cancelling task …")
            self.status_pill.setText("CANCELLING")
            self.process.kill()

    def _append_log(self, text: str) -> None:
        self._pending_log_lines.append(text)
        if not self._log_flush_timer.isActive():
            self._log_flush_timer.start()

    def _flush_log(self) -> None:
        if not self._pending_log_lines:
            return
        lines, self._pending_log_lines = self._pending_log_lines, []
        self.log.appendPlainText("\n".join(lines))
        scrollbar = self.log.verticalScrollBar()
        scrollbar.setValue(scrollbar.maximum())

    def _update_color_controls(self) -> None:
        enabled = self.color_mode.currentData() == "ocio"
        for widget in (self.ocio_config, self.input_space, self.working_space, self.output_space):
            widget.setEnabled(enabled)

    def _update_output_hint(self) -> None:
        codec = str(self.output_codec.currentData())
        hints = {
            "dpx12-sequence": "VFX/Resolve 교환용 12-bit RGB DPX 시퀀스. 출력 폴더가 필요합니다.",
            "prores-4444": "편집 호환용 10-bit YUV. 16-bit RGB 보존 마스터는 아닙니다.",
            "prores-hq": "편집용 10-bit 4:2:2. 크로마 해상도가 줄어듭니다.",
            "h264-mp4-10": "검수용 10-bit H.264 MP4. 15K 마스터 대신 축소 프리뷰에 권장합니다.",
            "hevc-444-10": "납품/검수용. 15K 이상의 표준 HEVC picture level 한계에 주의하십시오.",
        }
        self.output_hint.setText(hints.get(codec, ""))

    def _error(self, title: str, message: str) -> None:
        self._append_log(f"ERROR: {message}")
        self._show_message(QMessageBox.Icon.Critical, title, message)

    def _show_message(
        self,
        icon: QMessageBox.Icon,
        title: str,
        message: str,
        buttons: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
        default: QMessageBox.StandardButton = QMessageBox.StandardButton.Ok,
    ) -> QMessageBox.StandardButton:
        if self._message_box is None:
            self._message_box = QMessageBox(self)
        dialog = self._message_box
        dialog.setIcon(icon)
        dialog.setWindowTitle(title)
        dialog.setText(message)
        dialog.setStandardButtons(buttons)
        if buttons & default:
            dialog.setDefaultButton(default)
        result = QMessageBox.StandardButton(dialog.exec())
        dialog.hide()
        return result

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.mimeData().hasUrls():
            event.acceptProposedAction()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if self.process is not None:
            self.statusBar().showMessage("Finish or cancel the current task before changing media", 8000)
            event.ignore()
            return
        paths = [url.toLocalFile() for url in event.mimeData().urls() if url.isLocalFile()]
        configs = [path for path in paths if Path(path).suffix.lower() == ".json"]
        videos = [path for path in paths if Path(path).suffix.lower() in {".mov", ".mp4", ".mkv", ".avi", ".mxf"}]
        if len(configs) == 1:
            self.load_config(Path(configs[0]))
        if videos:
            try:
                self._set_video_sources(videos)
                self._analyze_imported_sources()
            except Exception as error:
                self._error("Drop videos", str(error))
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self.process is not None:
            answer = self._show_message(
                QMessageBox.Icon.Question,
                "Task running",
                "Cancel the running task and close?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._closing = True
            self._log_flush_timer.stop()
            self._pending_log_lines.clear()
            process = self.process
            self.process = None
            self._process_success = None
            self._process_failure = None
            try:
                process.readyReadStandardOutput.disconnect(self._read_process)
                process.finished.disconnect(self._process_finished)
            except (RuntimeError, TypeError):
                pass
            process.kill()
            process.waitForFinished(3000)
            process.deleteLater()
        else:
            self._closing = True
            self._log_flush_timer.stop()
            self._pending_log_lines.clear()
        event.accept()


def _configure_application_attributes() -> None:
    if sys.platform == "darwin":
        QCoreApplication.setAttribute(
            Qt.ApplicationAttribute.AA_DontUseNativeDialogs,
            True,
        )


def main() -> int:
    try:
        _configure_application_attributes()
        app = QApplication.instance() or QApplication(sys.argv)
        _stabilize_macos_accessibility_bridge()
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
