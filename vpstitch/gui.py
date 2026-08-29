from __future__ import annotations

import ctypes
import difflib
import hashlib
import json
import os
import re
import shutil
import sys
import time
import traceback
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Callable

import numpy as np
from PySide6.QtCore import (
    QCoreApplication,
    QObject,
    QPointF,
    QProcess,
    QSettings,
    QSize,
    QStandardPaths,
    QMimeData,
    Qt,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtGui import (
    QAction,
    QColor,
    QCloseEvent,
    QDrag,
    QFont,
    QImage,
    QKeySequence,
    QMouseEvent,
    QPainter,
    QPixmap,
    QPen,
    QShortcut,
    QWheelEvent,
)
from PySide6.QtMultimedia import QMediaPlayer
from PySide6.QtMultimediaWidgets import QVideoWidget
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
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
    QInputDialog,
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
    QStackedWidget,
    QStatusBar,
    QStyle,
    QTabWidget,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QTreeWidgetItemIterator,
    QVBoxLayout,
    QWidget,
)

from .ffmpegio import probe_video
from .imageio import read_image
from .interactive import InteractivePreviewRenderer
from .autostitch import (
    apply_auto_stitch_solution,
    prepare_auto_stitch_config,
    update_fine_tune_metadata,
)

from .canvas import recommend_full_plate_canvas
from .color import BUNDLED_ACES_STUDIO_ID, load_ocio_config
from .config import (
    MAX_CANVAS_HEIGHT,
    MAX_CANVAS_WIDTH,
    load_config as parse_config,
    repair_legacy_p3_pq_target,
)
from .project import (
    Bin,
    MediaCacheStatus,
    MediaRecord,
    PlaybackCacheStatus,
    ProjectError,
    ProjectStore,
    StitchStatus,
    TimelineRecord,
)
from .renderqueue import RenderJob, RenderQueueError, RenderQueueStore, RenderStatus
from .sourcecache import (
    SourceProxyCommand,
    SourceProxyPlan,
    finalize_source_proxy,
    plan_source_proxy,
    source_proxy_commands,
    source_proxy_ready,
)
from .liveplayback import AlignedFramePlan, LivePlaybackSession


APP_NAME = "VP Stitch"
BUILTIN_ACES_STUDIO = BUNDLED_ACES_STUDIO_ID
VIDEO_FILTER = "Video files (*.mov *.mp4 *.mkv *.avi *.mxf);;All files (*.*)"
SUPPORTED_CAMERA_COUNTS = (3, 5)
AUTOSAVE_INTERVAL_MS = 10 * 60 * 1000
_MEDIA_BIN_UNSET = object()
TIMELINE_CLIPBOARD_MIME = "application/x-vpstitch-timeline"
FPS_MODE_MATCH_SOURCE = "match_source"
FPS_MODE_CUSTOM = "custom"
FPS_MATCH_TOLERANCE = 0.001
STANDARD_FRAME_RATES = (
    24_000 / 1_001,
    24.0,
    25.0,
    30_000 / 1_001,
    30.0,
    48.0,
    50.0,
    60_000 / 1_001,
    60.0,
)
PLATE_NUMBERS_BY_COUNT = {
    3: (6, 7, 8),
    5: (1, 2, 3, 4, 5),
}
_MACOS_AX_SHIM: ctypes.CDLL | None = None
GUI_MASTER_BIT_DEPTHS = {
    "prores-hq": 10,
    "prores-4444": 10,
    "h264-mp4-10": 10,
    "hevc-444-10": 10,
    "dpx12-sequence": 12,
}
GUI_MASTER_CODEC_OPTIONS = (
    ("ProRes HQ · 10-bit 4:2:2", "prores-hq"),
    ("DPX · 12-bit RGB sequence", "dpx12-sequence"),
    ("H.264 MP4 · 10-bit 4:2:0", "h264-mp4-10"),
    ("ProRes 4444 · 10-bit YUV", "prores-4444"),
    ("HEVC · 10-bit 4:4:4", "hevc-444-10"),
)
GUI_MASTER_CODEC_LABELS = {
    value: label for label, value in GUI_MASTER_CODEC_OPTIONS
}
QUEUE_CODEC_LABELS = {
    "prores-hq": "PRORES HQ 10b",
    "dpx12-sequence": "DPX 12b",
    "h264-mp4-10": "H.264 MP4 10b",
    "prores-4444": "PRORES 4444 10b",
    "hevc-444-10": "HEVC 444 10b",
}
OUTPUT_SUFFIX_BY_CODEC = {
    "ffv1-16": ".mkv",
    "exr-half-sequence": "",
    "prores-hq": ".mov",
    "prores-4444": ".mov",
    "h264-mp4-10": ".mp4",
    "h264-proxy": ".mp4",
    "hevc-444-10": ".mkv",
    "dpx12-sequence": "",
}
KNOWN_OUTPUT_SUFFIXES = (".mov", ".mp4", ".mkv", ".dpx", ".exr")
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
VIEWER_MONITOR_TRANSFORMS = {
    "sdr-rec709": (
        "Standard Rec.709",
        "sRGB - Display",
        "ACES 2.0 - SDR 100 nits (Rec.709)",
    ),
}

DELIVERY_DISPLAY_LABELS = {
    "sRGB - Display": "Rec.709 SDR",
    "ST2084-P3-D65 - Display": "P3-D65 PQ",
    "Rec.2100-PQ - Display": "Rec.2020 PQ",
    "Rec.2100-HLG - Display": "Rec.2020 HLG",
    "Display P3 HDR - Display": "Apple Display P3 HDR · EDR, not PQ",
}
DELIVERY_DISPLAY_PRIORITY = tuple(DELIVERY_DISPLAY_LABELS)
PREFERRED_OUTPUT_COLOR_SPACES = (
    "V-Log V-Gamut",
    "Gamma 2.4 Encoded Rec.709",
    "Gamma 2.2 Encoded Rec.709",
    "ACEScct",
    "ACES2065-1",
)


def _ordered_output_spaces(spaces: tuple[str, ...]) -> tuple[str, ...]:
    preferred = tuple(value for value in PREFERRED_OUTPUT_COLOR_SPACES if value in spaces)
    return preferred + tuple(value for value in spaces if value not in preferred)


def _delivery_display_value(combo: QComboBox) -> str:
    return str(combo.currentData() or combo.currentText()).strip()


def _populate_delivery_display_combo(
    combo: QComboBox,
    displays: tuple[str, ...],
    current: str,
) -> str:
    ordered = tuple(value for value in DELIVERY_DISPLAY_PRIORITY if value in displays)
    ordered += tuple(value for value in displays if value not in ordered)
    resolved = _resolve_ocio_choice(ordered, current, DELIVERY_DISPLAY_PRIORITY)
    combo.blockSignals(True)
    combo.clear()
    for value in ordered:
        combo.addItem(DELIVERY_DISPLAY_LABELS.get(value, value), value)
    combo.setCurrentIndex(max(0, combo.findData(resolved)))
    combo.blockSignals(False)
    return resolved


def _display_view_video_tags(display: str, view: str) -> dict[str, str]:
    """Return file metadata for known display encodings, never for monitor-only EDR."""
    display_key = display.casefold()
    view_key = view.casefold()
    is_pq = "st2084" in display_key or "rec.2100-pq" in display_key
    is_hlg = "rec.2100-hlg" in display_key
    if is_pq or is_hlg:
        if "rec.2020" in view_key:
            primaries = "bt2020"
        elif "p3" in view_key:
            primaries = "smpte432"
        else:
            primaries = "smpte432" if "p3" in display_key else "bt2020"
        return {
            "color_primaries": primaries,
            "color_trc": "arib-std-b67" if is_hlg else "smpte2084",
            "colorspace": "bt2020nc",
            "color_range": "tv",
        }
    if "sdr" in view_key or "rec.709" in view_key or display == "sRGB - Display":
        return {
            "color_primaries": "bt709",
            "color_trc": "bt709",
            "colorspace": "bt709",
            "color_range": "tv",
        }
    return {}


def _canonical_frame_rate(value: float) -> float:
    """Keep common fractional rates exact while preserving unusual source rates."""

    fps = float(value)
    if not np.isfinite(fps) or fps <= 0.0:
        raise ValueError("source frame rate must be positive")
    return min(STANDARD_FRAME_RATES, key=lambda candidate: abs(candidate - fps)) \
        if min(abs(candidate - fps) for candidate in STANDARD_FRAME_RATES) <= 0.001 \
        else fps


def _format_frame_rate(value: object) -> str:
    try:
        fps = _canonical_frame_rate(float(value))
    except (TypeError, ValueError):
        return "UNKNOWN"
    if abs(fps - round(fps)) <= 1e-6:
        return f"{fps:.3f}"
    return f"{fps:.3f}"


def _matching_source_frame_rate(probes: list[dict[str, object]]) -> float | None:
    rates = [
        _canonical_frame_rate(float(probe["fps"]))
        for probe in probes
        if probe.get("fps") is not None
    ]
    if not rates:
        return None
    reference = rates[0]
    mismatches = [rate for rate in rates[1:] if abs(rate - reference) > FPS_MATCH_TOLERANCE]
    if mismatches:
        values = ", ".join(_format_frame_rate(rate) for rate in rates)
        raise ValueError(f"Plate frame rates do not match: {values}")
    return reference


def _frame_rate_mode(config: dict[str, object]) -> str:
    metadata = config.get("_vpstitch")
    if not isinstance(metadata, dict):
        return FPS_MODE_MATCH_SOURCE
    value = str(metadata.get("fps_mode") or FPS_MODE_MATCH_SOURCE)
    return value if value in {FPS_MODE_MATCH_SOURCE, FPS_MODE_CUSTOM} else FPS_MODE_MATCH_SOURCE


def resolved_render_output(
    folder: str | Path,
    base_name: str,
    codec: str,
) -> Path:
    """Return one canonical output path from a folder, name, and codec."""
    folder_text = str(folder).strip()
    if not folder_text:
        raise ValueError("Choose an output folder")
    directory = Path(folder_text).expanduser()
    name = str(base_name).strip()
    if not name or name in {".", ".."}:
        raise ValueError("Enter an output name")
    if "/" in name or "\\" in name:
        raise ValueError("Output name cannot contain folder separators")
    if codec not in OUTPUT_SUFFIX_BY_CODEC:
        raise ValueError(f"Unsupported output codec: {codec}")
    lowered = name.lower()
    removed = True
    while removed:
        removed = False
        for suffix in KNOWN_OUTPUT_SUFFIXES:
            if lowered.endswith(suffix):
                name = name[: -len(suffix)].rstrip(" .")
                lowered = name.lower()
                removed = True
                break
    if not name:
        raise ValueError("Enter an output name")
    return directory / f"{name}{OUTPUT_SUFFIX_BY_CODEC[codec]}"


def split_render_output(path: str | Path, codec: str) -> tuple[Path, str]:
    output = Path(path)
    suffix = OUTPUT_SUFFIX_BY_CODEC.get(codec, "")
    name = output.name
    if suffix and name.lower().endswith(suffix):
        name = name[: -len(suffix)]
    return output.parent, name
_EXPLICIT_PLATE_NUMBER = re.compile(
    r"(?:^|[^a-z0-9])(?:p(?:late)?|cam(?:era)?)[ ._-]*0?([1-8])(?=$|[^0-9])",
    re.IGNORECASE,
)
_BARE_PLATE_NUMBER = re.compile(r"(?:^|[^0-9])0([1-8])(?=$|[^0-9])")


def plate_number(path: str | Path) -> int | None:
    """Read a P01-P08 camera number from a clip or parent folder name."""
    source = Path(path)
    components = [source.stem, *(parent.name for parent in source.parents[:3])]
    for pattern in (_EXPLICIT_PLATE_NUMBER, _BARE_PLATE_NUMBER):
        for component in components:
            match = pattern.search(component)
            if match:
                return int(match.group(1))
    return None


def order_camera_plates(paths: list[str]) -> tuple[list[str], list[int] | None]:
    """Validate P06-P08 or P01-P05 sets and return them in camera order."""
    if len(paths) not in SUPPORTED_CAMERA_COUNTS:
        raise ValueError("Select either 3 plates (P06-P08) or 5 plates (P01-P05)")

    detected = [plate_number(path) for path in paths]
    if all(number is None for number in detected):
        natural = lambda value: [
            int(token) if token.isdigit() else token.casefold()
            for token in re.split(r"(\d+)", Path(value).name)
        ]
        return sorted(paths, key=natural), None
    if any(number is None for number in detected):
        raise ValueError("Some plate numbers are missing. Name every clip P06-P08 or P01-P05")

    numbers = [int(number) for number in detected if number is not None]
    expected = list(PLATE_NUMBERS_BY_COUNT[len(paths)])
    if sorted(numbers) != expected:
        expected_text = ", ".join(f"P{number:02d}" for number in expected)
        raise ValueError(f"Plate names must contain each of {expected_text} exactly once")
    ordered = sorted(zip(numbers, paths, strict=True), key=lambda item: item[0])
    return [path for _, path in ordered], [number for number, _ in ordered]


def suggest_camera_assignment(
    paths: list[str], camera_count: int
) -> tuple[list[str], bool]:
    """Return a deterministic slot order and whether operator confirmation is needed."""
    if camera_count not in SUPPORTED_CAMERA_COUNTS or len(paths) != camera_count:
        raise ValueError(f"Select exactly {camera_count} clips for this timeline")
    expected = PLATE_NUMBERS_BY_COUNT[camera_count]
    detected = [plate_number(path) for path in paths]
    if sorted(number for number in detected if number is not None) == list(expected) and all(
        number is not None for number in detected
    ):
        ordered = [
            path
            for _number, path in sorted(
                zip((int(number) for number in detected), paths, strict=True),
                key=lambda item: item[0],
            )
        ]
        return ordered, False

    slots: list[str | None] = [None] * camera_count
    used: set[str] = set()
    for path, number in zip(paths, detected, strict=True):
        if number not in expected:
            continue
        index = expected.index(int(number))
        if slots[index] is None:
            slots[index] = path
            used.add(path)
    remaining = sorted(
        (path for path in paths if path not in used),
        key=lambda value: [
            int(token) if token.isdigit() else token.casefold()
            for token in re.split(r"(\d+)", Path(value).name)
        ],
    )
    iterator = iter(remaining)
    return [value if value is not None else next(iterator) for value in slots], True


def _runtime_root() -> Path:
    """Return the directory that contains bundled read-only resources."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).resolve().parent.parent / "Resources"))
    return Path(__file__).resolve().parent.parent


def _user_data_root() -> Path:
    location = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.AppLocalDataLocation)
    return Path(location) if location else Path.home() / "Library" / "Application Support" / "VP-LAB" / APP_NAME


_STORAGE_ROOTS_KEY = "storage/authorizedRoots"
_STORAGE_SETUP_KEY = "storage/setupComplete"


def _application_settings() -> QSettings:
    """Keep automated UI tests out of the operator's real macOS preferences."""
    test_id = os.environ.get("PYTEST_CURRENT_TEST", "").strip()
    if test_id:
        settings_root = Path.cwd() / ".pytest-tmp" / "qsettings"
        settings_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(test_id.encode("utf-8")).hexdigest()[:16]
        return QSettings(
            str(settings_root / f"{digest}.ini"),
            QSettings.Format.IniFormat,
        )
    return QSettings("VP-LAB", APP_NAME)


def _setting_paths(settings: QSettings, key: str) -> list[str]:
    value = settings.value(key, [])
    if isinstance(value, str):
        return [value] if value else []
    if isinstance(value, (list, tuple)):
        return [str(path) for path in value if str(path)]
    return []


def _remember_storage_root(settings: QSettings, path: str | Path) -> Path:
    """Remember an operator-selected directory without probing protected storage."""
    root = Path(path).expanduser().resolve(strict=False)
    roots = [Path(value).expanduser().resolve(strict=False) for value in _setting_paths(settings, _STORAGE_ROOTS_KEY)]
    if any(root == existing or root.is_relative_to(existing) for existing in roots):
        return root
    roots = [existing for existing in roots if not existing.is_relative_to(root)]
    roots.append(root)
    settings.setValue(_STORAGE_ROOTS_KEY, [str(value) for value in roots])
    settings.sync()
    return root


def _preferred_storage_directory(
    settings: QSettings,
    key: str,
    fallback: str | Path,
) -> str:
    remembered = str(settings.value(key, "") or "").strip()
    if remembered:
        return remembered
    roots = _setting_paths(settings, _STORAGE_ROOTS_KEY)
    return roots[-1] if roots else str(fallback)


def preview_dimensions(
    width: int,
    height: int,
    max_width: int = 3840,
    max_height: int = 2160,
) -> tuple[int, int]:
    scale = min(1.0, max_width / width, max_height / height)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def live_playback_limits(camera_count: int) -> tuple[int, int]:
    """Return a sharp low-latency ceiling tuned for the active camera count."""
    if camera_count == 3:
        return 1920, 1080
    if camera_count == 5:
        return 1280, 720
    raise ValueError("live playback requires a 3-camera or 5-camera set")


def format_render_duration(seconds: float | None) -> str:
    if seconds is None or not np.isfinite(seconds) or seconds < 0.0:
        return "ESTIMATING"
    rounded = max(0, int(round(seconds)))
    hours, remainder = divmod(rounded, 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


def robust_render_seconds_per_frame(
    samples: list[float],
    previous: float | None = None,
) -> float | None:
    """Estimate recent sustained throughput while rejecting isolated stalls."""
    values = np.asarray(
        [value for value in samples if np.isfinite(value) and value > 0.0],
        dtype=np.float64,
    )
    if values.size == 0:
        return previous
    values = values[-15:]
    median = float(np.median(values))
    deviation = np.abs(values - median)
    mad = float(np.median(deviation))
    tolerance = max(median * 0.15, mad * 4.4478, 1.0e-6)
    inliers = values[deviation <= tolerance]
    if inliers.size == 0:
        inliers = values
    robust_center = median * 0.55 + float(np.mean(inliers)) * 0.45
    recent = values[-min(5, values.size) :]
    recent_center = float(np.median(recent))
    center = robust_center * 0.35 + recent_center * 0.65
    if previous is None or not np.isfinite(previous) or previous <= 0.0:
        return center
    # Median/MAD removes one-off stalls. Once three recent samples agree on a
    # speed change, follow it quickly enough that ETA remains useful after a
    # thermal, codec, or I/O transition.
    recent_three = values[-3:]
    sustained_change = recent_three.size == 3 and (
        bool(np.all(recent_three > previous * 1.08))
        or bool(np.all(recent_three < previous * 0.92))
    )
    bounded = float(np.clip(center, previous * 0.5, previous * 2.0))
    weight = 0.75 if sustained_change else 0.5
    return previous * (1.0 - weight) + bounded * weight


def render_progress_text(
    done: int, total: int, eta_seconds: float | None = None
) -> str:
    if total <= 0:
        return "ESTIMATING"
    bounded = min(max(0, int(done)), int(total))
    percent = 100.0 * bounded / total
    if bounded >= total:
        return "100%"
    return f"{percent:.1f}% · {format_render_duration(eta_seconds)} LEFT"


def render_queue_status_text(
    status: RenderStatus,
    progress: tuple[int, int, float | None] | None,
    *,
    elapsed_seconds: float | None = None,
    phase: str = "",
    map_progress: tuple[int, int] | None = None,
) -> str:
    if status is RenderStatus.DONE:
        duration = (
            ""
            if elapsed_seconds is None
            else f" · {format_render_duration(elapsed_seconds)}"
        )
        return "100% · DONE" + duration
    if status is not RenderStatus.RENDERING or progress is None:
        return status.value.upper()
    done, total, eta_seconds = progress
    elapsed = (
        "" if elapsed_seconds is None else f" · {format_render_duration(elapsed_seconds)} RUN"
    )
    if phase == "projection-cache":
        if map_progress is None or map_progress[1] <= 0:
            return "MAPS" + elapsed
        map_done, map_total = map_progress
        percent = 100.0 * min(max(0, map_done), map_total) / map_total
        return f"MAPS {percent:.1f}%" + elapsed
    if total <= 0:
        return "STARTING" + elapsed
    bounded = min(max(0, int(done)), int(total))
    percent = 100.0 * bounded / total
    if bounded >= total:
        return "100% · FINALIZING" + elapsed
    if eta_seconds is None:
        return f"{percent:.1f}% · EST" + elapsed
    return f"{percent:.1f}% · {format_render_duration(eta_seconds)} LEFT" + elapsed


def ocio_space_names(identifier: str) -> tuple[str, ...]:
    """Return every named color space exposed by an OCIO config."""
    config = load_ocio_config(identifier)
    return tuple(str(name) for name in config.getColorSpaceNames())


def ocio_display_views(identifier: str) -> dict[str, tuple[str, ...]]:
    """Return every display and its views exposed by an OCIO config."""
    config = load_ocio_config(identifier)
    return {
        str(display): tuple(str(view) for view in config.getViews(display))
        for display in config.getDisplays()
    }


def _populate_combo(combo: QComboBox, values: tuple[str, ...], current: str) -> None:
    requested = current.strip()
    resolved = _resolve_ocio_choice(values, requested)
    combo.blockSignals(True)
    combo.clear()
    combo.addItems(list(values))
    index = combo.findText(resolved, Qt.MatchFlag.MatchExactly)
    combo.setCurrentIndex(index if index >= 0 else (0 if values else -1))
    combo.setProperty(
        "recoveredFrom",
        requested if requested and resolved and requested != resolved else "",
    )
    combo.blockSignals(False)


def _resolve_ocio_choice(
    values: tuple[str, ...],
    requested: str,
    preferred: tuple[str, ...] = (),
) -> str:
    """Resolve a saved OCIO value without letting free-text typos reach renders."""
    if not values:
        return ""
    candidate = requested.strip()
    if candidate in values:
        return candidate
    casefolded = {value.casefold(): value for value in values}
    if candidate.casefold() in casefolded:
        return casefolded[candidate.casefold()]
    if candidate:
        close = difflib.get_close_matches(candidate, values, n=1, cutoff=0.72)
        if close:
            return close[0]
    for fallback in preferred:
        if fallback in values:
            return fallback
    return values[0]


def _new_ocio_space_combo(current: str = "", *, output: bool = False) -> QComboBox:
    combo = ChevronComboBox()
    combo.setEditable(False)
    combo.setMaxVisibleItems(18)
    combo.setMinimumContentsLength(18)
    combo.setSizeAdjustPolicy(
        QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
    )
    combo.setProperty("ocioRequested", current.strip())
    combo.setProperty("ocioPreferred", [current.strip()] if current.strip() else [])
    combo.setProperty("ocioOutput", output)
    combo.setToolTip("Select a color space read from the active OCIO config")
    return combo


def _request_ocio_combo_value(combo: QComboBox, value: str) -> None:
    requested = value.strip()
    combo.setProperty("ocioRequested", requested)
    index = combo.findText(requested, Qt.MatchFlag.MatchExactly)
    if index >= 0:
        combo.setCurrentIndex(index)


def _populate_ocio_combo(
    combo: QComboBox,
    spaces: tuple[str, ...],
    current: str,
) -> str:
    if bool(combo.property("ocioOutput")):
        spaces = _ordered_output_spaces(spaces)
    requested = current.strip() or str(combo.property("ocioRequested") or "").strip()
    preferred_value = combo.property("ocioPreferred")
    preferred = (
        tuple(str(value) for value in preferred_value)
        if isinstance(preferred_value, list)
        else ()
    )
    resolved = _resolve_ocio_choice(spaces, requested, preferred)
    combo.blockSignals(True)
    combo.clear()
    combo.addItems(list(spaces))
    index = combo.findText(resolved, Qt.MatchFlag.MatchExactly)
    combo.setCurrentIndex(index if index >= 0 else -1)
    combo.setProperty("ocioRequested", resolved)
    combo.setProperty(
        "recoveredFrom",
        requested if requested and resolved and requested != resolved else "",
    )
    combo.blockSignals(False)
    return resolved


def _load_ocio_combo_group(identifier: str, combos: tuple[QComboBox, ...]) -> int:
    spaces = ocio_space_names(identifier)
    if not spaces:
        raise ValueError("config contains no named color spaces")
    for combo in combos:
        requested = combo.currentText().strip() or str(
            combo.property("ocioRequested") or ""
        ).strip()
        _populate_ocio_combo(combo, spaces, requested)
    return len(spaces)


def _stabilize_macos_accessibility_bridge() -> bool:
    """Expose a minimal safe Cocoa AX element without entering Qt's hierarchy."""
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
        shim.vpstitch_empty_accessible_children.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        shim.vpstitch_empty_accessible_children.restype = ctypes.c_void_p
        shim.vpstitch_safe_accessibility_attribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_void_p,
        ]
        shim.vpstitch_safe_accessibility_attribute.restype = ctypes.c_void_p
        shim.vpstitch_safe_accessibility_hit_test.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.c_double,
            ctypes.c_double,
        ]
        shim.vpstitch_safe_accessibility_hit_test.restype = ctypes.c_void_p
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

        replacements = (
            (
                b"accessibilityChildren",
                shim.vpstitch_empty_accessible_children,
                b"@@:",
            ),
            # QNSView's legacy entry point builds Qt child interfaces. Crash
            # reports show stale QAccessible children being dereferenced there.
            # The shim supplies stable role/title/geometry values and an empty
            # child list instead of returning nil for every AX attribute.
            (
                b"accessibilityAttributeValue:",
                shim.vpstitch_safe_accessibility_attribute,
                b"@@:@",
            ),
            (
                b"accessibilityHitTest:",
                shim.vpstitch_safe_accessibility_hit_test,
                b"@@:{CGPoint=dd}",
            ),
        )
        for selector_name, replacement, fallback_encoding in replacements:
            selector = objc.sel_registerName(selector_name)
            method = objc.class_getInstanceMethod(qns_view, selector)
            encoding = (
                objc.method_getTypeEncoding(method) if method else fallback_encoding
            )
            objc.class_replaceMethod(
                qns_view,
                selector,
                ctypes.cast(replacement, ctypes.c_void_p),
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


class _InteractivePreviewSignals(QObject):
    finished = Signal(int, object, str)


_PREVIEW_KEY_COMMANDS = {
    Qt.Key.Key_P: "fullscreen",
    Qt.Key.Key_Space: "play-pause",
    Qt.Key.Key_M: "plate-move",
    Qt.Key.Key_J: "reverse",
    Qt.Key.Key_K: "stop",
    Qt.Key.Key_L: "forward",
    Qt.Key.Key_Left: "step-back",
    Qt.Key.Key_Right: "step-forward",
    Qt.Key.Key_Up: "move-up",
    Qt.Key.Key_Down: "move-down",
}


def _preview_key_command(event) -> str | None:  # type: ignore[no-untyped-def]
    command = _PREVIEW_KEY_COMMANDS.get(event.key())
    if not command or not event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
        return command
    return {
        "step-back": "move-fine-left",
        "step-forward": "move-fine-right",
        "move-up": "move-fine-up",
        "move-down": "move-fine-down",
    }.get(command, command)


class PlaybackVideoWidget(QVideoWidget):
    commandRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if event.key() == Qt.Key.Key_Escape and self.isFullScreen():
            self.setFullScreen(False)
            return
        command = _preview_key_command(event)
        if command:
            self.commandRequested.emit(command)
            return
        super().keyPressEvent(event)


class FullscreenPreviewLabel(QLabel):
    """Scale a preview from its original pixmap whenever the screen size changes."""

    def __init__(self, source: QPixmap) -> None:
        super().__init__()
        self._source = QPixmap(source)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setSizePolicy(
            QSizePolicy.Policy.Ignored,
            QSizePolicy.Policy.Ignored,
        )

    def set_source(self, source: QPixmap) -> None:
        self._source = QPixmap(source)
        if not self._source.isNull() and not self.size().isEmpty():
            self.setPixmap(
                self._source.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        if not self._source.isNull() and not self.size().isEmpty():
            self.setPixmap(
                self._source.scaled(
                    self.size(),
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            )


class ScrubbableDoubleSpinBox(QDoubleSpinBox):
    """Numeric field with vertical drag scrubbing and Shift precision."""

    def __init__(self) -> None:
        super().__init__()
        self._drag_last_y: float | None = None
        self._dragging = False
        self.setAccelerated(False)

    def _drag_increment(
        self,
        pixels: float,
        modifiers: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier,
    ) -> float:
        precision = 0.1 if modifiers & Qt.KeyboardModifier.ShiftModifier else 1.0
        return pixels * self.singleStep() * precision / 4.0

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() == Qt.MouseButton.LeftButton:
            self._drag_last_y = event.globalPosition().y()
            self._dragging = False
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if (
            self._drag_last_y is not None
            and event.buttons() & Qt.MouseButton.LeftButton
        ):
            current_y = event.globalPosition().y()
            delta = self._drag_last_y - current_y
            if self._dragging or abs(delta) >= 2.0:
                self._dragging = True
                self._drag_last_y = current_y
                self.setCursor(Qt.CursorShape.SizeVerCursor)
                self.setValue(
                    self.value() + self._drag_increment(delta, event.modifiers())
                )
                event.accept()
                return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        dragged = self._dragging
        self._drag_last_y = None
        self._dragging = False
        self.unsetCursor()
        if dragged and event.button() == Qt.MouseButton.LeftButton:
            event.accept()
            return
        super().mouseReleaseEvent(event)


class PreviewView(QGraphicsView):
    commandRequested = Signal(str)

    def __init__(self) -> None:
        super().__init__()
        self.setScene(QGraphicsScene(self))
        self._item: QGraphicsPixmapItem | None = None
        self._move_overlay_active = False
        self._move_overlay_label = ""
        self.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        self.setDragMode(QGraphicsView.DragMode.ScrollHandDrag)
        self.setBackgroundBrush(QColor("#08090a"))
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setFocusPolicy(Qt.FocusPolicy.StrongFocus)
        self._empty = QLabel("P06–P08 또는 P01–P05를 넣고 QUICK PREVIEW를 누르세요")
        self._empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._empty.setStyleSheet("color:#8a8f98; font-size:13px; letter-spacing:.5px;")
        self._empty.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._empty.setParent(self.viewport())

    def mousePressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        self.setFocus()
        super().mousePressEvent(event)

    def keyPressEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        command = _preview_key_command(event)
        if command:
            self.commandRequested.emit(command)
            return
        super().keyPressEvent(event)

    def resizeEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().resizeEvent(event)
        self._empty.setGeometry(self.viewport().rect())
        if self._item is not None:
            self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def set_array(self, array: np.ndarray) -> None:
        self.set_image(_display_image(array))

    def set_image(self, image: QImage) -> None:
        pixmap = QPixmap.fromImage(image)
        self.scene().clear()
        self._item = self.scene().addPixmap(pixmap)
        self.scene().setSceneRect(self._item.boundingRect())
        self._empty.hide()
        self.fitInView(self._item, Qt.AspectRatioMode.KeepAspectRatio)

    def set_move_overlay(self, active: bool, label: str = "") -> None:
        self._move_overlay_active = bool(active)
        self._move_overlay_label = label
        self.viewport().update()

    def drawForeground(self, painter: QPainter, rect) -> None:  # type: ignore[no-untyped-def]
        super().drawForeground(painter, rect)
        if not self._move_overlay_active or self._item is None:
            return
        center = self.mapFromScene(self._item.sceneBoundingRect().center())
        x = center.x()
        y = center.y()
        painter.save()
        painter.resetTransform()
        shadow = QPen(QColor(5, 7, 10, 210), 5.0)
        shadow.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(shadow)
        painter.drawLine(x - 22, y, x + 22, y)
        painter.drawLine(x, y - 22, x, y + 22)
        painter.drawEllipse(QPointF(x, y), 10.0, 10.0)
        accent = QPen(QColor("#b7adff"), 1.6)
        accent.setCapStyle(Qt.PenCapStyle.RoundCap)
        painter.setPen(accent)
        painter.drawLine(x - 22, y, x - 7, y)
        painter.drawLine(x + 7, y, x + 22, y)
        painter.drawLine(x, y - 22, x, y - 7)
        painter.drawLine(x, y + 7, x, y + 22)
        painter.drawEllipse(QPointF(x, y), 10.0, 10.0)
        painter.setPen(QColor("#d9d5ff"))
        label_font = QFont(self.font())
        label_font.setPointSizeF(9.0)
        label_font.setWeight(QFont.Weight.DemiBold)
        painter.setFont(label_font)
        painter.drawText(x + 16, y - 14, self._move_overlay_label)
        painter.restore()

    def current_pixmap(self) -> QPixmap | None:
        if self._item is None:
            return None
        return QPixmap(self._item.pixmap())

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
        painter.setBrush(QColor("#34343a") if self.isEnabled() else QColor("#23252a"))
        painter.drawRoundedRect(left, center - 5.0, right - left, 10.0, 5.0, 5.0)
        lower_x = self._position(self._lower)
        upper_x = self._position(self._upper)
        painter.setBrush(QColor("#5e6ad2") if self.isEnabled() else QColor("#34343a"))
        painter.drawRoundedRect(lower_x, center - 6.0, upper_x - lower_x, 12.0, 5.0, 5.0)
        for position in (lower_x, upper_x):
            painter.setBrush(QColor("#7170ff") if self.isEnabled() else QColor("#62666d"))
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
        self.color_space = ChevronComboBox()
        for label, value in INPUT_COLOR_SPACES:
            self.color_space.addItem(label, value)
        self.video_range = ChevronComboBox()
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


class NewTimelineDialog(QDialog):
    def __init__(
        self,
        parent: QWidget,
        *,
        default_name: str,
        suggested_count: int,
        selected_plate_count: int | None,
        selected_media_names: list[str] | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("New Plate Set Timeline")
        self.setMinimumWidth(540)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(12)

        heading = QLabel("NEW PLATE SET TIMELINE")
        heading.setProperty("sectionTitle", True)
        layout.addWidget(heading)
        intro = QLabel(
            "Choose the physical camera layout. Numbered files map automatically; other names open a camera-slot assignment step."
        )
        intro.setWordWrap(True)
        intro.setProperty("muted", True)
        layout.addWidget(intro)

        form = QFormLayout()
        self.name = QLineEdit(default_name)
        self.name.selectAll()
        form.addRow("Timeline name", self.name)
        layout.addLayout(form)

        layout_label = QLabel("CAMERA LAYOUT")
        layout_label.setProperty("sectionTitle", True)
        layout.addWidget(layout_label)
        choices = QHBoxLayout()
        choices.setSpacing(8)
        self.layout_group = QButtonGroup(self)
        self.layout_group.setExclusive(True)
        self.layout_buttons: dict[int, QPushButton] = {}
        for count, label in (
            (3, "3 CAM · FRONT\nP06–P08"),
            (5, "5 CAM · REAR\nP01–P05"),
        ):
            button = QPushButton(label)
            button.setObjectName("layoutChoice")
            button.setCheckable(True)
            button.setMinimumHeight(62)
            button.setAccessibleName(
                f"{count} camera layout, plates "
                + ("P06 through P08" if count == 3 else "P01 through P05")
            )
            self.layout_group.addButton(button, count)
            self.layout_buttons[count] = button
            choices.addWidget(button, 1)
        self.layout_buttons[suggested_count].setChecked(True)
        layout.addLayout(choices)

        selection_card = QFrame()
        selection_card.setObjectName("selectedMediaCard")
        selection_layout = QVBoxLayout(selection_card)
        selection_layout.setContentsMargins(12, 10, 12, 10)
        selection_layout.setSpacing(7)

        selection_header = QHBoxLayout()
        selection_title = QLabel("MEDIA POOL SELECTION")
        selection_title.setProperty("sectionTitle", True)
        self.selected_media_state = QLabel()
        self.selected_media_state.setObjectName("selectedMediaState")
        self.selected_media_state.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        selection_header.addWidget(selection_title)
        selection_header.addStretch(1)
        selection_header.addWidget(self.selected_media_state)
        selection_layout.addLayout(selection_header)

        self.selected_media_files = QLabel()
        self.selected_media_files.setObjectName("selectedMediaFiles")
        self.selected_media_files.setWordWrap(True)
        self.selected_media_files.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse
        )
        selection_layout.addWidget(self.selected_media_files)

        self.add_selected = QCheckBox()
        self._selected_plate_count = max(0, int(selected_plate_count or 0))
        self._selected_media_names = list(selected_media_names or [])
        self.layout_group.idClicked.connect(self._update_selected_media_option)
        selection_layout.addWidget(self.add_selected)

        self.selection_note = QLabel()
        self.selection_note.setWordWrap(True)
        self.selection_note.setProperty("muted", True)
        selection_layout.addWidget(self.selection_note)
        layout.addWidget(selection_card)
        self.selected_media_card = selection_card

        note = QLabel(
            "You can replace the set later. Manual assignments stay in the saved timeline order."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        create = buttons.button(QDialogButtonBox.StandardButton.Save)
        create.setObjectName("primaryButton")
        self.create_button = create
        self.add_selected.toggled.connect(self._update_create_button)
        self._update_selected_media_option(suggested_count)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _update_selected_media_option(self, count: int) -> None:
        selected = self._selected_plate_count
        matches = selected == count and selected in SUPPORTED_CAMERA_COUNTS
        if matches:
            plate_range = "P06–P08" if selected == 3 else "P01–P05"
            state = "ready"
            self.selected_media_state.setText(f"READY · {selected} PLATES")
            self.add_selected.setText(
                f"Add these {selected} plates ({plate_range}) to the new timeline"
            )
            self.selection_note.setText(
                "The selected clips will be assigned during creation. "
                "Numbered files map automatically."
            )
        elif selected == 0:
            state = "empty"
            self.selected_media_state.setText("NO PLATES SELECTED")
            self.add_selected.setText("Create without Media Pool plates")
            self.selection_note.setText(
                "This will create an empty timeline. Select 3 or 5 clips first "
                "to add a complete camera set during creation."
            )
        elif selected in SUPPORTED_CAMERA_COUNTS:
            state = "warning"
            self.selected_media_state.setText(
                f"MISMATCH · {selected} SELECTED / {count} REQUIRED"
            )
            self.add_selected.setText(
                f"The {selected} selected clips do not match this {count}-camera layout"
            )
            self.selection_note.setText(
                "Choose the matching camera layout or return to the Media Pool "
                "and select the intended complete set."
            )
        else:
            state = "warning"
            self.selected_media_state.setText(
                f"INCOMPLETE · {selected} SELECTED"
            )
            self.add_selected.setText(
                "A timeline needs exactly 3 or 5 selected Media Pool clips"
            )
            self.selection_note.setText(
                "Return to the Media Pool and select the complete 3-camera or "
                "5-camera plate set, or continue with an empty timeline."
            )

        if self._selected_media_names:
            self.selected_media_files.setText("\n".join(self._selected_media_names))
        elif selected:
            self.selected_media_files.setText(f"{selected} Media Pool clips selected")
        else:
            self.selected_media_files.setText("No Media Pool clips selected")

        for widget in (self.selected_media_card, self.selected_media_state):
            widget.setProperty("state", state)
            widget.style().unpolish(widget)
            widget.style().polish(widget)
        self.add_selected.setEnabled(matches)
        self.add_selected.setChecked(matches)
        self._update_create_button()

    def _update_create_button(self) -> None:
        if not hasattr(self, "create_button"):
            return
        if self.add_selected.isEnabled() and self.add_selected.isChecked():
            self.create_button.setText(
                f"CREATE WITH {self._selected_plate_count} PLATES"
            )
        else:
            self.create_button.setText("CREATE EMPTY TIMELINE")

    def values(self) -> tuple[str, int, bool]:
        return (
            self.name.text().strip(),
            int(self.layout_group.checkedId()),
            self.add_selected.isChecked(),
        )


class PlateAssignmentDialog(QDialog):
    """Map arbitrary clip names to the physical camera slots of one timeline."""

    def __init__(
        self,
        parent: QWidget,
        *,
        paths: list[str],
        camera_count: int,
    ) -> None:
        super().__init__(parent)
        suggested, _manual = suggest_camera_assignment(paths, camera_count)
        self.setWindowTitle("Assign Camera Slots")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 18)
        layout.setSpacing(10)

        heading = QLabel("ASSIGN CAMERA SLOTS")
        heading.setProperty("sectionTitle", True)
        layout.addWidget(heading)
        note = QLabel(
            "Camera numbers were not complete in the filenames. Choose which clip belongs to each physical plate; this order is saved with the timeline."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        form = QFormLayout()
        form.setHorizontalSpacing(10)
        form.setVerticalSpacing(7)
        self.slot_combos: list[QComboBox] = []
        expected = PLATE_NUMBERS_BY_COUNT[camera_count]
        for index, plate in enumerate(expected):
            combo = ChevronComboBox()
            combo.setObjectName("cameraAssignmentCombo")
            for path in paths:
                combo.addItem(Path(path).name, path)
                combo.setItemData(combo.count() - 1, path, Qt.ItemDataRole.ToolTipRole)
            selected = combo.findData(suggested[index])
            combo.setCurrentIndex(max(0, selected))
            combo.currentIndexChanged.connect(self._update_validation)
            self.slot_combos.append(combo)
            form.addRow(f"SLOT {index + 1} · P{plate:02d}", combo)
        layout.addLayout(form)

        self.validation = QLabel()
        self.validation.setProperty("muted", True)
        layout.addWidget(self.validation)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        self.assign_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if self.assign_button is not None:
            self.assign_button.setText("ASSIGN TO TIMELINE")
            self.assign_button.setObjectName("primaryButton")
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._update_validation()

    def _update_validation(self, *_args) -> None:
        selected = self.values()
        valid = len(selected) == len(set(selected))
        self.validation.setText(
            "Each clip is assigned once."
            if valid
            else "Choose a different clip for every camera slot."
        )
        if self.assign_button is not None:
            self.assign_button.setEnabled(valid)

    def values(self) -> list[str]:
        return [str(combo.currentData()) for combo in self.slot_combos]


class ChevronComboBox(QComboBox):
    """Draw a consistent dropdown chevron instead of relying on native style."""

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        center = QPointF(self.width() - 11.0, self.height() / 2.0)
        color = QColor("#d0d6e0" if self.isEnabled() else "#62666d")
        pen = QPen(color, 1.6)
        pen.setCosmetic(True)
        pen.setCapStyle(Qt.PenCapStyle.RoundCap)
        pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setPen(pen)
        painter.drawLine(
            QPointF(center.x() - 3.5, center.y() - 1.5),
            QPointF(center.x(), center.y() + 2.0),
        )
        painter.drawLine(
            QPointF(center.x(), center.y() + 2.0),
            QPointF(center.x() + 3.5, center.y() - 1.5),
        )


class ScrollableLibraryTree(QTreeWidget):
    """Use native scrolling first, with a deterministic trackpad/wheel fallback."""

    moveRequested = Signal(object, object)
    dragStatusChanged = Signal(str)
    MIME_TYPE = "application/x-vpstitch-media-tree-items"

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self._drop_target_item: QTreeWidgetItem | None = None
        self._drop_indicator = QAbstractItemView.DropIndicatorPosition.OnViewport
        self._last_drag_status = ""
        self.setDragEnabled(True)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setDropIndicatorShown(True)
        self.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.setDefaultDropAction(Qt.DropAction.MoveAction)

    @staticmethod
    def _item_key(item: QTreeWidgetItem) -> tuple[str, str] | None:
        kind = item.data(0, Qt.ItemDataRole.UserRole)
        item_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind not in {"bin", "media"} or not item_id:
            return None
        return str(kind), str(item_id)

    def selected_payload(self) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        seen: set[tuple[str, str]] = set()
        for item in self.selectedItems():
            key = self._item_key(item)
            if key is None or key in seen:
                continue
            seen.add(key)
            payload.append({"kind": key[0], "id": key[1]})
        return payload

    def startDrag(self, _supported_actions) -> None:  # type: ignore[no-untyped-def]
        payload = self.selected_payload()
        if not payload:
            return
        indexes = self.selectedIndexes()
        mime_data = self.model().mimeData(indexes) if indexes else QMimeData()
        mime_data.setData(
            self.MIME_TYPE,
            json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )
        drag = QDrag(self)
        drag.setMimeData(mime_data)
        drag.setPixmap(self._drag_pixmap(payload))
        drag.setHotSpot(QPointF(18.0, 18.0).toPoint())
        self.dragStatusChanged.emit(
            f"Moving {len(payload)} Media Pool item{'s' if len(payload) != 1 else ''}"
        )
        drag.exec(Qt.DropAction.MoveAction)

    def _drag_pixmap(self, payload: list[dict[str, str]]) -> QPixmap:
        width, height = 286, 42
        pixmap = QPixmap(width, height)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#7170ff"), 1.5))
        painter.setBrush(QColor(30, 31, 35, 244))
        painter.drawRoundedRect(1, 1, width - 3, height - 3, 7, 7)
        current = self.currentItem()
        label = current.text(0).splitlines()[0] if current is not None else "Media Pool item"
        if len(label) > 28:
            label = f"{label[:27]}…"
        if len(payload) > 1:
            label = f"{len(payload)} items  ·  {label}"
        painter.setPen(QColor("#f7f8f8"))
        painter.drawText(16, 0, width - 30, height, Qt.AlignmentFlag.AlignVCenter, label)
        painter.end()
        return pixmap

    @staticmethod
    def _parent_bin_id(item: QTreeWidgetItem | None) -> str | None:
        parent = item.parent() if item is not None else None
        if parent is None:
            return None
        if parent.data(0, Qt.ItemDataRole.UserRole) != "bin":
            return None
        parent_id = parent.data(0, Qt.ItemDataRole.UserRole + 1)
        return str(parent_id) if parent_id else None

    @staticmethod
    def _same_kind_index(item: QTreeWidgetItem, kind: str) -> int:
        parent = item.parent()
        if parent is None:
            return 0
        index = 0
        for child_index in range(parent.childCount()):
            child = parent.child(child_index)
            if child is item:
                return index
            if child.data(0, Qt.ItemDataRole.UserRole) == kind:
                index += 1
        return index

    def _drop_destination(
        self,
        item: QTreeWidgetItem | None,
        indicator: QAbstractItemView.DropIndicatorPosition,
    ) -> dict[str, object]:
        if item is None:
            return {"bin_id": None, "kind": None, "index": None, "label": "Project root"}
        kind = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
        item_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "project":
            return {"bin_id": None, "kind": None, "index": None, "label": item.text(0)}
        if indicator == QAbstractItemView.DropIndicatorPosition.OnItem and kind == "bin":
            return {"bin_id": str(item_id), "kind": None, "index": None, "label": item.text(0)}
        parent_id = self._parent_bin_id(item)
        parent = item.parent()
        parent_label = parent.text(0) if parent is not None else "Project root"
        if kind in {"bin", "media"}:
            index = self._same_kind_index(item, kind)
            if indicator == QAbstractItemView.DropIndicatorPosition.BelowItem:
                index += 1
            return {"bin_id": parent_id, "kind": kind, "index": index, "label": parent_label}
        return {"bin_id": parent_id, "kind": None, "index": None, "label": parent_label}

    def _has_supported_mime(self, event) -> bool:  # type: ignore[no-untyped-def]
        return event.mimeData().hasFormat(self.MIME_TYPE)

    def dragEnterEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._has_supported_mime(event):
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragMoveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._has_supported_mime(event):
            event.ignore()
            return
        super().dragMoveEvent(event)
        point = event.position().toPoint()
        self._drop_target_item = self.itemAt(point)
        self._drop_indicator = self.dropIndicatorPosition()
        destination = self._drop_destination(self._drop_target_item, self._drop_indicator)
        status = f"Drop to move into {destination['label']}"
        if status != self._last_drag_status:
            self._last_drag_status = status
            self.dragStatusChanged.emit(status)
        self.viewport().update()
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()

    def dragLeaveEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().dragLeaveEvent(event)
        self._clear_drop_feedback()

    def dropEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        if not self._has_supported_mime(event):
            event.ignore()
            return
        try:
            payload = json.loads(bytes(event.mimeData().data(self.MIME_TYPE)).decode("utf-8"))
        except (TypeError, ValueError, json.JSONDecodeError):
            event.ignore()
            self._clear_drop_feedback()
            return
        point = event.position().toPoint()
        item = self.itemAt(point)
        destination = self._drop_destination(item, self.dropIndicatorPosition())
        event.setDropAction(Qt.DropAction.MoveAction)
        event.accept()
        self._clear_drop_feedback()
        self.moveRequested.emit(payload, destination)

    def _clear_drop_feedback(self) -> None:
        self._drop_target_item = None
        self._drop_indicator = QAbstractItemView.DropIndicatorPosition.OnViewport
        self._last_drag_status = ""
        self.dragStatusChanged.emit("")
        self.viewport().update()

    def paintEvent(self, event) -> None:  # type: ignore[no-untyped-def]
        super().paintEvent(event)
        item = self._drop_target_item
        if item is None:
            return
        rect = self.visualItemRect(item)
        if not rect.isValid():
            return
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(QColor("#8583ff"), 2))
        if self._drop_indicator == QAbstractItemView.DropIndicatorPosition.OnItem:
            painter.setBrush(QColor(113, 112, 255, 28))
            painter.drawRoundedRect(rect.adjusted(2, 1, -3, -2), 4, 4)
        else:
            y = rect.bottom() if self._drop_indicator == QAbstractItemView.DropIndicatorPosition.BelowItem else rect.top()
            painter.drawLine(rect.left() + 2, y, rect.right() - 3, y)
        painter.end()

    def wheelEvent(self, event: QWheelEvent) -> None:
        scrollbar = self.verticalScrollBar()
        before = scrollbar.value()
        super().wheelEvent(event)
        if scrollbar.maximum() <= scrollbar.minimum() or scrollbar.value() != before:
            return
        pixel_delta = event.pixelDelta().y()
        angle_delta = event.angleDelta().y()
        if pixel_delta:
            distance = -pixel_delta
        elif angle_delta:
            distance = int(
                max(1.0, abs(angle_delta) / 120.0)
                * max(24, scrollbar.singleStep())
            )
            distance = -distance if angle_delta > 0 else distance
        else:
            return
        scrollbar.setValue(scrollbar.value() + distance)
        event.accept()


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
        # The surrounding Active Timeline pane is resizable. Let this table
        # scroll when the pane is compact and reveal every row as it grows.
        self.setMinimumHeight(118)
        self.setMaximumHeight(16_777_215)
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

    def set_camera_numbers(self, numbers: list[int] | None) -> None:
        """Show physical plate numbers while preserving sequential rig slots."""
        if numbers is not None and len(numbers) != self.camera_count():
            raise ValueError("camera number count does not match the active rig")
        for row in range(self.camera_count()):
            number = numbers[row] if numbers is not None else row + 1
            item = self.item(row, 0)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, 0, item)
            item.setText(f"CAM {number}")
            item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEditable)

    def set_orientation(
        self,
        row: int,
        yaw: float,
        pitch: float,
        roll: float,
    ) -> None:
        for column, value in ((5, yaw), (6, pitch), (7, roll)):
            item = self.item(row, column)
            if item is None:
                item = QTableWidgetItem()
                self.setItem(row, column, item)
            item.setText(f"{float(value):.9g}")

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


class StorageAccessDialog(QDialog):
    """One place to grant and remember production storage roots on macOS."""

    def __init__(self, parent: QWidget | None = None, *, first_run: bool = False) -> None:
        super().__init__(parent)
        self.settings = _application_settings()
        self.setWindowTitle("VP Stitch · Storage Access")
        self.setMinimumWidth(620)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(22, 20, 22, 18)
        layout.setSpacing(12)

        heading = QLabel("ONE-TIME STORAGE ACCESS" if first_run else "STORAGE ACCESS")
        heading.setProperty("sectionTitle", True)
        layout.addWidget(heading)
        note = QLabel(
            "Choose the highest production folder that contains your plates, projects, "
            "and renders. VP Stitch remembers it and native macOS file panels reuse that "
            "approval. Add another root only for a separate drive or network volume."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        self.roots = QTreeWidget()
        self.roots.setHeaderLabels(["AUTHORIZED PRODUCTION ROOT"])
        self.roots.setRootIsDecorated(False)
        self.roots.header().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.roots.setMinimumHeight(150)
        layout.addWidget(self.roots)

        actions = QHBoxLayout()
        add_button = QPushButton("ADD ROOT…")
        add_button.setObjectName("primaryButton")
        remove_button = QPushButton("FORGET SELECTED")
        close_button = QPushButton(
            "CONTINUE" if first_run else "DONE"
        )
        add_button.clicked.connect(self._add_root)
        remove_button.clicked.connect(self._remove_selected)
        close_button.clicked.connect(self.accept)
        actions.addWidget(add_button)
        actions.addWidget(remove_button)
        actions.addStretch(1)
        actions.addWidget(close_button)
        layout.addLayout(actions)

        detail = QLabel(
            "A rebuilt ad-hoc development app may be asked again by macOS. A stable "
            "Developer ID signature preserves the app identity across updates."
        )
        detail.setWordWrap(True)
        detail.setProperty("muted", True)
        layout.addWidget(detail)
        self._refresh()

    def _refresh(self) -> None:
        self.roots.clear()
        for path in _setting_paths(self.settings, _STORAGE_ROOTS_KEY):
            item = QTreeWidgetItem([path])
            item.setToolTip(0, path)
            self.roots.addTopLevelItem(item)

    def _add_root(self) -> None:
        initial = _preferred_storage_directory(
            self.settings,
            "storage/lastRoot",
            Path.home(),
        )
        selected = QFileDialog.getExistingDirectory(
            self,
            "Choose production root",
            initial,
        )
        if not selected:
            return
        root = _remember_storage_root(self.settings, selected)
        self.settings.setValue("storage/lastRoot", str(root))
        self.settings.sync()
        self._refresh()

    def _remove_selected(self) -> None:
        item = self.roots.currentItem()
        if item is None:
            return
        selected = item.text(0)
        roots = [
            path
            for path in _setting_paths(self.settings, _STORAGE_ROOTS_KEY)
            if path != selected
        ]
        self.settings.setValue(_STORAGE_ROOTS_KEY, roots)
        self.settings.sync()
        self._refresh()


class ProjectManagerDialog(QDialog):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("VP Stitch · Project Manager")
        self.setMinimumSize(760, 500)
        self.setStyleSheet(
            """
            QDialog, QWidget { background:#08090a; color:#f7f8f8; font-family:'Inter Variable','Inter','SF Pro Text','-apple-system','Segoe UI'; font-size:11px; }
            QLabel { background:transparent; }
            QTreeWidget { background:#0f1011; border:1px solid #23252a; border-radius:8px; }
            QTreeWidget::item { min-height:28px; padding:3px 6px; }
            QTreeWidget::item:selected { background:#28282c; color:#f7f8f8; }
            QHeaderView::section { background:#0f1011; color:#8a8f98; border:0; border-bottom:1px solid #23252a; padding:6px; font-size:9px; font-weight:590; }
            QLineEdit, QSpinBox, QComboBox { background:#191a1b; border:1px solid #34343a; border-radius:6px; padding:5px 7px; }
            QLineEdit:focus, QSpinBox:focus, QComboBox:focus { border-color:#7170ff; }
            QPushButton { background:#191a1b; color:#d0d6e0; border:1px solid #23252a; border-radius:6px; padding:6px 10px; font-weight:510; }
            QPushButton:hover { background:#28282c; border-color:#3e3e44; color:#f7f8f8; }
            QPushButton#primaryButton { background:#5e6ad2; color:#f7f8f8; border-color:#7170ff; }
            QPushButton#primaryButton:hover { background:#828fff; }
            QPushButton#primaryButton:disabled {
                background:#151617;
                color:#62666d;
                border-color:#23252a;
            }
            """
        )
        self.project_path: Path | None = None
        self.settings = _application_settings()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(24, 22, 24, 20)
        layout.setSpacing(12)
        title = QLabel("VP STITCH PROJECTS")
        title.setProperty("sectionTitle", True)
        layout.addWidget(title)
        note = QLabel(
            "Choose a project first. Media bins, Plate Set timelines, proxies and render queue stay inside it."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        self.projects = QTreeWidget()
        self.projects.setHeaderLabels(["PROJECT", "LOCATION"])
        self.projects.setRootIsDecorated(False)
        self.projects.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.projects.itemActivated.connect(lambda _item, _column: self.open_selected())
        self.projects.header().setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        self.projects.header().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        layout.addWidget(self.projects, 1)
        actions = QHBoxLayout()
        self.new_project_button = QPushButton("NEW PROJECT")
        self.new_project_button.setObjectName("primaryButton")
        self.new_project_button.setAutoDefault(False)
        self.new_project_button.clicked.connect(self.create_project)
        open_button = QPushButton("OPEN…")
        open_button.setObjectName("secondaryButton")
        open_button.setAutoDefault(False)
        open_button.clicked.connect(self.open_existing)
        self.open_selected_button = QPushButton("OPEN SELECTED")
        self.open_selected_button.setObjectName("primaryButton")
        self.open_selected_button.setAutoDefault(False)
        self.open_selected_button.clicked.connect(self.open_selected)
        actions.addWidget(self.new_project_button)
        actions.addWidget(open_button)
        actions.addStretch()
        actions.addWidget(self.open_selected_button)
        layout.addLayout(actions)
        self.projects.itemSelectionChanged.connect(self._update_open_selected_state)
        self._load_recents()
        self._update_open_selected_state()

    def _update_open_selected_state(self) -> None:
        has_selection = self.projects.currentItem() is not None
        self.open_selected_button.setEnabled(has_selection)
        self.open_selected_button.setDefault(has_selection)
        if has_selection:
            self.projects.setFocus()

    def _recent_paths(self) -> list[str]:
        value = self.settings.value("recentProjects", [])
        if isinstance(value, str):
            return [value] if value else []
        return [str(path) for path in value]

    def _load_recents(self) -> None:
        paths = self._recent_paths()
        last = str(self.settings.value("lastProject", ""))
        if last and last not in paths:
            paths.insert(0, last)
        for value in paths:
            path = Path(value)
            # Do not probe protected folders while the launcher is merely
            # listing recent projects. Access happens only after the operator
            # explicitly opens one.
            name = path.parent.name or path.stem
            item = QTreeWidgetItem([name, str(path.parent)])
            item.setData(0, Qt.ItemDataRole.UserRole, str(path))
            self.projects.addTopLevelItem(item)
        if self.projects.topLevelItemCount():
            self.projects.setCurrentItem(self.projects.topLevelItem(0))

    def _remember(self, path: Path) -> None:
        values = [str(path), *[item for item in self._recent_paths() if item != str(path)]]
        self.settings.setValue("lastProject", str(path))
        self.settings.setValue("recentProjects", values[:12])
        self.settings.setValue("storage/lastProjectDir", str(path.parent))
        _remember_storage_root(self.settings, path.parent)
        self.settings.sync()

    def create_project(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("New VP Stitch Project")
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        name = QLineEdit("Untitled Project")
        default_projects = _user_data_root() / "projects"
        location = QLineEdit(
            _preferred_storage_directory(
                self.settings,
                "storage/lastProjectDir",
                default_projects,
            )
        )
        browse = QPushButton("…")
        browse.setObjectName("iconButton")
        location_row = QWidget()
        location_layout = QHBoxLayout(location_row)
        location_layout.setContentsMargins(0, 0, 0, 0)
        location_layout.addWidget(location)
        location_layout.addWidget(browse)
        def browse_project_location() -> None:
            selected = QFileDialog.getExistingDirectory(
                dialog,
                "Project location",
                location.text(),
            )
            if selected:
                location.setText(selected)
                self.settings.setValue("storage/lastProjectDir", selected)
                _remember_storage_root(self.settings, selected)
                self.settings.sync()

        browse.clicked.connect(browse_project_location)
        width = QSpinBox()
        width.setRange(640, MAX_CANVAS_WIDTH)
        width.setValue(20_000)
        height = QSpinBox()
        height.setRange(320, MAX_CANVAS_HEIGHT)
        height.setValue(5_504)
        ocio = QLineEdit(BUNDLED_ACES_STUDIO_ID)
        ocio.setPlaceholderText("Bundled ACES Studio config · can be changed later")
        input_space = _new_ocio_space_combo("Camera Rec.709")
        working_space = _new_ocio_space_combo("ACEScg")
        output_space = _new_ocio_space_combo("Gamma 2.4 Encoded Rec.709", output=True)
        output_mode = ChevronComboBox()
        output_mode.addItem("Color space / Log", "colorspace")
        output_mode.addItem("Display transform", "display_view")
        output_display = ChevronComboBox()
        output_view = ChevronComboBox()
        output_space_label = QLabel("Output color space")
        output_display_label = QLabel("Display")
        output_view_label = QLabel("View")
        ocio_spaces_status = QLabel()
        ocio_spaces_status.setProperty("muted", True)
        load_spaces = QPushButton("LOAD SPACES")
        load_spaces.setObjectName("secondaryButton")
        ocio_row = QWidget()
        ocio_layout = QHBoxLayout(ocio_row)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.addWidget(ocio)
        ocio_layout.addWidget(load_spaces)
        for field in (ocio, input_space, working_space, output_space):
            field.setMinimumWidth(320)

        def reload_spaces() -> None:
            try:
                count = _load_ocio_combo_group(
                    ocio.text().strip(),
                    (input_space, working_space, output_space),
                )
                displays = ocio_display_views(ocio.text().strip())
                _populate_delivery_display_combo(
                    output_display,
                    tuple(displays),
                    _delivery_display_value(output_display),
                )
                _populate_combo(
                    output_view,
                    displays.get(_delivery_display_value(output_display), ()),
                    output_view.currentText(),
                )
                ocio_spaces_status.setText(f"{count} OCIO spaces loaded")
            except Exception as error:
                ocio_spaces_status.setText(f"Could not read OCIO config · {error}")

        load_spaces.clicked.connect(reload_spaces)
        ocio.editingFinished.connect(reload_spaces)
        output_display.currentIndexChanged.connect(
            lambda _index: _populate_combo(
                output_view,
                ocio_display_views(ocio.text().strip()).get(
                    _delivery_display_value(output_display), ()
                ),
                "",
            )
        )

        def update_delivery() -> None:
            hdr = output_mode.currentData() == "display_view"
            output_space.setVisible(not hdr)
            output_space_label.setVisible(not hdr)
            output_display.setVisible(hdr)
            output_display_label.setVisible(hdr)
            output_view.setVisible(hdr)
            output_view_label.setVisible(hdr)

        output_mode.currentIndexChanged.connect(update_delivery)
        reload_spaces()
        form.addRow("Project name", name)
        form.addRow("Location", location_row)
        form.addRow("Default canvas width", width)
        form.addRow("Default canvas height", height)
        form.addRow("OCIO config", ocio_row)
        form.addRow("Input transform", input_space)
        form.addRow("Working space", working_space)
        form.addRow("Delivery method", output_mode)
        form.addRow(output_space_label, output_space)
        form.addRow(output_display_label, output_display)
        form.addRow(output_view_label, output_view)
        form.addRow(ocio_spaces_status)
        update_delivery()
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        save_button = buttons.button(QDialogButtonBox.StandardButton.Save)
        if save_button is not None:
            save_button.setText("CREATE")
            save_button.setObjectName("primaryButton")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        project_name = name.text().strip()
        if not project_name:
            return
        path = Path(location.text()).expanduser() / project_name / "project.json"
        try:
            ocio_value = ocio.text().strip()
            delivery_mode = str(output_mode.currentData())
            delivery = (
                {
                    "output_mode": "display_view",
                    "display": _delivery_display_value(output_display),
                    "view": output_view.currentText().strip(),
                }
                if delivery_mode == "display_view"
                else {
                    "output_mode": "colorspace",
                    "output_space": output_space.currentText().strip(),
                }
            )
            store = ProjectStore.create(
                path,
                name=project_name,
                settings_snapshot={
                    "output": {"width": width.value(), "height": height.value()},
                    "color": {
                        "mode": "ocio" if ocio_value else "passthrough",
                        **(
                            {
                                "ocio_config": ocio_value,
                                "working_space": working_space.currentText().strip(),
                                **delivery,
                            }
                            if ocio_value
                            else {}
                        ),
                    },
                    "cameras": [
                        {"colorspace": input_space.currentText().strip()}
                    ] if ocio_value else [],
                },
            )
            store.add_bin(Bin.create("Master"))
        except ProjectError as error:
            QMessageBox.critical(self, "New Project", str(error))
            return
        self.project_path = path
        self._remember(path)
        self.accept()

    def open_existing(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open VP Stitch Project",
            _preferred_storage_directory(
                self.settings,
                "storage/lastProjectDir",
                _user_data_root() / "projects",
            ),
            "VP Stitch project (project.json *.vpstitch);;JSON (*.json)",
        )
        if not path:
            return
        try:
            ProjectStore.load(path, autosave=False)
        except ProjectError as error:
            QMessageBox.critical(self, "Open Project", str(error))
            return
        self.project_path = Path(path)
        self._remember(self.project_path)
        self.accept()

    def open_selected(self) -> None:
        item = self.projects.currentItem()
        if item is None:
            self.open_existing()
            return
        path = Path(str(item.data(0, Qt.ItemDataRole.UserRole)))
        if not path.is_file():
            return
        self.project_path = path
        self._remember(path)
        self.accept()


class MainWindow(QMainWindow):
    def __init__(self, project_path: Path | None = None) -> None:
        super().__init__()
        self.setWindowTitle(f"{APP_NAME}  —  5-Camera 180°")
        self.resize(1600, 960)
        self.setMinimumSize(1180, 720)
        self.setAcceptDrops(True)
        self.settings = _application_settings()
        self.runtime_root = _runtime_root()
        self.project_root = self.runtime_root if getattr(sys, "frozen", False) else Path.cwd()
        self.user_data_root = _user_data_root() if getattr(sys, "frozen", False) else self.project_root / ".vpstitch-ui"
        self.user_data_root.mkdir(parents=True, exist_ok=True)
        self.config_path: Path | None = None
        self.config_data: dict[str, object] = {}
        self._rig_profiles: dict[int, dict[str, object]] = {}
        self._plate_numbers: list[int] | None = None
        self._source_probes: list[dict[str, object]] | None = None
        self._source_fps_error: str | None = None
        self._source_overrides: dict[str, dict[str, str | None]] = {}
        self._closing = False
        self._loading_config = False
        self._loading_plate_controls = False
        self._selected_camera_row = 0
        self._plate_move_mode = False
        self._latest_playback_frame: QImage | None = None
        self._plate_reset_cameras: list[dict[str, object]] = []
        self._import_dialog: QFileDialog | None = None
        self._message_box: QMessageBox | None = None
        self._pending_log_lines: list[str] = []
        self._log_flush_timer = QTimer(self)
        self._log_flush_timer.setInterval(50)
        self._log_flush_timer.setSingleShot(True)
        self._log_flush_timer.timeout.connect(self._flush_log)
        self._live_preview_timer = QTimer(self)
        self._live_preview_timer.setInterval(180)
        self._live_preview_timer.setSingleShot(True)
        self._live_preview_timer.timeout.connect(self._run_live_preview)
        self._playback_warmup_timer = QTimer(self)
        self._playback_warmup_timer.setSingleShot(True)
        self._playback_warmup_timer.timeout.connect(self._warm_playback_cache)
        self._live_preview_pending = False
        self._live_preview_revision = 0
        self._live_preview_message = "Live preview updated"
        self._interactive_renderer = InteractivePreviewRenderer(
            max_width=2048, max_height=1152
        )
        self._interactive_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vpstitch-preview",
        )
        self._interactive_future: Future[np.ndarray] | None = None
        self._interactive_request: (
            tuple[int, object, list[str], str] | None
        ) = None
        self._interactive_signals = _InteractivePreviewSignals(self)
        self._interactive_signals.finished.connect(
            self._interactive_preview_finished
        )
        self._live_playback_executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="vpstitch-live-playback",
        )
        self._live_playback_signals = _InteractivePreviewSignals(self)
        self._live_playback_signals.finished.connect(
            self._live_proxy_frame_finished
        )
        self._live_playback_future: Future[object] | None = None
        self._live_playback_session: LivePlaybackSession | None = None
        self._live_playback_key: str | None = None
        self._live_playback_revision = 0
        self._live_playback_pending: tuple[int, str, bool, int, bool] | None = None
        self._live_playing = False
        self._live_direction = 1
        self._live_close_pending = False
        self._fullscreen_live_label: FullscreenPreviewLabel | None = None
        self.process: QProcess | None = None
        self._process_success: Callable[[], None] | None = None
        self._process_failure: Callable[[], None] | None = None
        self._process_interactive = False
        self._process_task_name = ""
        self._last_reference_dir: Path | None = None
        self._last_reference_config_path: Path | None = None
        self._reference_frame_index: int | None = None
        self._preview_ready = False
        self._preview_in_progress = False
        self._pending_scrub_frame: int | None = None
        self._playback_path: Path | None = None
        self._playback_key: str | None = None
        self._playback_autostart = False
        self._pending_playback_request = False
        self._last_auto_output: str | None = None
        self._updating_output_destination = False
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
        self._queue_running = False
        self._queue_current_id: str | None = None
        self._queue_load_error: str | None = None
        self._queue_progress: dict[str, tuple[int, int, float | None]] = {}
        self._render_progress_started_at: float | None = None
        self._render_progress_last_at: float | None = None
        self._render_progress_last_done = 0
        self._render_seconds_per_frame: float | None = None
        self._render_frame_samples: list[float] = []
        self._render_eta_warmup_remaining = 1
        self._render_progress_total = 0
        self._render_map_progress: tuple[int, int] | None = None
        self._last_render_elapsed_seconds: float | None = None
        self._render_progress_timer = QTimer(self)
        self._render_progress_timer.setInterval(1000)
        self._render_progress_timer.timeout.connect(self._refresh_render_clock)
        self._process_output_buffer = ""
        self._process_phase = ""
        self._active_timeline_id: str | None = None
        self._active_bin_id: str | None = None
        self._loading_timeline = False
        self._auto_cache_requested = False
        self._auto_cache_in_progress = False
        self._playback_cache_cancelled_for_interaction = False
        self._source_proxy_queue: list[str] = []
        self._source_proxy_current: tuple[str, SourceProxyPlan] | None = None
        self._source_proxy_attempts: list[SourceProxyCommand] = []
        self._source_proxy_backend = "unknown"
        self._source_proxy_output = bytearray()
        self._source_proxy_process = QProcess(self)
        self._source_proxy_process.setProcessChannelMode(
            QProcess.ProcessChannelMode.MergedChannels
        )
        self._source_proxy_process.readyReadStandardOutput.connect(
            self._read_source_proxy_process
        )
        self._source_proxy_process.finished.connect(
            self._source_proxy_finished
        )
        self._auto_workflows_enabled = not bool(os.environ.get("PYTEST_CURRENT_TEST"))
        self._last_autosave_digest: str | None = None
        self._last_autosave_at: float | None = None
        self._project_undo_stack: list[
            tuple[dict[str, object], str | None, str | None]
        ] = []
        self._project_redo_stack: list[
            tuple[dict[str, object], str | None, str | None]
        ] = []
        self._pending_project_undo: (
            tuple[dict[str, object], str | None, str | None] | None
        ) = None
        self._restoring_project_snapshot = False
        self._undo_capture_timer = QTimer(self)
        self._undo_capture_timer.setSingleShot(True)
        self._undo_capture_timer.setInterval(350)
        self._undo_capture_timer.timeout.connect(self._flush_pending_project_undo)
        self._fullscreen_preview: QDialog | None = None
        self._fullscreen_video: PlaybackVideoWidget | None = None
        self._reverse_timer = QTimer(self)
        self._reverse_timer.setInterval(42)
        self._reverse_timer.timeout.connect(self._reverse_tick)
        self.project_store = self._open_project_store(project_path)
        self.project_store.change_listener = self._project_store_changed
        project_directory = self.project_store.path.parent
        self._working_dir = project_directory / "work"
        self._cache_dir = project_directory / "cache"
        self._output_root = project_directory / "renders"
        for directory in (self._working_dir, self._cache_dir, self._output_root):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            self.render_queue = RenderQueueStore.load(
                project_directory / "render-queue.json"
            )
        except RenderQueueError as error:
            self._queue_load_error = str(error)
            self.render_queue = RenderQueueStore(
                project_directory / "render-queue.recovered.json"
            )
        self._build_ui()
        self._apply_style()
        self._restore_workspace_layout()
        self._autosave_timer = QTimer(self)
        self._autosave_timer.setInterval(AUTOSAVE_INTERVAL_MS)
        self._autosave_timer.timeout.connect(self._autosave_project_snapshot)
        if self._auto_workflows_enabled:
            self._autosave_timer.start()
        initial = Path(str(self.settings.value("lastConfig", "")))
        if not initial.is_file():
            initial = self.project_root / "configs" / "drive_5cam_180.prores-hq.json"
        if not initial.is_file():
            initial = self.project_root / "configs" / "five_cam_180.sample.json"
        if initial.is_file():
            self.load_config(initial)
        if not self.ocio_config.text().strip():
            self.ocio_config.setText(BUNDLED_ACES_STUDIO_ID)
        self._apply_project_defaults()
        self._refresh_media_tree()
        if self._auto_workflows_enabled:
            self._queue_source_proxies(list(self.project_store.media))
        self._update_project_header()
        if self._auto_workflows_enabled:
            self._restore_active_timeline()
        self.statusBar().showMessage(
            "Ready · Quick Preview uses one 2K frame; final render stays full resolution"
        )
        self._clear_project_history()

    def _open_project_store(self, project_path: Path | None) -> ProjectStore:
        testing = bool(os.environ.get("PYTEST_CURRENT_TEST"))
        path = project_path or self.user_data_root / "projects" / "Default Project" / "project.json"
        if path.is_file():
            try:
                return ProjectStore.load(path, autosave=not testing)
            except ProjectError as error:
                self._queue_load_error = f"Project recovery: {error}"
        name = path.parent.name if path.name == "project.json" else path.stem
        store = ProjectStore.create(
            path,
            name=name or "Untitled Project",
            settings_snapshot={},
            autosave=not testing,
        )
        if not store.bins:
            store.add_bin(Bin.create("Master"))
        return store

    def _build_ui(self) -> None:
        top_bar = QFrame()
        top_bar.setObjectName("topBar")
        top_bar.setFixedHeight(42)
        top_layout = QHBoxLayout(top_bar)
        top_layout.setContentsMargins(12, 0, 10, 0)
        top_layout.setSpacing(8)
        app_title = QLabel("VP Stitch")
        app_title.setObjectName("appTitle")
        top_layout.addWidget(app_title)
        self.app_subtitle = QLabel("5-CAMERA 180° PANORAMA")
        self.app_subtitle.setObjectName("appSubtitle")
        top_layout.addWidget(self.app_subtitle)
        top_layout.addStretch()
        self.project_title = QLabel(self.project_store.settings.name)
        self.project_title.setObjectName("projectTitle")
        self.project_title.setAlignment(Qt.AlignmentFlag.AlignCenter)
        top_layout.addWidget(self.project_title)
        top_layout.addSpacing(12)
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
        self.inspector_toggle.hide()
        self.jobs_toggle = QPushButton("JOBS")
        self.jobs_toggle.setObjectName("topButton")
        self.jobs_toggle.setCheckable(True)
        self.jobs_toggle.clicked.connect(self._toggle_log)
        top_layout.addWidget(self.jobs_toggle)
        self.jobs_toggle.hide()

        self.source_table = SourceTable()
        self.source_table.itemChanged.connect(self._source_item_changed)
        self.source_table.itemSelectionChanged.connect(self._source_selection_changed)
        self.source_table.inputSettingsRequested.connect(self._open_input_settings)
        source_group = QFrame()
        source_group.setObjectName("libraryPanel")
        self.library_panel = source_group
        source_group.setMinimumWidth(270)
        source_group.setMaximumWidth(560)
        source_group.setSizePolicy(
            QSizePolicy.Policy.Preferred,
            QSizePolicy.Policy.Expanding,
        )
        source_layout = QVBoxLayout(source_group)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_layout.setSpacing(0)

        self.library_splitter = QSplitter(Qt.Orientation.Vertical)
        self.library_splitter.setObjectName("librarySplitter")
        self.library_splitter.setAccessibleName("Resizable library sections")
        self.library_splitter.setChildrenCollapsible(False)
        self.library_splitter.setHandleWidth(7)

        self.media_pool_section = QFrame()
        self.media_pool_section.setObjectName("librarySection")
        self.media_pool_section.setAccessibleName("Media Pool section")
        self.media_pool_section.setMinimumHeight(145)
        media_layout = QVBoxLayout(self.media_pool_section)
        media_layout.setContentsMargins(11, 9, 11, 8)
        media_layout.setSpacing(6)
        media_header = QHBoxLayout()
        media_title = QLabel("MEDIA POOL")
        media_title.setProperty("sectionTitle", True)
        media_header.addWidget(media_title)
        media_header.addStretch()
        self.new_bin_button = QPushButton("+ FOLDER")
        self.new_bin_button.setObjectName("secondaryButton")
        self.new_bin_button.setToolTip("New folder")
        self.new_bin_button.clicked.connect(self.create_media_bin)
        self.import_button = QPushButton("IMPORT")
        self.import_button.setObjectName("primaryButton")
        self.import_button.setAccessibleName("Import Media")
        self.import_button.setAccessibleDescription(
            "Add individual source clips to the project Media Pool"
        )
        self.import_button.setToolTip("Import clips into the Media Pool")
        self.import_button.clicked.connect(self.choose_videos)
        media_header.addWidget(self.import_button)
        media_header.addWidget(self.new_bin_button)
        media_layout.addLayout(media_header)
        self.media_hint = QLabel(
            "Drag clips or folders to organize · drop on a folder to move inside."
        )
        self.media_hint.setWordWrap(True)
        self.media_hint.setProperty("muted", True)
        media_layout.addWidget(self.media_hint)

        self.media_tree = ScrollableLibraryTree()
        self.media_tree.setObjectName("mediaTree")
        self.media_tree.setAccessibleName("Source Media Pool")
        self.media_tree.setAccessibleDescription(
            "Hierarchical project folders contain source clips. Drag clips or folders "
            "to move them, or use the context menu."
        )
        self.media_tree.setHeaderHidden(True)
        self.media_tree.setIndentation(16)
        self.media_tree.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.media_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.media_tree.customContextMenuRequested.connect(self._media_tree_menu)
        self.media_tree.itemActivated.connect(self._media_item_activated)
        self.media_tree.itemSelectionChanged.connect(self._media_selection_changed)
        self.media_tree.moveRequested.connect(self._move_media_tree_items)
        self.media_tree.dragStatusChanged.connect(self._media_drag_status_changed)
        self.media_tree.setAnimated(True)
        self.media_tree.setMouseTracking(True)
        self.media_tree.setMinimumHeight(120)
        self.media_tree.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.media_tree.verticalScrollBar().setSingleStep(28)
        self.remove_media_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            shortcut = QShortcut(QKeySequence(key), self.media_tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.delete_selected_media_items)
            self.remove_media_shortcuts.append(shortcut)
        media_layout.addWidget(self.media_tree, 1)

        self.plate_sets_section = QFrame()
        self.plate_sets_section.setObjectName("librarySection")
        self.plate_sets_section.setAccessibleName("Plate Sets section")
        self.plate_sets_section.setMinimumHeight(115)
        timeline_layout = QVBoxLayout(self.plate_sets_section)
        timeline_layout.setContentsMargins(11, 9, 11, 8)
        timeline_layout.setSpacing(6)

        timeline_header = QHBoxLayout()
        timeline_title = QLabel("PLATE SETS")
        timeline_title.setProperty("sectionTitle", True)
        timeline_header.addWidget(timeline_title)
        timeline_header.addStretch()
        self.new_timeline_button = QPushButton("NEW TIMELINE")
        self.new_timeline_button.setObjectName("secondaryButton")
        self.new_timeline_button.setToolTip(
            "Create a named 3-camera or 5-camera Plate Set timeline"
        )
        self.new_timeline_button.clicked.connect(self.create_timeline)
        timeline_header.addWidget(self.new_timeline_button)
        timeline_layout.addLayout(timeline_header)

        self.timeline_tree = ScrollableLibraryTree()
        self.timeline_tree.setObjectName("timelineTree")
        self.timeline_tree.setAccessibleName("Plate Set timelines")
        self.timeline_tree.setAccessibleDescription(
            "Named timelines only. Source clips stay in the Media Pool above."
        )
        self.timeline_tree.setHeaderHidden(True)
        self.timeline_tree.setRootIsDecorated(False)
        self.timeline_tree.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.timeline_tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.timeline_tree.customContextMenuRequested.connect(self._timeline_tree_menu)
        self.timeline_tree.itemActivated.connect(self._timeline_item_activated)
        self.timeline_tree.setMinimumHeight(90)
        self.timeline_tree.setVerticalScrollMode(
            QAbstractItemView.ScrollMode.ScrollPerPixel
        )
        self.timeline_tree.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.timeline_tree.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.timeline_tree.verticalScrollBar().setSingleStep(28)
        self.rename_plate_set_shortcut = QShortcut(QKeySequence(Qt.Key.Key_F2), self.timeline_tree)
        self.rename_plate_set_shortcut.activated.connect(self.rename_timeline)
        self.delete_plate_set_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            shortcut = QShortcut(QKeySequence(key), self.timeline_tree)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.delete_selected_timeline)
            self.delete_plate_set_shortcuts.append(shortcut)
        timeline_layout.addWidget(self.timeline_tree, 1)

        self.active_timeline_section = QFrame()
        self.active_timeline_section.setObjectName("librarySection")
        self.active_timeline_section.setAccessibleName("Active Timeline section")
        self.active_timeline_section.setMinimumHeight(235)
        active_layout = QVBoxLayout(self.active_timeline_section)
        active_layout.setContentsMargins(11, 9, 11, 8)
        active_layout.setSpacing(6)

        self.active_plates_title = QLabel("ACTIVE TIMELINE · NONE")
        self.active_plates_title.setProperty("inspectorTitle", True)
        self.active_plates_title.setWordWrap(True)
        active_layout.addWidget(self.active_plates_title)
        active_layout.addWidget(self.source_table, 1)
        source_buttons = QHBoxLayout()
        self.assign_media_button = QPushButton("ASSIGN SELECTED")
        self.assign_media_button.setObjectName("primaryButton")
        self.assign_media_button.setAccessibleName("Assign selected media")
        self.assign_media_button.setToolTip(
            "Assign the selected 3 or 5 Media Pool clips to this timeline"
        )
        self.assign_media_button.clicked.connect(
            self.add_selected_media_to_active_timeline
        )
        self.clear_button = QPushButton("REMOVE ALL")
        self.clear_button.setObjectName("secondaryButton")
        self.clear_button.setToolTip("Remove all assigned plates from this timeline")
        self.clear_button.clicked.connect(self.clear_sources)
        source_buttons.addWidget(self.assign_media_button, 1)
        source_buttons.addWidget(self.clear_button)
        active_layout.addLayout(source_buttons)
        self.source_status = QLabel("Import clips, then add a complete camera set to a timeline")
        self.source_status.setObjectName("sourceStatus")
        self.source_status.setWordWrap(True)
        active_layout.addWidget(self.source_status)

        self.library_splitter.addWidget(self.media_pool_section)
        self.library_splitter.addWidget(self.plate_sets_section)
        self.library_splitter.addWidget(self.active_timeline_section)
        self.library_splitter.setStretchFactor(0, 3)
        self.library_splitter.setStretchFactor(1, 2)
        self.library_splitter.setStretchFactor(2, 4)
        self.library_splitter.setSizes([245, 165, 300])
        source_layout.addWidget(self.library_splitter, 1)

        self.preview = PreviewView()
        self.preview.commandRequested.connect(self._handle_preview_command)
        self.video_preview = PlaybackVideoWidget()
        self.video_preview.commandRequested.connect(self._handle_preview_command)
        self.video_preview.setStyleSheet("background:#05070a;")
        self.preview_stack = QStackedWidget()
        self.preview_stack.addWidget(self.preview)
        self.preview_stack.addWidget(self.video_preview)
        self.media_player = QMediaPlayer(self)
        self.media_player.setVideoOutput(self.video_preview)
        self.video_preview.videoSink().videoFrameChanged.connect(
            self._capture_playback_frame
        )
        self.media_player.positionChanged.connect(self._playback_position_changed)
        self.media_player.playbackStateChanged.connect(self._playback_state_changed)
        self.media_player.errorOccurred.connect(self._playback_error)
        preview_box = QFrame()
        preview_box.setObjectName("previewPanel")
        preview_layout = QVBoxLayout(preview_box)
        preview_layout.setContentsMargins(12, 10, 12, 7)
        preview_layout.setSpacing(7)
        preview_header = QHBoxLayout()
        title = QLabel("PANORAMA VIEWER")
        title.setProperty("sectionTitle", True)
        self.preview_context = QLabel("NO TIMELINE OPEN")
        self.preview_context.setProperty("muted", True)
        preview_limit = QLabel("LIVE ADAPTIVE  ·  FINAL FULL QUALITY")
        preview_limit.setObjectName("previewLimit")
        preview_header.addWidget(title)
        preview_header.addSpacing(12)
        preview_header.addWidget(self.preview_context)
        preview_header.addStretch()
        preview_header.addWidget(preview_limit)
        preview_layout.addLayout(preview_header)
        preview_layout.addWidget(self.preview_stack, 1)
        self.preview_note = QLabel(
            "Import builds lightweight source cache · TC Align enables synchronized playback"
        )
        self.preview_note.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.preview_note.setProperty("muted", True)
        preview_layout.addWidget(self.preview_note)

        settings_scroll = QScrollArea()
        settings_scroll.setObjectName("settingsPanel")
        settings_scroll.setWidgetResizable(True)
        settings_scroll.setMinimumWidth(330)
        settings_scroll.setMaximumWidth(390)
        settings_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.settings_tabs = QTabWidget()
        self.settings_tabs.setMinimumWidth(0)
        self.settings_tabs.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        self.settings_tabs.addTab(self._stitch_settings(), "RIG")
        self._plate_settings_index = self.settings_tabs.addTab(
            self._plate_settings(), "PLATE"
        )
        self.settings_tabs.addTab(self._color_settings(), "COLOR")
        self.settings_tabs.addTab(self._output_settings(), "DELIVER")
        settings_scroll.setWidget(self.settings_tabs)

        inspector_page = QWidget()
        inspector_page_layout = QVBoxLayout(inspector_page)
        inspector_page_layout.setContentsMargins(0, 0, 0, 0)
        inspector_page_layout.setSpacing(6)
        inspector_page_layout.addWidget(settings_scroll, 1)

        self.inspector_panel = QFrame()
        self.inspector_panel.setObjectName("inspectorPanel")
        self.inspector_panel.setMinimumWidth(360)
        self.inspector_panel.setMaximumWidth(430)
        inspector_layout = QVBoxLayout(self.inspector_panel)
        inspector_layout.setContentsMargins(8, 7, 8, 7)
        inspector_layout.setSpacing(0)
        self.right_tabs = QTabWidget()
        self.right_tabs.setObjectName("rightTabs")
        self.right_tabs.addTab(inspector_page, "INSPECTOR")
        inspector_layout.addWidget(self.right_tabs, 1)

        self.workspace_splitter = QSplitter(Qt.Orientation.Horizontal)
        self.workspace_splitter.setObjectName("workspaceSplitter")
        self.workspace_splitter.setAccessibleName("Resizable workspace panels")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(7)
        self.workspace_splitter.addWidget(source_group)
        self.workspace_splitter.addWidget(preview_box)
        self.workspace_splitter.addWidget(self.inspector_panel)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.workspace_splitter.setStretchFactor(2, 0)
        self.workspace_splitter.setSizes([340, 870, 390])

        timing_panel = QFrame()
        timing_panel.setObjectName("timingPanel")
        timing_layout = QVBoxLayout(timing_panel)
        timing_layout.setContentsMargins(12, 7, 12, 7)
        timing_layout.setSpacing(2)
        timing_header = QHBoxLayout()
        self.timing_title = QLabel("TIMELINE RANGE · NO TIMELINE")
        self.timing_title.setProperty("sectionTitle", True)
        self.timing_status = QLabel("TC Align finds the shortest common range across every camera")
        self.timing_status.setProperty("muted", True)
        timing_header.addWidget(self.timing_title)
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
        self.playback_button = QPushButton("▶  PLAY")
        self.playback_button.setObjectName("quietButton")
        self.playback_button.setToolTip(
            "Space starts the adaptive live draft immediately; a 960px playback cache replaces it when ready"
        )
        self.playback_button.clicked.connect(self.toggle_playback)
        timing_values.addWidget(self.playback_button)
        timing_values.addWidget(self.reset_timeline_button)
        timing_layout.addLayout(timing_values)

        self.playback_shortcut = QShortcut(QKeySequence(Qt.Key.Key_Space), self)
        self.playback_shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
        self.playback_shortcut.activated.connect(self._toggle_playback_shortcut)
        self.preview_shortcuts: list[QShortcut] = []
        for key, command in (
            ("J", "reverse"),
            ("K", "stop"),
            ("L", "forward"),
            (Qt.Key.Key_Left, "step-back"),
            (Qt.Key.Key_Right, "step-forward"),
            (Qt.Key.Key_Up, "move-up"),
            (Qt.Key.Key_Down, "move-down"),
            ("Shift+Left", "move-fine-left"),
            ("Shift+Right", "move-fine-right"),
            ("Shift+Up", "move-fine-up"),
            ("Shift+Down", "move-fine-down"),
        ):
            # Keep transport available after clicking inspectors, the task log,
            # or the timeline. The handler below still protects editable fields
            # so typing J/L or using arrow keys in a value control is safe.
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(
                Qt.ShortcutContext.ApplicationShortcut
                if command == "fullscreen"
                else Qt.ShortcutContext.WindowShortcut
            )
            shortcut.activated.connect(
                lambda selected=command: self._handle_preview_shortcut(selected)
            )
            self.preview_shortcuts.append(shortcut)
        self.shortcut_hint = QLabel(
            "Space Play/Pause  ·  J/K/L Transport  ·  M Move Plate  ·  P Full Screen"
        )
        self.shortcut_hint.setProperty("muted", True)
        self.shortcut_hint.setAlignment(Qt.AlignmentFlag.AlignCenter)
        timing_layout.addWidget(self.shortcut_hint)

        action_bar = QFrame()
        action_bar.setObjectName("actionBar")
        action_layout = QHBoxLayout(action_bar)
        action_layout.setContentsMargins(10, 6, 10, 6)
        action_layout.setSpacing(6)

        def workflow_button(text: str, callback: Callable[[], None]) -> QPushButton:
            button = QPushButton(text)
            button.setObjectName("workflowButton")
            button.setMinimumSize(106, 30)
            button.setMaximumWidth(146)
            button.clicked.connect(callback)
            action_layout.addWidget(button)
            return button

        self.tc_align_button = workflow_button("TC ALIGN", self.align_timecode)
        self.preview_button = workflow_button("PREVIEW", self.create_preview)
        self.preview_button.setToolTip(
            "Stitch only the current playhead frame at 2K using the saved camera geometry"
        )
        self.rig_align_button = workflow_button("STITCH", self.auto_align)
        self.rig_align_button.setEnabled(False)
        self.rig_align_button.setToolTip(
            "Solve yaw, pitch and roll once from the Quick Preview frame, then reuse those values for the timeline"
        )
        self.add_queue_button = workflow_button(
            "ADD TO QUEUE", self.add_current_to_queue
        )
        self.add_queue_button.setObjectName("primaryButton")
        action_layout.addStretch()
        self.render_button = workflow_button("RENDER NOW", self.render)
        self.render_button.setObjectName("secondaryButton")
        self.render_button.setMinimumWidth(126)
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
        queue_page = QWidget()
        queue_layout = QVBoxLayout(queue_page)
        queue_layout.setContentsMargins(0, 4, 0, 0)
        self.queue_status = QLabel("No timelines queued")
        self.queue_status.setProperty("muted", True)
        queue_layout.addWidget(self.queue_status)
        self.queue_table = QTableWidget(0, 4)
        self.queue_table.setHorizontalHeaderLabels(
            ["TIMELINE", "FPS", "FORMAT", "STATUS / ETA"]
        )
        self.queue_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        self.queue_table.setSelectionMode(
            QAbstractItemView.SelectionMode.SingleSelection
        )
        self.queue_table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.queue_table.verticalHeader().setVisible(False)
        self.queue_table.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeMode.Stretch
        )
        self.queue_table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.ResizeToContents
        )
        self.queue_table.horizontalHeader().setSectionResizeMode(
            2, QHeaderView.ResizeMode.ResizeToContents
        )
        self.queue_table.horizontalHeader().setSectionResizeMode(
            3, QHeaderView.ResizeMode.ResizeToContents
        )
        self.queue_table.doubleClicked.connect(self.load_selected_queue_job)
        self.queue_table.itemSelectionChanged.connect(
            self._update_queue_action_state
        )
        self.queue_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu
        )
        self.queue_table.customContextMenuRequested.connect(self._queue_table_menu)
        self.remove_queue_shortcuts: list[QShortcut] = []
        for key in (Qt.Key.Key_Backspace, Qt.Key.Key_Delete):
            shortcut = QShortcut(QKeySequence(key), self.queue_table)
            shortcut.setContext(Qt.ShortcutContext.WidgetWithChildrenShortcut)
            shortcut.activated.connect(self.remove_selected_queue_job)
            self.remove_queue_shortcuts.append(shortcut)
        queue_layout.addWidget(self.queue_table)
        queue_actions = QHBoxLayout()
        queue_actions.addStretch()
        self.render_selected_queue_button = QPushButton("RENDER SELECTED")
        self.render_selected_queue_button.setObjectName("secondaryButton")
        self.render_selected_queue_button.setToolTip("Render the selected queue item")
        self.render_selected_queue_button.clicked.connect(
            self.render_selected_queue_job
        )
        queue_actions.addWidget(self.render_selected_queue_button)
        self.render_all_queue_button = QPushButton("RENDER ALL")
        self.render_all_queue_button.setObjectName("primaryButton")
        self.render_all_queue_button.clicked.connect(self.render_all_queue_jobs)
        queue_actions.addWidget(self.render_all_queue_button)
        queue_layout.addLayout(queue_actions)
        log_page = QWidget()
        task_log_layout = QVBoxLayout(log_page)
        task_log_layout.setContentsMargins(0, 4, 0, 0)
        task_log_header = QHBoxLayout()
        self.log_status = QLabel("Task output and warnings")
        self.log_status.setProperty("muted", True)
        task_log_header.addWidget(self.log_status)
        task_log_header.addStretch()
        self.clear_log_button = QPushButton("CLEAR LOG")
        self.clear_log_button.setObjectName("secondaryButton")
        self.clear_log_button.clicked.connect(self.log.clear)
        task_log_header.addWidget(self.clear_log_button)
        task_log_layout.addLayout(task_log_header)
        task_log_layout.addWidget(self.log)
        self.right_tabs.addTab(queue_page, "RENDER QUEUE")
        self.right_tabs.addTab(log_page, "TASK LOG")
        self.jobs_tabs = self.right_tabs
        self.log_box = self.inspector_panel

        workspace = QWidget()
        workspace_layout = QVBoxLayout(workspace)
        workspace_layout.setContentsMargins(6, 6, 6, 5)
        workspace_layout.setSpacing(5)
        workspace_layout.addWidget(self.workspace_splitter, 1)
        workspace_layout.addWidget(timing_panel)
        workspace_layout.addWidget(action_bar)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        central_layout.addWidget(top_bar)
        central_layout.addWidget(workspace, 1)
        self.setCentralWidget(central)
        self._build_menus()

        status = QStatusBar()
        self.autosave_status = QLabel("AUTOSAVE ON")
        self.autosave_status.setObjectName("autosaveStatus")
        self.autosave_status.setToolTip(
            "Project edits save atomically as they happen. The time shown is the last verified disk save; a recovery snapshot refreshes every 10 minutes only when content changed."
        )
        self.task_label = QLabel("READY")
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        self.progress.setFixedWidth(128)
        self.progress.setTextVisible(False)
        self.progress.hide()
        status.addPermanentWidget(self.autosave_status)
        status.addPermanentWidget(self.task_label)
        status.addPermanentWidget(self.progress)
        self.setStatusBar(status)
        self._refresh_queue_table()
        if self._queue_load_error:
            self._append_log(f"RENDER QUEUE RECOVERY: {self._queue_load_error}")

    def _restore_workspace_layout(self) -> None:
        if not self._auto_workflows_enabled:
            return
        for splitter, key in (
            (self.workspace_splitter, "layout/workspaceSplitter"),
            (self.library_splitter, "layout/librarySplitter"),
        ):
            state = self.settings.value(key)
            if state is not None:
                splitter.restoreState(state)

    def _save_workspace_layout(self) -> None:
        if not self._auto_workflows_enabled:
            return
        self.settings.setValue(
            "layout/workspaceSplitter",
            self.workspace_splitter.saveState(),
        )
        self.settings.setValue(
            "layout/librarySplitter",
            self.library_splitter.saveState(),
        )

    def _build_menus(self) -> None:
        bar = self.menuBar()
        bar.setNativeMenuBar(sys.platform == "darwin")

        def action(menu, text: str, callback: Callable[[], None]) -> QAction:
            item = QAction(text, self)
            item.triggered.connect(callback)
            menu.addAction(item)
            return item

        def standard_action(
            menu,
            text: str,
            callback: Callable[[], None],
            standard_key: QKeySequence.StandardKey,
            ctrl_fallback: str,
        ) -> QAction:
            item = action(menu, text, callback)
            shortcuts = list(QKeySequence.keyBindings(standard_key))
            shortcuts.append(QKeySequence(ctrl_fallback))
            unique: dict[str, QKeySequence] = {}
            for shortcut in shortcuts:
                unique[shortcut.toString(QKeySequence.SequenceFormat.PortableText)] = shortcut
            item.setShortcuts(list(unique.values()))
            item.setShortcutContext(Qt.ShortcutContext.ApplicationShortcut)
            return item

        file_menu = bar.addMenu("File")
        action(file_menu, "New Project…", self.new_project)
        action(file_menu, "Open Project…", self.open_project)
        action(file_menu, "Storage Access…", self.manage_storage_access)
        file_menu.addSeparator()
        action(file_menu, "Import Media…", self.choose_videos)
        self.save_project_action = standard_action(
            file_menu,
            "Save Project",
            self.save_project,
            QKeySequence.StandardKey.Save,
            "Ctrl+S",
        )
        file_menu.addSeparator()
        action(file_menu, "Quit", self.close)

        edit_menu = bar.addMenu("Edit")
        self.undo_action = standard_action(
            edit_menu,
            "Undo",
            lambda: self._dispatch_edit_command("undo"),
            QKeySequence.StandardKey.Undo,
            "Ctrl+Z",
        )
        self.redo_action = standard_action(
            edit_menu,
            "Redo",
            lambda: self._dispatch_edit_command("redo"),
            QKeySequence.StandardKey.Redo,
            "Ctrl+Shift+Z",
        )
        edit_menu.addSeparator()
        self.cut_action = standard_action(
            edit_menu,
            "Cut",
            lambda: self._dispatch_edit_command("cut"),
            QKeySequence.StandardKey.Cut,
            "Ctrl+X",
        )
        self.copy_action = standard_action(
            edit_menu,
            "Copy",
            lambda: self._dispatch_edit_command("copy"),
            QKeySequence.StandardKey.Copy,
            "Ctrl+C",
        )
        self.paste_action = standard_action(
            edit_menu,
            "Paste",
            lambda: self._dispatch_edit_command("paste"),
            QKeySequence.StandardKey.Paste,
            "Ctrl+V",
        )
        self.select_all_action = standard_action(
            edit_menu,
            "Select All",
            lambda: self._dispatch_edit_command("select-all"),
            QKeySequence.StandardKey.SelectAll,
            "Ctrl+A",
        )
        edit_menu.addSeparator()
        action(edit_menu, "New Folder…", self.create_media_bin)
        action(edit_menu, "Rename Selected…", self.rename_focused_item)
        action(edit_menu, "Delete Selected…", self.delete_focused_item)

        project_menu = bar.addMenu("Project")
        action(project_menu, "Project Settings…", self.edit_project_settings)
        action(project_menu, "Open Project Folder", self.open_project_folder)

        timeline_menu = bar.addMenu("Timeline")
        action(timeline_menu, "New Timeline…", self.create_timeline)
        action(
            timeline_menu,
            "Add Selected Media to Active Timeline",
            self.add_selected_media_to_active_timeline,
        )
        action(timeline_menu, "Open Selected Timeline", self.open_selected_timeline)
        action(timeline_menu, "Timeline Settings…", self.edit_timeline_settings)
        action(timeline_menu, "Rename Timeline…", self.rename_timeline)
        action(timeline_menu, "Duplicate Timeline", self.duplicate_selected_timeline)
        action(timeline_menu, "Delete Timeline…", self.delete_selected_timeline)
        timeline_menu.addSeparator()
        action(timeline_menu, "Add Timeline to Render Queue", self.add_current_to_queue)

        playback_menu = bar.addMenu("Playback")
        self.fullscreen_action = action(
            playback_menu,
            "Full Screen Preview",
            lambda: self._handle_preview_command("fullscreen"),
        )
        self.fullscreen_action.setShortcut(QKeySequence(Qt.Key.Key_P))
        self.fullscreen_action.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        self.plate_move_action = action(
            playback_menu,
            "Move Selected Plate",
            lambda: self._handle_preview_shortcut("plate-move"),
        )
        self.plate_move_action.setCheckable(True)
        self.plate_move_action.setShortcut(QKeySequence(Qt.Key.Key_M))
        self.plate_move_action.setShortcutContext(
            Qt.ShortcutContext.ApplicationShortcut
        )
        playback_menu.addSeparator()
        action(playback_menu, "Play / Pause    Space", self.toggle_playback)
        action(playback_menu, "Reverse    J", lambda: self._handle_preview_command("reverse"))
        action(playback_menu, "Stop    K", lambda: self._handle_preview_command("stop"))
        action(playback_menu, "Forward    L", lambda: self._handle_preview_command("forward"))
        action(playback_menu, "Previous Frame    ←", lambda: self.step_playback(-1))
        action(playback_menu, "Next Frame    →", lambda: self.step_playback(1))

        tools_menu = bar.addMenu("Tools")
        action(tools_menu, "TC Align", self.align_timecode)
        action(tools_menu, "Quick Preview", self.create_preview)
        action(tools_menu, "Auto Stitch", self.auto_align)

        window_menu = bar.addMenu("Window")
        action(window_menu, "Inspector", lambda: self._show_right_tab(0))
        action(window_menu, "Render Queue", lambda: self._show_right_tab(1))
        action(window_menu, "Task Log", lambda: self._show_right_tab(2))

        help_menu = bar.addMenu("Help")
        action(help_menu, "Keyboard Shortcuts", self.show_shortcuts)
        self._update_edit_action_state()

    @staticmethod
    def _editable_project_payload(payload: dict[str, object]) -> dict[str, object]:
        """Remove generated cache state that should never create an undo step."""
        editable = json.loads(json.dumps(payload))
        for media in editable.get("media", []):
            if isinstance(media, dict):
                for key in (
                    "source_cache_path",
                    "source_cache_status",
                    "source_cache_error",
                ):
                    media.pop(key, None)
        for timeline in editable.get("timelines", []):
            if isinstance(timeline, dict):
                for key in (
                    "playback_cache_path",
                    "playback_cache_status",
                    "stitch_status",
                    "updated_at",
                ):
                    timeline.pop(key, None)
        return editable

    def _project_store_changed(
        self,
        before: dict[str, object],
        after: dict[str, object],
    ) -> None:
        if self._restoring_project_snapshot:
            return
        if self._editable_project_payload(before) == self._editable_project_payload(after):
            return
        if self._pending_project_undo is None:
            self._pending_project_undo = (
                json.loads(json.dumps(before)),
                self._active_timeline_id,
                self._active_bin_id,
            )
        self._undo_capture_timer.start()
        self._project_redo_stack.clear()
        self._update_edit_action_state()

    def _flush_pending_project_undo(self) -> None:
        pending = self._pending_project_undo
        self._pending_project_undo = None
        if pending is None:
            self._update_edit_action_state()
            return
        current = self.project_store.to_dict()
        if self._editable_project_payload(pending[0]) == self._editable_project_payload(current):
            self._update_edit_action_state()
            return
        if not self._project_undo_stack or (
            self._editable_project_payload(self._project_undo_stack[-1][0])
            != self._editable_project_payload(pending[0])
        ):
            self._project_undo_stack.append(pending)
            del self._project_undo_stack[:-64]
        self._update_edit_action_state()

    def _clear_project_history(self) -> None:
        self._undo_capture_timer.stop()
        self._pending_project_undo = None
        self._project_undo_stack.clear()
        self._project_redo_stack.clear()
        self._update_edit_action_state()

    def _update_edit_action_state(self) -> None:
        if hasattr(self, "undo_action"):
            self.undo_action.setEnabled(
                self._pending_project_undo is not None or bool(self._project_undo_stack)
            )
        if hasattr(self, "redo_action"):
            self.redo_action.setEnabled(bool(self._project_redo_stack))

    def _current_project_snapshot(
        self,
    ) -> tuple[dict[str, object], str | None, str | None]:
        return (
            json.loads(json.dumps(self.project_store.to_dict())),
            self._active_timeline_id,
            self._active_bin_id,
        )

    def _restore_project_snapshot(
        self,
        snapshot: tuple[dict[str, object], str | None, str | None],
        *,
        verb: str,
    ) -> bool:
        if self.process is not None:
            self.statusBar().showMessage(
                f"Finish {self._process_task_name or 'the current task'} before {verb.lower()}",
                7000,
            )
            return False
        payload, timeline_id, bin_id = snapshot
        self._restoring_project_snapshot = True
        try:
            self._live_preview_timer.stop()
            self._playback_warmup_timer.stop()
            self._live_preview_pending = False
            self._live_preview_revision += 1
            self.project_store.change_listener = None
            self._cancel_source_proxy_items()
            self._stop_playback(clear=True)
            restored = ProjectStore.from_dict(
                self.project_store.path,
                payload,
                autosave=self.project_store.autosave,
            )
            restored.save()
            restored.change_listener = self._project_store_changed
            self.project_store = restored
            self._last_autosave_digest = None
            self._last_autosave_at = None
            self._active_timeline_id = None
            known_bins = {item.id for item in restored.bins}
            self._active_bin_id = bin_id if bin_id in known_bins else None
            known_timelines = {item.id for item in restored.timelines}
            if timeline_id in known_timelines:
                self.load_project_timeline(str(timeline_id))
            else:
                self.clear_sources()
                self._apply_project_defaults()
                self._refresh_media_tree()
            self._update_project_header()
            self.statusBar().showMessage(f"{verb} complete", 5000)
            return True
        except (OSError, ProjectError, TypeError, ValueError) as error:
            self._error(verb, str(error))
            return False
        finally:
            self._restoring_project_snapshot = False
            self._update_edit_action_state()

    def _undo_project_edit(self) -> None:
        self._undo_capture_timer.stop()
        self._flush_pending_project_undo()
        if not self._project_undo_stack:
            self.statusBar().showMessage("Nothing to undo", 2500)
            return
        target = self._project_undo_stack.pop()
        current = self._current_project_snapshot()
        if self._restore_project_snapshot(target, verb="Undo"):
            self._project_redo_stack.append(current)
        else:
            self._project_undo_stack.append(target)
        self._update_edit_action_state()

    def _redo_project_edit(self) -> None:
        if not self._project_redo_stack:
            self.statusBar().showMessage("Nothing to redo", 2500)
            return
        target = self._project_redo_stack.pop()
        current = self._current_project_snapshot()
        if self._restore_project_snapshot(target, verb="Redo"):
            self._project_undo_stack.append(current)
        else:
            self._project_redo_stack.append(target)
        self._update_edit_action_state()

    @staticmethod
    def _focused_text_editor() -> QLineEdit | QPlainTextEdit | None:
        widget = QApplication.focusWidget()
        while widget is not None:
            if isinstance(widget, (QLineEdit, QPlainTextEdit)):
                return widget
            widget = widget.parentWidget()
        return None

    @staticmethod
    def _focus_within(widget: QWidget) -> bool:
        focused = QApplication.focusWidget()
        return focused is widget or (
            focused is not None and widget.isAncestorOf(focused)
        )

    def _dispatch_edit_command(self, command: str) -> None:
        editor = self._focused_text_editor()
        if editor is not None:
            if command == "undo":
                available = (
                    editor.isUndoAvailable()
                    if isinstance(editor, QLineEdit)
                    else editor.document().isUndoAvailable()
                )
                if available:
                    editor.undo()
                    return
            elif command == "redo":
                available = (
                    editor.isRedoAvailable()
                    if isinstance(editor, QLineEdit)
                    else editor.document().isRedoAvailable()
                )
                if available:
                    editor.redo()
                    return
            elif command == "cut":
                editor.cut()
                return
            elif command == "copy":
                editor.copy()
                return
            elif command == "paste":
                editor.paste()
                return
            elif command == "select-all":
                editor.selectAll()
                return
        if command == "undo":
            self._undo_project_edit()
        elif command == "redo":
            self._redo_project_edit()
        elif command == "copy":
            self._copy_contextual()
        elif command == "paste":
            self._paste_contextual()
        elif command == "select-all":
            self._select_all_contextual()
        elif command == "cut":
            self.statusBar().showMessage("Cut is available while editing text", 3000)

    def _copy_table_selection(self, table: QTableWidget) -> bool:
        indexes = [index for index in table.selectedIndexes() if not table.isColumnHidden(index.column())]
        if not indexes:
            return False
        rows = sorted({index.row() for index in indexes})
        columns = sorted({index.column() for index in indexes})
        lines: list[str] = []
        for row in rows:
            values = []
            for column in columns:
                item = table.item(row, column)
                values.append(item.text() if item is not None else "")
            lines.append("\t".join(values))
        QApplication.clipboard().setText("\n".join(lines))
        self.statusBar().showMessage(f"Copied {len(rows)} row{'s' if len(rows) != 1 else ''}", 3000)
        return True

    def _copy_contextual(self) -> None:
        if self._focus_within(self.media_tree):
            records = self._selected_media_records()
            if not records:
                return
            mime = QMimeData()
            paths = [str(record.path) for record in records]
            mime.setUrls([QUrl.fromLocalFile(path) for path in paths])
            mime.setText("\n".join(paths))
            QApplication.clipboard().setMimeData(mime)
            self.statusBar().showMessage(
                f"Copied {len(paths)} media path{'s' if len(paths) != 1 else ''}",
                3000,
            )
            return
        if self._focus_within(self.timeline_tree):
            kind, timeline_id = self._selected_timeline_item()
            if kind != "timeline" or not timeline_id:
                timeline_id = self._active_timeline_id
            timeline = next(
                (item for item in self.project_store.timelines if item.id == timeline_id),
                None,
            )
            if timeline is None:
                return
            mime = QMimeData()
            mime.setData(
                TIMELINE_CLIPBOARD_MIME,
                json.dumps(
                    {"version": 1, "timeline": timeline.to_dict()},
                    ensure_ascii=False,
                ).encode("utf-8"),
            )
            mime.setText(timeline.name)
            QApplication.clipboard().setMimeData(mime)
            self.statusBar().showMessage(f"Copied timeline: {timeline.name}", 3000)
            return
        for table in (self.source_table, self.queue_table):
            if self._focus_within(table) and self._copy_table_selection(table):
                return

    def _paste_contextual(self) -> None:
        mime = QApplication.clipboard().mimeData()
        if self._focus_within(self.timeline_tree) and mime.hasFormat(TIMELINE_CLIPBOARD_MIME):
            try:
                payload = json.loads(bytes(mime.data(TIMELINE_CLIPBOARD_MIME)).decode("utf-8"))
                if not isinstance(payload, dict) or payload.get("version") != 1:
                    raise ProjectError("Unsupported timeline clipboard data")
                raw = payload.get("timeline")
                if not isinstance(raw, dict):
                    raise ProjectError("Timeline clipboard data is incomplete")
                source = TimelineRecord.from_dict(raw)
                kind, selected_id = self._selected_timeline_item()
                known_bins = {item.id for item in self.project_store.bins}
                destination = (
                    str(selected_id)
                    if kind == "bin" and selected_id in known_bins
                    else source.bin_id
                    if source.bin_id in known_bins
                    else self._active_bin_id
                    if self._active_bin_id in known_bins
                    else None
                )
                duplicate = TimelineRecord.create(
                    name=self._unique_timeline_name(f"{source.name} Copy"),
                    source_paths=source.source_paths,
                    config_snapshot=source.config_snapshot,
                    inherits_project_settings=source.inherits_project_settings,
                    bin_id=destination,
                    tc_alignment_snapshot=source.tc_alignment_snapshot,
                    in_frame=source.in_frame,
                    out_frame=source.out_frame,
                    playback_cache_path=source.playback_cache_path,
                    playback_cache_status=source.playback_cache_status,
                    stitch_status=source.stitch_status,
                    order=len(self.project_store.list_timelines(destination)),
                )
                self.project_store.add_timeline(duplicate)
                self._active_timeline_id = duplicate.id
                self._active_bin_id = duplicate.bin_id
                self._remember_active_timeline()
                self._refresh_media_tree()
                self.statusBar().showMessage(f"Pasted timeline: {duplicate.name}", 4000)
            except (json.JSONDecodeError, UnicodeDecodeError, ProjectError, TypeError, ValueError) as error:
                self._error("Paste Timeline", str(error))
            return
        if self._focus_within(self.media_tree):
            paths = [url.toLocalFile() for url in mime.urls() if url.isLocalFile()]
            if not paths and mime.hasText():
                candidates = [line.strip() for line in mime.text().splitlines() if line.strip()]
                paths = [path for path in candidates if Path(path).is_file()]
            paths = [path for path in paths if Path(path).is_file()]
            if not paths:
                self.statusBar().showMessage("Clipboard contains no local media files", 3500)
                return
            try:
                added = self.import_media_paths(
                    paths,
                    destination_bin_id=self._media_tree_destination_bin(default_to_first=True),
                )
                self.statusBar().showMessage(
                    f"Pasted {len(added)} media file{'s' if len(added) != 1 else ''}",
                    4000,
                )
            except ProjectError as error:
                self._error("Paste Media", str(error))

    def _select_all_contextual(self) -> None:
        for widget in (self.media_tree, self.timeline_tree, self.source_table, self.queue_table):
            if self._focus_within(widget):
                widget.selectAll()
                return

    def save_project(self) -> None:
        saved = self._autosave_project_snapshot(force=True)
        self._undo_capture_timer.stop()
        self._flush_pending_project_undo()
        if not saved:
            self.statusBar().showMessage("Project save failed · see Task Log", 7000)
            return
        stamp = time.strftime("%H:%M")
        self.autosave_status.setText(f"SAVED · {stamp}")
        self.statusBar().showMessage(f"Project saved · {stamp}", 5000)
        self._append_log(f"Saved project: {self.project_store.path}")

    def _show_right_tab(self, index: int) -> None:
        self.inspector_panel.show()
        self.right_tabs.setCurrentIndex(index)
        self.inspector_toggle.setChecked(index == 0)
        self.jobs_toggle.setChecked(index == 1)

    def _update_project_header(self) -> None:
        if hasattr(self, "project_title"):
            self.project_title.setText(self.project_store.settings.name)
        self.setWindowTitle(
            f"{APP_NAME}  —  {self.project_store.settings.name}"
        )
        self._update_plate_set_context()

    def _last_timeline_setting_key(self) -> str:
        project_id = hashlib.sha1(
            str(self.project_store.path.resolve()).encode("utf-8")
        ).hexdigest()
        return f"projects/{project_id}/lastPlateSet"

    def _remember_active_timeline(self) -> None:
        if self._active_timeline_id:
            self.settings.setValue(
                self._last_timeline_setting_key(), self._active_timeline_id
            )
            self.settings.sync()

    def _restore_active_timeline(self) -> None:
        timelines = list(self.project_store.timelines)
        if not timelines or self.process is not None:
            self._update_plate_set_context()
            return
        remembered = str(self.settings.value(self._last_timeline_setting_key(), ""))
        timeline = next((item for item in timelines if item.id == remembered), None)
        if timeline is None:
            timeline = timelines[0]
        self.load_project_timeline(timeline.id)

    def _active_timeline_record(self) -> TimelineRecord | None:
        return next(
            (
                timeline
                for timeline in self.project_store.timelines
                if timeline.id == self._active_timeline_id
            ),
            None,
        )

    def _update_plate_set_context(self) -> None:
        timeline = self._active_timeline_record()
        if timeline is None:
            active_label = "ACTIVE TIMELINE · NONE"
            preview_label = f"{self.project_store.settings.name} / NO TIMELINE OPEN"
            range_label = "TIMELINE RANGE · NO TIMELINE"
        else:
            folder = next(
                (
                    item.name
                    for item in self.project_store.bins
                    if item.id == timeline.bin_id
                ),
                "Master",
            )
            plate_count = len(timeline.source_paths)
            plate_state = f"{plate_count} PLATES" if plate_count else "PLATES UNASSIGNED"
            active_label = f"ACTIVE TIMELINE · {timeline.name} · {plate_state}"
            preview_label = (
                f"{self.project_store.settings.name} / {folder} / {timeline.name}"
            )
            range_label = f"TIMELINE RANGE · {timeline.name}"
        if hasattr(self, "active_plates_title"):
            self.active_plates_title.setText(active_label)
            self.active_plates_title.setToolTip(active_label)
        if hasattr(self, "preview_context"):
            self.preview_context.setText(preview_label)
            self.preview_context.setToolTip(preview_label)
        if hasattr(self, "timing_title"):
            self.timing_title.setText(range_label)
            self.timing_title.setToolTip(range_label)

    @staticmethod
    def _settings_values(snapshot: dict[str, object]) -> dict[str, object]:
        output = snapshot.get("output")
        output = output if isinstance(output, dict) else {}
        video = snapshot.get("video")
        video = video if isinstance(video, dict) else {}
        metadata = snapshot.get("_vpstitch")
        metadata = metadata if isinstance(metadata, dict) else {}
        color = snapshot.get("color")
        color = color if isinstance(color, dict) else {}
        cameras = snapshot.get("cameras")
        input_space = "Camera Rec.709"
        if isinstance(cameras, list) and cameras and isinstance(cameras[0], dict):
            input_space = str(cameras[0].get("colorspace") or input_space)
        return {
            "width": int(output.get("width") or 20_000),
            "height": int(output.get("height") or 5_504),
            "fps_mode": str(metadata.get("fps_mode") or FPS_MODE_MATCH_SOURCE),
            "fps": float(video.get("fps") or 24.0),
            "mode": str(color.get("mode") or "ocio"),
            "ocio_config": str(color.get("ocio_config") or BUNDLED_ACES_STUDIO_ID),
            "input_space": input_space,
            "working_space": str(color.get("working_space") or "ACEScg"),
            "output_space": str(color.get("output_space") or "Gamma 2.4 Encoded Rec.709"),
            "output_mode": str(color.get("output_mode") or "colorspace"),
            "display": str(color.get("display") or "Rec.2100-PQ - Display"),
            "view": str(
                color.get("view") or "ACES 2.0 - HDR 1000 nits (Rec.2020)"
            ),
        }

    def _config_with_project_defaults(self, base: dict[str, object]) -> dict[str, object]:
        merged = json.loads(json.dumps(base))
        defaults = self._settings_values(self.project_store.settings.settings_snapshot)
        output = merged.setdefault("output", {})
        if isinstance(output, dict):
            output["width"] = defaults["width"]
            output["height"] = defaults["height"]
        metadata = merged.setdefault("_vpstitch", {})
        if not isinstance(metadata, dict):
            metadata = {}
            merged["_vpstitch"] = metadata
        metadata["fps_mode"] = defaults["fps_mode"]
        video = merged.setdefault("video", {})
        if isinstance(video, dict) and defaults["fps_mode"] == FPS_MODE_CUSTOM:
            video["fps"] = defaults["fps"]
        color = merged.setdefault("color", {})
        if isinstance(color, dict):
            color["mode"] = defaults["mode"]
            if defaults["mode"] == "ocio":
                color["ocio_config"] = defaults["ocio_config"]
                color["working_space"] = defaults["working_space"]
                color["output_mode"] = defaults["output_mode"]
                if defaults["output_mode"] == "display_view":
                    color["display"] = defaults["display"]
                    color["view"] = defaults["view"]
                    color.pop("output_space", None)
                else:
                    color["output_space"] = defaults["output_space"]
                    color.pop("display", None)
                    color.pop("view", None)
            else:
                for key in (
                    "ocio_config", "working_space", "output_space",
                    "output_mode", "display", "view",
                ):
                    color.pop(key, None)
        cameras = merged.get("cameras")
        if isinstance(cameras, list):
            for camera in cameras:
                if not isinstance(camera, dict):
                    continue
                if defaults["mode"] == "ocio":
                    camera["colorspace"] = defaults["input_space"]
                else:
                    camera.pop("colorspace", None)
        return merged

    def _effective_timeline_config(self, timeline: TimelineRecord) -> dict[str, object]:
        base = json.loads(json.dumps(timeline.config_snapshot))
        return self._config_with_project_defaults(base) if timeline.inherits_project_settings else base

    def _apply_project_defaults(self) -> None:
        if not hasattr(self, "canvas_width"):
            return
        values = self._settings_values(self.project_store.settings.settings_snapshot)
        self._loading_config = True
        self.canvas_width.setValue(int(values["width"]))
        self.canvas_height.setValue(int(values["height"]))
        self.fps_mode.setCurrentIndex(
            max(0, self.fps_mode.findData(str(values["fps_mode"])))
        )
        if values["fps_mode"] == FPS_MODE_CUSTOM:
            self.fps.setValue(float(values["fps"]))
        elif self._source_probes:
            source_fps = _matching_source_frame_rate(self._source_probes)
            if source_fps is not None:
                self.fps.setValue(source_fps)
        self.fps.setEnabled(values["fps_mode"] == FPS_MODE_CUSTOM)
        index = self.color_mode.findData(values["mode"])
        if index >= 0:
            self.color_mode.setCurrentIndex(index)
        self.ocio_config.setText(str(values["ocio_config"]))
        self.ocio_config.setCursorPosition(0)
        _request_ocio_combo_value(self.input_space, str(values["input_space"]))
        _request_ocio_combo_value(self.working_space, str(values["working_space"]))
        _request_ocio_combo_value(self.output_space, str(values["output_space"]))
        self.output_mode.setCurrentIndex(
            max(0, self.output_mode.findData(str(values["output_mode"])))
        )
        if values["mode"] == "ocio":
            self._reload_ocio_spaces(quiet=True)
            self._reload_ocio_delivery(
                display=str(values["display"]),
                view=str(values["view"]),
                quiet=True,
            )
        self._loading_config = False

    def edit_project_settings(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Project Settings")
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        title = QLabel("PROJECT DEFAULTS")
        title.setProperty("sectionTitle", True)
        layout.addWidget(title)
        form = QFormLayout()
        defaults = self._settings_values(self.project_store.settings.settings_snapshot)
        name = QLineEdit(self.project_store.settings.name)
        width = QSpinBox()
        width.setRange(640, MAX_CANVAS_WIDTH)
        width.setValue(int(defaults["width"]))
        height = QSpinBox()
        height.setRange(320, MAX_CANVAS_HEIGHT)
        height.setValue(int(defaults["height"]))
        fps_mode = ChevronComboBox()
        fps_mode.addItem("Match each plate set", FPS_MODE_MATCH_SOURCE)
        fps_mode.addItem("Custom conform", FPS_MODE_CUSTOM)
        fps_mode.setCurrentIndex(max(0, fps_mode.findData(defaults["fps_mode"])))
        fps = QDoubleSpinBox()
        fps.setRange(1.0, 240.0)
        fps.setDecimals(6)
        fps.setValue(float(defaults["fps"]))
        fps.setEnabled(fps_mode.currentData() == FPS_MODE_CUSTOM)
        fps_mode.currentIndexChanged.connect(
            lambda _index: fps.setEnabled(fps_mode.currentData() == FPS_MODE_CUSTOM)
        )
        color_mode = ChevronComboBox()
        color_mode.addItem("Passthrough", "passthrough")
        color_mode.addItem("OCIO", "ocio")
        current_mode = color_mode.findData(defaults["mode"])
        color_mode.setCurrentIndex(max(0, current_mode))
        ocio = QLineEdit(str(defaults["ocio_config"]))
        input_space = _new_ocio_space_combo(str(defaults["input_space"]))
        working_space = _new_ocio_space_combo(str(defaults["working_space"]))
        output_space = _new_ocio_space_combo(str(defaults["output_space"]), output=True)
        output_mode = ChevronComboBox()
        output_mode.addItem("Color space / Log", "colorspace")
        output_mode.addItem("Display transform", "display_view")
        output_mode.setCurrentIndex(max(0, output_mode.findData(defaults["output_mode"])))
        output_display = ChevronComboBox()
        output_view = ChevronComboBox()
        output_space_label = QLabel("Output color space")
        output_display_label = QLabel("Display")
        output_view_label = QLabel("View")
        ocio_spaces_status = QLabel()
        ocio_spaces_status.setProperty("muted", True)
        load_spaces = QPushButton("LOAD SPACES")
        load_spaces.setObjectName("secondaryButton")
        ocio_row = QWidget()
        ocio_layout = QHBoxLayout(ocio_row)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.addWidget(ocio)
        ocio_layout.addWidget(load_spaces)
        for field in (ocio, input_space, working_space, output_space):
            field.setMinimumWidth(320)

        def reload_spaces() -> None:
            try:
                count = _load_ocio_combo_group(
                    ocio.text().strip(),
                    (input_space, working_space, output_space),
                )
                displays = ocio_display_views(ocio.text().strip())
                _populate_delivery_display_combo(
                    output_display, tuple(displays), str(defaults["display"])
                )
                _populate_combo(
                    output_view,
                    displays.get(_delivery_display_value(output_display), ()),
                    str(defaults["view"]),
                )
                ocio_spaces_status.setText(f"{count} OCIO spaces loaded")
            except Exception as error:
                ocio_spaces_status.setText(f"Could not read OCIO config · {error}")

        load_spaces.clicked.connect(reload_spaces)
        ocio.editingFinished.connect(reload_spaces)
        output_display.currentIndexChanged.connect(
            lambda _index: _populate_combo(
                output_view,
                ocio_display_views(ocio.text().strip()).get(
                    _delivery_display_value(output_display), ()
                ),
                "",
            )
        )

        def update_delivery() -> None:
            hdr = output_mode.currentData() == "display_view"
            output_space.setVisible(not hdr)
            output_space_label.setVisible(not hdr)
            output_display.setVisible(hdr)
            output_display_label.setVisible(hdr)
            output_view.setVisible(hdr)
            output_view_label.setVisible(hdr)

        output_mode.currentIndexChanged.connect(update_delivery)
        reload_spaces()
        form.addRow("Project name", name)
        form.addRow("Default canvas width", width)
        form.addRow("Default canvas height", height)
        form.addRow("Timeline frame rate", fps_mode)
        form.addRow("Custom FPS", fps)
        form.addRow("Color pipeline", color_mode)
        form.addRow("OCIO config", ocio_row)
        form.addRow("Input transform", input_space)
        form.addRow("Working space", working_space)
        form.addRow("Delivery method", output_mode)
        form.addRow(output_space_label, output_space)
        form.addRow(output_display_label, output_display)
        form.addRow(output_view_label, output_view)
        form.addRow(ocio_spaces_status)
        update_delivery()
        layout.addLayout(form)
        note = QLabel(
            "New timelines inherit these values. Timelines set to Use Project Settings update immediately."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            settings_snapshot = json.loads(
                json.dumps(self.project_store.settings.settings_snapshot)
            )
            previous_basis = self._color_match_basis(settings_snapshot)
            previous_color = dict(settings_snapshot.get("color") or {})
            output = settings_snapshot.get("output")
            if not isinstance(output, dict):
                output = {}
                settings_snapshot["output"] = output
            output.update({"width": width.value(), "height": height.value()})
            metadata = settings_snapshot.setdefault("_vpstitch", {})
            if not isinstance(metadata, dict):
                metadata = {}
                settings_snapshot["_vpstitch"] = metadata
            selected_fps_mode = str(fps_mode.currentData())
            metadata["fps_mode"] = selected_fps_mode
            if selected_fps_mode == FPS_MODE_CUSTOM:
                settings_snapshot.setdefault("video", {})["fps"] = fps.value()
            mode = str(color_mode.currentData())
            settings_snapshot["color"] = {"mode": mode}
            if mode == "ocio":
                delivery_mode = str(output_mode.currentData())
                required = [
                    ocio.text().strip(), input_space.currentText().strip(),
                    working_space.currentText().strip(),
                ]
                if delivery_mode == "display_view":
                    required.extend(
                        [_delivery_display_value(output_display), output_view.currentText().strip()]
                    )
                else:
                    required.append(output_space.currentText().strip())
                if not all(required):
                    raise ValueError("OCIO config and selected delivery fields are required")
                settings_snapshot["color"].update(
                    {
                        "ocio_config": required[0],
                        "working_space": required[2],
                        "output_mode": delivery_mode,
                    }
                )
                if delivery_mode == "display_view":
                    settings_snapshot["color"].update(
                        {"display": required[3], "view": required[4]}
                    )
                else:
                    settings_snapshot["color"]["output_space"] = required[3]
                cameras = settings_snapshot.get("cameras")
                if not isinstance(cameras, list) or not cameras:
                    cameras = [{}]
                    settings_snapshot["cameras"] = cameras
                for camera in cameras:
                    if isinstance(camera, dict):
                        camera["colorspace"] = required[1]
            for key in ("match_reference", "match_strength", "preserve_luminance"):
                if key in previous_color:
                    settings_snapshot["color"][key] = previous_color[key]
            color_basis_changed = (
                previous_basis != self._color_match_basis(settings_snapshot)
            )
            if color_basis_changed:
                self._clear_color_match_snapshot(settings_snapshot)
            elif "match_enabled" in previous_color:
                settings_snapshot["color"]["match_enabled"] = bool(
                    previous_color["match_enabled"]
                )
            self.project_store.update_settings(
                name=name.text().strip(), settings_snapshot=settings_snapshot
            )
            self._invalidate_inherited_timeline_caches(
                reset_color_match=color_basis_changed
            )
        except Exception as error:
            self._error("Project Settings", str(error))
            return
        active = self._active_timeline_record()
        if active is None or active.inherits_project_settings:
            self._apply_project_defaults()
        if active is not None and active.inherits_project_settings:
            self._stop_playback(clear=True)
            self._cleanup_reference_dir(self._last_reference_dir)
            self._last_reference_dir = None
            self._last_reference_config_path = None
            self._reference_frame_index = None
            self._preview_ready = False
            self.rig_align_button.setEnabled(False)
            self.preview.show_message("PROJECT SETTINGS CHANGED · QUICK PREVIEW")
        self._update_project_header()
        self._refresh_media_tree()

    @staticmethod
    def _color_match_basis(config: dict[str, object]) -> tuple[object, ...]:
        color = config.get("color")
        color = color if isinstance(color, dict) else {}
        mode = str(color.get("mode", "passthrough"))
        if mode != "ocio":
            return (mode,)
        cameras = config.get("cameras")
        camera_spaces = tuple(
            str(camera.get("colorspace", ""))
            for camera in (cameras if isinstance(cameras, list) else [])
            if isinstance(camera, dict)
        )
        return (
            mode,
            str(color.get("ocio_config", "")),
            str(color.get("working_space", "")),
            camera_spaces,
        )

    @staticmethod
    def _clear_color_match_snapshot(config: dict[str, object]) -> None:
        color = config.get("color")
        if not isinstance(color, dict):
            color = {"mode": "passthrough"}
            config["color"] = color
        color["match_enabled"] = False
        color.pop("match_space", None)
        cameras = config.get("cameras")
        if not isinstance(cameras, list):
            return
        for camera in cameras:
            if isinstance(camera, dict):
                camera["color_gain"] = [1.0, 1.0, 1.0]
                camera.pop("color_match_confidence", None)

    def _invalidate_inherited_timeline_caches(
        self, *, reset_color_match: bool = False
    ) -> None:
        for timeline in tuple(self.project_store.timelines):
            if not timeline.inherits_project_settings:
                continue
            updates: dict[str, object] = {}
            if reset_color_match:
                snapshot = json.loads(json.dumps(timeline.config_snapshot))
                self._clear_color_match_snapshot(snapshot)
                updates["config_snapshot"] = snapshot
            self.project_store.update_timeline(
                timeline.id,
                playback_cache_path=None,
                playback_cache_status=(
                    PlaybackCacheStatus.PENDING
                    if timeline.source_paths else PlaybackCacheStatus.EMPTY
                ),
                stitch_status=StitchStatus.UNSTITCHED,
                **updates,
            )

    def edit_timeline_settings(self) -> None:
        kind, timeline_id = self._selected_timeline_item()
        if kind != "timeline" or not timeline_id:
            timeline_id = self._active_timeline_id
        timeline = next(
            (item for item in self.project_store.timelines if item.id == timeline_id),
            None,
        )
        if timeline is None:
            self._error("Timeline Settings", "Create or open a timeline first")
            return
        values = self._settings_values(self._effective_timeline_config(timeline))
        dialog = QDialog(self)
        dialog.setWindowTitle(f"Timeline Settings · {timeline.name}")
        dialog.setMinimumWidth(620)
        layout = QVBoxLayout(dialog)
        title = QLabel("TIMELINE OVERRIDES")
        title.setProperty("sectionTitle", True)
        layout.addWidget(title)
        inherit = QCheckBox("Use Project Settings")
        inherit.setChecked(timeline.inherits_project_settings)
        inherit.setToolTip("When enabled, project resolution and OCIO transforms stay linked")
        layout.addWidget(inherit)
        form = QFormLayout()
        width = QSpinBox()
        width.setRange(640, MAX_CANVAS_WIDTH)
        width.setValue(int(values["width"]))
        height = QSpinBox()
        height.setRange(320, MAX_CANVAS_HEIGHT)
        height.setValue(int(values["height"]))
        fps_mode = ChevronComboBox()
        fps_mode.addItem("Match plate set", FPS_MODE_MATCH_SOURCE)
        fps_mode.addItem("Custom conform", FPS_MODE_CUSTOM)
        fps_mode.setCurrentIndex(max(0, fps_mode.findData(values["fps_mode"])))
        fps = QDoubleSpinBox()
        fps.setRange(1.0, 240.0)
        fps.setDecimals(6)
        fps.setSingleStep(0.001)
        fps.setValue(float(values["fps"]))
        fps.setEnabled(fps_mode.currentData() == FPS_MODE_CUSTOM)
        fps_mode.currentIndexChanged.connect(
            lambda _index: fps.setEnabled(
                not inherit.isChecked()
                and fps_mode.currentData() == FPS_MODE_CUSTOM
            )
        )
        color_mode = ChevronComboBox()
        color_mode.addItem("Passthrough", "passthrough")
        color_mode.addItem("OCIO", "ocio")
        color_mode.setCurrentIndex(max(0, color_mode.findData(values["mode"])))
        ocio = QLineEdit(str(values["ocio_config"]))
        input_space = _new_ocio_space_combo(str(values["input_space"]))
        working_space = _new_ocio_space_combo(str(values["working_space"]))
        output_space = _new_ocio_space_combo(str(values["output_space"]), output=True)
        output_mode = ChevronComboBox()
        output_mode.addItem("Color space / Log", "colorspace")
        output_mode.addItem("Display transform", "display_view")
        output_mode.setCurrentIndex(max(0, output_mode.findData(values["output_mode"])))
        output_display = ChevronComboBox()
        output_view = ChevronComboBox()
        output_space_label = QLabel("Output color space")
        output_display_label = QLabel("Display")
        output_view_label = QLabel("View")
        ocio_spaces_status = QLabel()
        ocio_spaces_status.setProperty("muted", True)
        load_spaces = QPushButton("LOAD SPACES")
        load_spaces.setObjectName("secondaryButton")
        ocio_row = QWidget()
        ocio_layout = QHBoxLayout(ocio_row)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.addWidget(ocio)
        ocio_layout.addWidget(load_spaces)
        for field in (ocio, input_space, working_space, output_space):
            field.setMinimumWidth(320)

        def reload_spaces() -> None:
            try:
                count = _load_ocio_combo_group(
                    ocio.text().strip(),
                    (input_space, working_space, output_space),
                )
                displays = ocio_display_views(ocio.text().strip())
                _populate_delivery_display_combo(
                    output_display, tuple(displays), str(values["display"])
                )
                _populate_combo(
                    output_view,
                    displays.get(_delivery_display_value(output_display), ()),
                    str(values["view"]),
                )
                ocio_spaces_status.setText(f"{count} OCIO spaces loaded")
            except Exception as error:
                ocio_spaces_status.setText(f"Could not read OCIO config · {error}")

        load_spaces.clicked.connect(reload_spaces)
        ocio.editingFinished.connect(reload_spaces)
        output_display.currentIndexChanged.connect(
            lambda _index: _populate_combo(
                output_view,
                ocio_display_views(ocio.text().strip()).get(
                    _delivery_display_value(output_display), ()
                ),
                "",
            )
        )

        def update_delivery() -> None:
            hdr = output_mode.currentData() == "display_view"
            output_space.setVisible(not hdr)
            output_space_label.setVisible(not hdr)
            output_display.setVisible(hdr)
            output_display_label.setVisible(hdr)
            output_view.setVisible(hdr)
            output_view_label.setVisible(hdr)

        output_mode.currentIndexChanged.connect(update_delivery)
        reload_spaces()
        form.addRow("Canvas width", width)
        form.addRow("Canvas height", height)
        form.addRow("Timeline frame rate", fps_mode)
        form.addRow("Custom FPS", fps)
        form.addRow("Color pipeline", color_mode)
        form.addRow("OCIO config", ocio_row)
        form.addRow("Input transform", input_space)
        form.addRow("Working space", working_space)
        form.addRow("Delivery method", output_mode)
        form.addRow(output_space_label, output_space)
        form.addRow(output_display_label, output_display)
        form.addRow(output_view_label, output_view)
        form.addRow(ocio_spaces_status)
        update_delivery()
        layout.addLayout(form)
        override_widgets = (
            width, height, fps_mode, fps, color_mode, ocio_row, input_space, working_space,
            output_mode, output_space, output_display, output_view,
            ocio_spaces_status
        )

        def update_override_state(checked: bool) -> None:
            for widget in override_widgets:
                widget.setEnabled(not checked)
            fps.setEnabled(
                not checked and fps_mode.currentData() == FPS_MODE_CUSTOM
            )

        inherit.toggled.connect(update_override_state)
        update_override_state(inherit.isChecked())
        note = QLabel(
            "Disable Use Project Settings only when this timeline needs a different canvas, frame rate, or color transform."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        try:
            config = self._effective_timeline_config(timeline)
            if not inherit.isChecked():
                previous_basis = self._color_match_basis(config)
                previous_color = dict(config.get("color") or {})
                config.setdefault("output", {}).update(
                    {"width": width.value(), "height": height.value()}
                )
                metadata = config.setdefault("_vpstitch", {})
                if not isinstance(metadata, dict):
                    metadata = {}
                    config["_vpstitch"] = metadata
                selected_fps_mode = str(fps_mode.currentData())
                metadata["fps_mode"] = selected_fps_mode
                if selected_fps_mode == FPS_MODE_CUSTOM:
                    config.setdefault("video", {})["fps"] = fps.value()
                mode = str(color_mode.currentData())
                config["color"] = {"mode": mode}
                if mode == "ocio":
                    delivery_mode = str(output_mode.currentData())
                    required = [
                        ocio.text().strip(), input_space.currentText().strip(),
                        working_space.currentText().strip(),
                    ]
                    if delivery_mode == "display_view":
                        required.extend(
                            [_delivery_display_value(output_display), output_view.currentText().strip()]
                        )
                    else:
                        required.append(output_space.currentText().strip())
                    if not all(required):
                        raise ValueError(
                            "OCIO config and selected delivery fields are required"
                        )
                    config["color"].update(
                        {
                            "ocio_config": required[0],
                            "working_space": required[2],
                            "output_mode": delivery_mode,
                        }
                    )
                    if delivery_mode == "display_view":
                        config["color"].update(
                            {"display": required[3], "view": required[4]}
                        )
                    else:
                        config["color"]["output_space"] = required[3]
                    for camera in config.get("cameras", []):
                        if isinstance(camera, dict):
                            camera["colorspace"] = required[1]
                else:
                    for camera in config.get("cameras", []):
                        if isinstance(camera, dict):
                            camera.pop("colorspace", None)
                for key in (
                    "match_reference", "match_strength", "preserve_luminance"
                ):
                    if key in previous_color:
                        config["color"][key] = previous_color[key]
                if previous_basis != self._color_match_basis(config):
                    self._clear_color_match_snapshot(config)
                elif "match_enabled" in previous_color:
                    config["color"]["match_enabled"] = bool(
                        previous_color["match_enabled"]
                    )
            updated = self.project_store.update_timeline(
                timeline.id,
                config_snapshot=config,
                inherits_project_settings=inherit.isChecked(),
            )
            if self._active_timeline_id == updated.id:
                self._active_timeline_id = None
                self.load_project_timeline(updated.id)
            self._refresh_media_tree()
        except Exception as error:
            self._error("Timeline Settings", str(error))

    def new_project(self) -> None:
        manager = ProjectManagerDialog(self)
        try:
            manager.create_project()
            if manager.project_path is not None:
                self._switch_project(ProjectStore.load(manager.project_path))
        except Exception as error:
            self._error("New Project", str(error))

    def open_project(self) -> None:
        path, _ = QFileDialog.getOpenFileName(
            self,
            "Open VP Stitch Project",
            _preferred_storage_directory(
                self.settings,
                "storage/lastProjectDir",
                self.user_data_root / "projects",
            ),
            "VP Stitch project (project.json *.vpstitch);;JSON (*.json)",
        )
        if not path:
            return
        try:
            self._switch_project(ProjectStore.load(path))
            self.settings.setValue("storage/lastProjectDir", str(Path(path).parent))
            _remember_storage_root(self.settings, Path(path).parent)
            self.settings.sync()
        except ProjectError as error:
            self._error("Open Project", str(error))

    def manage_storage_access(self) -> None:
        StorageAccessDialog(self).exec()

    def _switch_project(self, store: ProjectStore) -> None:
        self._save_active_timeline()
        self._cancel_source_proxy_items()
        self.project_store.change_listener = None
        self.project_store = store
        self.project_store.change_listener = self._project_store_changed
        self._last_autosave_digest = None
        self._last_autosave_at = None
        self._active_timeline_id = None
        self._active_bin_id = None
        project_directory = store.path.parent
        self._working_dir = project_directory / "work"
        self._cache_dir = project_directory / "cache"
        self._output_root = project_directory / "renders"
        for directory in (self._working_dir, self._cache_dir, self._output_root):
            directory.mkdir(parents=True, exist_ok=True)
        try:
            self.render_queue = RenderQueueStore.load(project_directory / "render-queue.json")
        except RenderQueueError:
            self.render_queue = RenderQueueStore(project_directory / "render-queue.recovered.json")
        self.settings.setValue("lastProject", str(store.path))
        self.settings.sync()
        self.clear_sources()
        self._apply_project_defaults()
        self._update_project_header()
        self._refresh_media_tree()
        self._refresh_queue_table()
        if self._auto_workflows_enabled:
            self._restore_active_timeline()
            self._queue_source_proxies(list(self.project_store.media))
        self._clear_project_history()

    def _autosave_project_snapshot(self, *, force: bool = False) -> bool:
        """Refresh a low-frequency recovery copy only when project data changed."""
        try:
            self._save_active_timeline(strict=True)
            encoded = json.dumps(
                self.project_store.to_dict(),
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode("utf-8")
            digest = hashlib.sha256(encoded).hexdigest()
            if not force and digest == self._last_autosave_digest:
                stamp = (
                    time.strftime("%H:%M", time.localtime(self._last_autosave_at))
                    if self._last_autosave_at is not None
                    else "VERIFIED"
                )
                self.autosave_status.setText(f"AUTOSAVE · UP TO DATE · {stamp}")
                return False
            self.project_store.save()
            self.project_store.save_copy(
                self.project_store.path.with_name("project.autosave.json")
            )
            self._last_autosave_digest = digest
            self._last_autosave_at = time.time()
            self.autosave_status.setText(
                f"AUTOSAVED · {time.strftime('%H:%M', time.localtime(self._last_autosave_at))}"
            )
            return True
        except (OSError, ProjectError, TypeError, ValueError) as error:
            self.autosave_status.setText("AUTOSAVE · ERROR")
            self._append_log(f"AUTOSAVE ERROR: {error}")
            return False

    def open_project_folder(self) -> None:
        QProcess.startDetached("open" if sys.platform == "darwin" else "explorer", [str(self.project_store.path.parent)])

    def _selected_media_item(self) -> tuple[str | None, str | None]:
        item = self.media_tree.currentItem()
        if item is None:
            return None, None
        return item.data(0, Qt.ItemDataRole.UserRole), item.data(0, Qt.ItemDataRole.UserRole + 1)

    def _media_selection_changed(self) -> None:
        kind, item_id = self._selected_media_item()
        if kind == "bin" and item_id:
            self._active_bin_id = str(item_id)
        elif kind == "media" and item_id:
            record = next(
                (item for item in self.project_store.media if item.id == item_id),
                None,
            )
            self._active_bin_id = record.bin_id if record is not None else None
        elif kind == "project":
            self._active_bin_id = None
        self._update_source_status()

    def _media_drag_status_changed(self, message: str) -> None:
        if message:
            self.statusBar().showMessage(message)
        else:
            self.statusBar().clearMessage()

    def _media_tree_destination_bin(
        self, *, default_to_first: bool = False
    ) -> str | None:
        kind, item_id = self._selected_media_item()
        if kind == "bin" and item_id:
            return str(item_id)
        if kind == "media" and item_id:
            record = next(
                (item for item in self.project_store.media if item.id == item_id),
                None,
            )
            return record.bin_id if record is not None else None
        if kind == "project":
            return None
        if self._active_bin_id is not None:
            return self._active_bin_id
        if default_to_first and self.project_store.bins:
            return self.project_store.list_bins()[0].id
        return None

    def _bin_display_path(self, bin_id: str | None) -> str:
        if bin_id is None:
            return self.project_store.settings.name
        bins = {item.id: item for item in self.project_store.bins}
        names: list[str] = []
        current_id: str | None = bin_id
        visited: set[str] = set()
        while current_id is not None and current_id not in visited:
            visited.add(current_id)
            current = bins.get(current_id)
            if current is None:
                break
            names.append(current.name)
            current_id = current.parent_id
        return " / ".join(reversed(names)) or self.project_store.settings.name

    def _selected_timeline_item(self) -> tuple[str | None, str | None]:
        item = self.timeline_tree.currentItem()
        if item is None:
            return None, None
        return item.data(0, Qt.ItemDataRole.UserRole), item.data(0, Qt.ItemDataRole.UserRole + 1)

    def _selected_media_records(self) -> list[MediaRecord]:
        selected_ids = {
            str(item.data(0, Qt.ItemDataRole.UserRole + 1))
            for item in self.media_tree.selectedItems()
            if item.data(0, Qt.ItemDataRole.UserRole) == "media"
        }
        return [item for item in self.project_store.media if item.id in selected_ids]

    def delete_focused_item(self) -> None:
        if self.timeline_tree.hasFocus():
            self.delete_selected_timeline()
        else:
            self.delete_selected_media_items()

    def delete_selected_media_items(self) -> None:
        records = self._selected_media_records()
        kind, item_id = self._selected_media_item()
        if records:
            count = len(records)
            names = ", ".join(Path(str(item.path)).name for item in records[:3])
            if count > 3:
                names += f" and {count - 3} more"
            if self._show_message(
                QMessageBox.Icon.Question,
                "Remove Media from Project",
                (
                    f"Remove {count} selected clip{'s' if count != 1 else ''} from the Media Pool?\n\n"
                    f"{names}\n\nSource files stay on disk. Existing timelines keep their source references."
                ),
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            ) != QMessageBox.StandardButton.Yes:
                return
            self._cancel_source_proxy_items({record.id for record in records})
            try:
                for record in records:
                    self.project_store.remove_media(record.id)
            except ProjectError as error:
                self._error("Remove Media", str(error))
                return
            self._refresh_media_tree()
            self.statusBar().showMessage(
                f"Removed {count} clip{'s' if count != 1 else ''} from Media Pool · files remain on disk",
                8000,
            )
            return

        if kind != "bin" or not item_id:
            return
        folder = next(
            (item for item in self.project_store.bins if item.id == item_id),
            None,
        )
        if folder is None:
            return
        descendants = {folder.id}
        changed = True
        while changed:
            before = len(descendants)
            descendants.update(
                item.id
                for item in self.project_store.bins
                if item.parent_id in descendants
            )
            changed = len(descendants) != before
        media_count = sum(
            item.bin_id in descendants for item in self.project_store.media
        )
        timeline_count = sum(
            item.bin_id in descendants for item in self.project_store.timelines
        )
        if self._show_message(
            QMessageBox.Icon.Question,
            "Remove Folder from Project",
            (
                f'Remove folder “{folder.name}” and its contents from this project?\n\n'
                f"{media_count} media clip(s) · {timeline_count} timeline(s)\n\n"
                "Source files stay on disk."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        removed_media_ids = {
            item.id for item in self.project_store.media if item.bin_id in descendants
        }
        self._cancel_source_proxy_items(removed_media_ids)
        try:
            self.project_store.remove_bin(
                folder.id,
                recursive=(len(descendants) > 1 or media_count > 0 or timeline_count > 0),
            )
        except ProjectError as error:
            self._error("Remove Folder", str(error))
            return
        remaining_timeline_ids = {item.id for item in self.project_store.timelines}
        if self._active_timeline_id not in remaining_timeline_ids:
            self._active_timeline_id = None
            self.settings.remove(self._last_timeline_setting_key())
            self.clear_sources()
        self._active_bin_id = folder.parent_id
        self._refresh_media_tree()
        self.statusBar().showMessage(
            f'Removed folder “{folder.name}” from project · files remain on disk',
            8000,
        )

    def create_media_bin(self) -> None:
        parent_id = self._media_tree_destination_bin()
        destination = self._bin_display_path(parent_id)
        name, accepted = QInputDialog.getText(
            self,
            "New Folder",
            f"Folder name\nLocation: {destination}",
        )
        if not accepted or not name.strip():
            return
        try:
            created = self.project_store.add_bin(
                Bin.create(name.strip(), parent_id=parent_id)
            )
            self._active_bin_id = created.id
            self._refresh_media_tree()
            self.statusBar().showMessage(
                f'Created “{created.name}” in {destination}',
                7000,
            )
        except ProjectError as error:
            self._error("New Folder", str(error))

    def _request_new_timeline(
        self, default_name: str
    ) -> tuple[str, int, bool] | None:
        records = self._selected_media_records()
        selected_count = len(records)
        cameras = self.config_data.get("cameras")
        current_count = len(cameras) if isinstance(cameras, list) else 5
        suggested_count = (
            selected_count
            if selected_count in SUPPORTED_CAMERA_COUNTS
            else current_count
            if current_count in SUPPORTED_CAMERA_COUNTS
            else 5
        )
        dialog = NewTimelineDialog(
            self,
            default_name=default_name,
            suggested_count=suggested_count,
            selected_plate_count=selected_count,
            selected_media_names=[record.path.name for record in records],
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        name, count, add_selected = dialog.values()
        if not name:
            return None
        return name, count, add_selected

    def create_timeline(self) -> None:
        existing_names = {item.name for item in self.project_store.timelines}
        number = 1
        default_name = f"Timeline {number:02d}"
        while default_name in existing_names:
            number += 1
            default_name = f"Timeline {number:02d}"
        requested = self._request_new_timeline(default_name)
        if requested is None:
            return
        name, camera_count, add_selected = requested
        bin_id = self._media_tree_destination_bin(default_to_first=True)
        try:
            source_paths: tuple[str, ...] = ()
            plate_range = (
                "P06–P08" if camera_count == 3 else "P01–P05"
            )
            if add_selected:
                ordered = self._request_camera_assignment(
                    self._selected_media_records(), camera_count
                )
                if ordered is None:
                    return
                source_paths = tuple(ordered)
            config = self._config_with_project_defaults(
                self._profile_for_count(camera_count)
            )
            timeline = TimelineRecord.create(
                name=self._unique_timeline_name(name),
                source_paths=source_paths,
                config_snapshot=config,
                inherits_project_settings=True,
                bin_id=bin_id,
                playback_cache_status=(
                    PlaybackCacheStatus.PENDING
                    if source_paths
                    else PlaybackCacheStatus.EMPTY
                ),
            )
            self.project_store.add_timeline(timeline)
            self.load_project_timeline(timeline.id)
            if source_paths and self._auto_workflows_enabled:
                self._analyze_imported_sources()
            self.statusBar().showMessage(
                f"Created {timeline.name} · {camera_count}-camera {plate_range}"
                + (" · selected plates added" if source_paths else " · ready for plates"),
                10000,
            )
        except Exception as error:
            self._error("New Timeline", str(error))

    def _move_media_tree_items(self, payload, destination) -> None:  # type: ignore[no-untyped-def]
        if not isinstance(payload, list) or not isinstance(destination, dict):
            return
        destination_value = destination.get("bin_id")
        destination_bin_id = (
            str(destination_value) if destination_value not in {None, ""} else None
        )
        bins_by_id = {item.id: item for item in self.project_store.bins}
        media_by_id = {item.id: item for item in self.project_store.media}
        if destination_bin_id is not None and destination_bin_id not in bins_by_id:
            self._error("Move Media Pool Items", "The destination folder no longer exists.")
            return

        requested_bins: list[str] = []
        requested_media: list[str] = []
        for entry in payload:
            if not isinstance(entry, dict):
                continue
            kind = entry.get("kind")
            item_id = str(entry.get("id") or "")
            if kind == "bin" and item_id in bins_by_id and item_id not in requested_bins:
                requested_bins.append(item_id)
            elif kind == "media" and item_id in media_by_id and item_id not in requested_media:
                requested_media.append(item_id)

        selected_bin_ids = set(requested_bins)

        def has_selected_ancestor(bin_id: str) -> bool:
            parent_id = bins_by_id[bin_id].parent_id
            while parent_id is not None:
                if parent_id in selected_bin_ids:
                    return True
                parent = bins_by_id.get(parent_id)
                parent_id = parent.parent_id if parent is not None else None
            return False

        moving_bins = [
            bin_id for bin_id in requested_bins if not has_selected_ancestor(bin_id)
        ]

        def bin_is_inside(bin_id: str | None, possible_parent: str) -> bool:
            current_id = bin_id
            visited: set[str] = set()
            while current_id is not None and current_id not in visited:
                if current_id == possible_parent:
                    return True
                visited.add(current_id)
                current = bins_by_id.get(current_id)
                current_id = current.parent_id if current is not None else None
            return False

        for bin_id in moving_bins:
            if destination_bin_id == bin_id or bin_is_inside(destination_bin_id, bin_id):
                self._error(
                    "Move Media Pool Items",
                    "A folder cannot be moved into itself or one of its subfolders.",
                )
                return

        moving_media = [
            media_id
            for media_id in requested_media
            if not any(
                bin_is_inside(media_by_id[media_id].bin_id, bin_id)
                for bin_id in moving_bins
            )
        ]
        if not moving_bins and not moving_media:
            return

        insertion_kind = destination.get("kind")
        insertion_value = destination.get("index")
        insertion_index = insertion_value if isinstance(insertion_value, int) else None
        try:
            if moving_bins:
                target_index = insertion_index if insertion_kind == "bin" else None
                if target_index is not None:
                    target_index -= sum(
                        1
                        for bin_id in moving_bins
                        if bins_by_id[bin_id].parent_id == destination_bin_id
                        and bins_by_id[bin_id].order < target_index
                    )
                    target_index = max(0, target_index)
                for offset, bin_id in enumerate(moving_bins):
                    self.project_store.move_bin(
                        bin_id,
                        destination_bin_id,
                        None if target_index is None else target_index + offset,
                    )
            if moving_media:
                target_index = insertion_index if insertion_kind == "media" else None
                if target_index is not None:
                    target_index -= sum(
                        1
                        for media_id in moving_media
                        if media_by_id[media_id].bin_id == destination_bin_id
                        and media_by_id[media_id].order < target_index
                    )
                    target_index = max(0, target_index)
                self.project_store.move_media_many(
                    moving_media,
                    destination_bin_id,
                    target_index,
                )
        except ProjectError as error:
            self._error("Move Media Pool Items", str(error))
            return

        self._active_bin_id = destination_bin_id
        self._refresh_media_tree()
        moved_count = len(moving_bins) + len(moving_media)
        destination_path = self._bin_display_path(destination_bin_id)
        self.statusBar().showMessage(
            f"Moved {moved_count} item{'s' if moved_count != 1 else ''} "
            f"to {destination_path}",
            8000,
        )

    def move_selected_media_tree_items(self, destination_bin_id: str | None) -> None:
        payload = self.media_tree.selected_payload()
        self._move_media_tree_items(
            payload,
            {
                "bin_id": destination_bin_id,
                "kind": None,
                "index": None,
                "label": self._bin_display_path(destination_bin_id),
            },
        )

    def _capture_media_tree_state(
        self,
    ) -> tuple[set[tuple[str, str]], set[tuple[str, str]]]:
        selected: set[tuple[str, str]] = set()
        expanded: set[tuple[str, str]] = set()
        iterator = QTreeWidgetItemIterator(self.media_tree)
        while iterator.value() is not None:
            item = iterator.value()
            kind = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            key = (kind, item_id)
            if item.isSelected():
                selected.add(key)
            if item.isExpanded():
                expanded.add(key)
            iterator += 1
        return selected, expanded

    def _refresh_media_tree(self) -> None:
        if not hasattr(self, "media_tree"):
            return
        selected_keys, expanded_keys = self._capture_media_tree_state()
        if not selected_keys and self._active_bin_id:
            selected_keys.add(("bin", self._active_bin_id))
        had_expanded_state = bool(expanded_keys)
        self.media_tree.blockSignals(True)
        self.media_tree.clear()
        root = QTreeWidgetItem([self.project_store.settings.name])
        root.setData(0, Qt.ItemDataRole.UserRole, "project")
        root.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_DriveHDIcon))
        root.setFlags(
            (root.flags() | Qt.ItemFlag.ItemIsDropEnabled)
            & ~Qt.ItemFlag.ItemIsDragEnabled
        )
        root_font = root.font(0)
        root_font.setBold(True)
        root.setFont(0, root_font)
        root.setBackground(0, QColor("#17181b"))
        self.media_tree.addTopLevelItem(root)

        def add_bin(parent_item: QTreeWidgetItem, parent_id: str | None) -> None:
            for folder in self.project_store.list_bins(parent_id):
                folder_item = QTreeWidgetItem([folder.name])
                folder_item.setData(0, Qt.ItemDataRole.UserRole, "bin")
                folder_item.setData(0, Qt.ItemDataRole.UserRole + 1, folder.id)
                folder_item.setIcon(
                    0,
                    self.style().standardIcon(QStyle.StandardPixmap.SP_DirIcon),
                )
                folder_item.setFlags(
                    folder_item.flags()
                    | Qt.ItemFlag.ItemIsDragEnabled
                    | Qt.ItemFlag.ItemIsDropEnabled
                )
                folder_font = folder_item.font(0)
                folder_font.setBold(True)
                folder_item.setFont(0, folder_font)
                child_count = len(self.project_store.list_bins(folder.id))
                media_count = len(self.project_store.list_media(folder.id))
                folder_path = self._bin_display_path(folder.id)
                folder_item.setToolTip(
                    0,
                    f"{folder_path}\n"
                    f"{child_count} subfolder{'s' if child_count != 1 else ''} · "
                    f"{media_count} clip{'s' if media_count != 1 else ''}",
                )
                parent_item.addChild(folder_item)
                for media in self.project_store.list_media(folder.id):
                    self._append_media_tree_item(folder_item, media)
                add_bin(folder_item, folder.id)

        for media in self.project_store.list_media(None):
            self._append_media_tree_item(root, media)
        add_bin(root, None)
        iterator = QTreeWidgetItemIterator(self.media_tree)
        first_selected: QTreeWidgetItem | None = None
        selected_items: list[QTreeWidgetItem] = []
        while iterator.value() is not None:
            item = iterator.value()
            kind = str(item.data(0, Qt.ItemDataRole.UserRole) or "")
            item_id = str(item.data(0, Qt.ItemDataRole.UserRole + 1) or "")
            key = (kind, item_id)
            if key in selected_keys:
                selected_items.append(item)
                first_selected = first_selected or item
                parent = item.parent()
                while parent is not None:
                    parent.setExpanded(True)
                    parent = parent.parent()
            if key in expanded_keys:
                item.setExpanded(True)
            iterator += 1
        root.setExpanded(True)
        if not had_expanded_state:
            self.media_tree.expandToDepth(1)
        if first_selected is not None:
            self.media_tree.setCurrentItem(first_selected)
            for selected_item in selected_items:
                selected_item.setSelected(True)
            self.media_tree.scrollToItem(first_selected)
        self.media_tree.blockSignals(False)
        self._refresh_timeline_tree()
        self._update_plate_set_context()

    def _append_media_tree_item(self, parent: QTreeWidgetItem, media: MediaRecord) -> None:
        number = plate_number(media.path)
        prefix = f"P{number:02d}  " if number is not None else ""
        cache_suffix = {
            MediaCacheStatus.BUILDING: "  · CACHE",
            MediaCacheStatus.FAILED: "  · CACHE !",
        }.get(media.source_cache_status, "")
        item = QTreeWidgetItem(
            [f"{prefix}{Path(str(media.path)).name}{cache_suffix}"]
        )
        cache_detail = {
            MediaCacheStatus.EMPTY: "Source proxy not queued",
            MediaCacheStatus.PENDING: "Source proxy queued",
            MediaCacheStatus.BUILDING: "Source proxy building in background",
            MediaCacheStatus.READY: f"Source proxy ready: {media.source_cache_path}",
            MediaCacheStatus.FAILED: f"Source proxy failed: {media.source_cache_error or 'unknown error'}",
        }[media.source_cache_status]
        item.setToolTip(0, f"{media.path}\n{cache_detail}")
        item.setData(0, Qt.ItemDataRole.UserRole, "media")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, media.id)
        item.setIcon(0, self.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon))
        item.setFlags(
            item.flags()
            | Qt.ItemFlag.ItemIsDragEnabled
            | Qt.ItemFlag.ItemIsDropEnabled
        )
        parent.addChild(item)

    def _refresh_timeline_tree(self) -> None:
        if not hasattr(self, "timeline_tree"):
            return
        self.timeline_tree.clear()
        for timeline in sorted(self.project_store.timelines, key=lambda item: item.order):
            self._append_timeline_tree_item(timeline)

    def _append_timeline_tree_item(self, timeline: TimelineRecord) -> None:
        numbers = [plate_number(path) for path in timeline.source_paths]
        expected = PLATE_NUMBERS_BY_COUNT.get(len(numbers), ())
        if numbers and tuple(numbers) == expected:
            plate_label = f"P{numbers[0]:02d}–P{numbers[-1]:02d}"
        elif numbers:
            plate_label = f"{len(numbers)}-CAM · MANUAL ORDER"
        else:
            cameras = timeline.config_snapshot.get("cameras")
            count = len(cameras) if isinstance(cameras, list) else 0
            if count in SUPPORTED_CAMERA_COUNTS:
                expected = PLATE_NUMBERS_BY_COUNT[count]
                plate_label = (
                    f"{count}-CAM · P{expected[0]:02d}–P{expected[-1]:02d} · EMPTY"
                )
            else:
                plate_label = "NO PLATES"
        active = timeline.id == self._active_timeline_id
        folder = next(
            (item.name for item in self.project_store.bins if item.id == timeline.bin_id),
            "Master",
        )
        details = f"{plate_label} · {folder}"
        if active:
            details = f"{details} · ACTIVE"
        item = QTreeWidgetItem([f"{timeline.name}\n{details}"])
        item.setSizeHint(0, QSize(0, 40))
        item.setToolTip(0, f"{timeline.name} · {details}")
        if active:
            active_font = item.font(0)
            active_font.setBold(True)
            item.setFont(0, active_font)
            item.setBackground(0, QColor("#28282c"))
            item.setForeground(0, QColor("#f7f8f8"))
        item.setData(0, Qt.ItemDataRole.UserRole, "timeline")
        item.setData(0, Qt.ItemDataRole.UserRole + 1, timeline.id)
        self.timeline_tree.addTopLevelItem(item)
        if active:
            self.timeline_tree.setCurrentItem(item)

    def _media_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        kind = item.data(0, Qt.ItemDataRole.UserRole)
        item_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
        if kind == "bin" and item_id:
            self._active_bin_id = str(item_id)

    def _timeline_item_activated(self, item: QTreeWidgetItem, _column: int) -> None:
        if item.data(0, Qt.ItemDataRole.UserRole) == "timeline":
            item_id = item.data(0, Qt.ItemDataRole.UserRole + 1)
            if item_id:
                self.load_project_timeline(str(item_id))

    def _media_tree_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        item = self.media_tree.itemAt(position)
        if item is not None and not item.isSelected():
            self.media_tree.setCurrentItem(item)
        kind, _item_id = self._selected_media_item()
        menu = QMenu(self)
        menu.addAction("Import Media…", self.choose_videos)
        menu.addAction("New Folder…", self.create_media_bin)
        if kind == "bin":
            menu.addAction("Rename Folder…", self.rename_media_item)
        selected_media = self._selected_media_records()
        if selected_media:
            menu.addSeparator()
            active = self._active_timeline_record()
            if active is not None:
                action = menu.addAction(
                    f'Add {len(selected_media)} Selected to “{active.name}”',
                    lambda checked=False, timeline_id=active.id: self.add_selected_media_to_timeline(timeline_id),
                )
                action.setEnabled(True)
            submenu = menu.addMenu("Add Selected to Timeline")
            for timeline in self.project_store.timelines:
                submenu.addAction(
                    timeline.name,
                    lambda checked=False, timeline_id=timeline.id: self.add_selected_media_to_timeline(timeline_id),
                )
            submenu.setEnabled(bool(self.project_store.timelines))
        movable_payload = self.media_tree.selected_payload()
        if movable_payload:
            menu.addSeparator()
            move_menu = menu.addMenu("Move to Folder")
            move_menu.addAction(
                f"Project Root · {self.project_store.settings.name}",
                lambda checked=False: self.move_selected_media_tree_items(None),
            )

            def add_folder_actions(parent_menu: QMenu, parent_id: str | None) -> None:
                for folder in self.project_store.list_bins(parent_id):
                    children = self.project_store.list_bins(folder.id)
                    if children:
                        folder_menu = parent_menu.addMenu(folder.name)
                        folder_menu.addAction(
                            "Move Here",
                            lambda checked=False, destination=folder.id: (
                                self.move_selected_media_tree_items(destination)
                            ),
                        )
                        folder_menu.addSeparator()
                        add_folder_actions(folder_menu, folder.id)
                    else:
                        parent_menu.addAction(
                            folder.name,
                            lambda checked=False, destination=folder.id: (
                                self.move_selected_media_tree_items(destination)
                            ),
                        )

            move_menu.addSeparator()
            add_folder_actions(move_menu, None)
        if kind in {"bin", "media"} or selected_media:
            menu.addSeparator()
            menu.addAction("Remove from Project…", self.delete_selected_media_items)
        menu.exec(self.media_tree.viewport().mapToGlobal(position))

    def _timeline_tree_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        item = self.timeline_tree.itemAt(position)
        if item is not None:
            self.timeline_tree.setCurrentItem(item)
        kind, _item_id = self._selected_timeline_item()
        menu = QMenu(self)
        menu.addAction("New Timeline…", self.create_timeline)
        if kind == "timeline":
            menu.addAction("Open Timeline", self.open_selected_timeline)
            menu.addAction("Timeline Settings…", self.edit_timeline_settings)
            menu.addAction("Rename Timeline…", self.rename_timeline)
            menu.addAction("Duplicate Timeline", self.duplicate_selected_timeline)
            menu.addSeparator()
            menu.addAction("Delete Timeline…", self.delete_selected_timeline)
            menu.addAction(
                "Add Timeline to Render Queue", self.add_selected_timeline_to_queue
            )
        menu.exec(self.timeline_tree.viewport().mapToGlobal(position))

    def rename_media_item(self) -> None:
        kind, item_id = self._selected_media_item()
        if kind != "bin" or not item_id:
            return
        current = next(
            (
                item.name
                for item in self.project_store.bins
                if item.id == item_id
            ),
            "",
        )
        name, accepted = QInputDialog.getText(
            self, "Rename Folder", "Folder name", text=current
        )
        if not accepted or not name.strip():
            return
        try:
            self.project_store.update_bin(item_id, name=name.strip())
            self._refresh_media_tree()
        except ProjectError as error:
            self._error("Rename", str(error))

    def rename_timeline(self) -> None:
        kind, item_id = self._selected_timeline_item()
        if kind != "timeline" or not item_id:
            item_id = self._active_timeline_id
        timeline = next(
            (item for item in self.project_store.timelines if item.id == item_id),
            None,
        )
        if timeline is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename Timeline", "Timeline name", text=timeline.name
        )
        if not accepted or not name.strip():
            return
        try:
            self.project_store.update_timeline(timeline.id, name=name.strip())
            self._refresh_media_tree()
        except ProjectError as error:
            self._error("Rename Timeline", str(error))

    def rename_focused_item(self) -> None:
        if self.timeline_tree.hasFocus():
            self.rename_timeline()
        else:
            self.rename_media_item()

    def open_selected_timeline(self) -> None:
        kind, item_id = self._selected_timeline_item()
        if kind == "timeline" and item_id:
            self.load_project_timeline(item_id)

    def duplicate_selected_timeline(self) -> None:
        kind, item_id = self._selected_timeline_item()
        if kind != "timeline" or not item_id:
            item_id = self._active_timeline_id
        if not item_id:
            return
        source = next(item for item in self.project_store.timelines if item.id == item_id)
        duplicate = TimelineRecord.create(
            name=f"{source.name} Copy",
            source_paths=source.source_paths,
            config_snapshot=source.config_snapshot,
            inherits_project_settings=source.inherits_project_settings,
            bin_id=source.bin_id,
            tc_alignment_snapshot=source.tc_alignment_snapshot,
            in_frame=source.in_frame,
            out_frame=source.out_frame,
            playback_cache_path=source.playback_cache_path,
            playback_cache_status=source.playback_cache_status,
            stitch_status=source.stitch_status,
            order=source.order + 1,
        )
        try:
            self.project_store.add_timeline(duplicate)
            self._active_timeline_id = duplicate.id
            self._remember_active_timeline()
            self._refresh_media_tree()
        except ProjectError as error:
            self._error("Duplicate Timeline", str(error))

    def delete_selected_timeline(self) -> None:
        kind, item_id = self._selected_timeline_item()
        if kind != "timeline" or not item_id:
            item_id = self._active_timeline_id
        if not item_id:
            return
        timeline = next(
            (item for item in self.project_store.timelines if item.id == item_id),
            None,
        )
        if timeline is None:
            return
        if self._show_message(
            QMessageBox.Icon.Question,
            "Delete Timeline",
            (
                f'Delete timeline “{timeline.name}”? '
                f"Its {len(timeline.source_paths)} source media files will remain on disk."
            ),
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        ) != QMessageBox.StandardButton.Yes:
            return
        self.project_store.remove_timeline(item_id)
        if self._active_timeline_id == item_id:
            self._active_timeline_id = None
            self.settings.remove(self._last_timeline_setting_key())
            self.clear_sources()
        self._refresh_media_tree()

    def _request_camera_assignment(
        self,
        records: list[MediaRecord],
        camera_count: int,
    ) -> list[str] | None:
        paths = [str(item.path) for item in records]
        suggested, manual = suggest_camera_assignment(paths, camera_count)
        if not manual:
            return suggested
        dialog = PlateAssignmentDialog(
            self,
            paths=paths,
            camera_count=camera_count,
        )
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        return dialog.values()

    def add_selected_media_to_timeline(self, timeline_id: str) -> None:
        timeline = next(
            (item for item in self.project_store.timelines if item.id == timeline_id),
            None,
        )
        if timeline is None:
            self._error("Add to Timeline", "Create or select a timeline first")
            return
        records = self._selected_media_records()
        if not records:
            self._error("Add to Timeline", "Select camera clips in the Media Pool first")
            return
        try:
            camera_count = len(records)
            if camera_count not in SUPPORTED_CAMERA_COUNTS:
                raise ValueError("Select exactly 3 or 5 clips")
            ordered = self._request_camera_assignment(records, camera_count)
            if ordered is None:
                return
            detected = [plate_number(path) for path in ordered]
            expected = list(PLATE_NUMBERS_BY_COUNT[camera_count])
            automatic = detected == expected
            was_active = self._active_timeline_id == timeline.id
            self._save_active_timeline()
            self.project_store.update_timeline(
                timeline.id,
                source_paths=tuple(ordered),
                tc_alignment_snapshot=None,
                in_frame=0,
                out_frame=None,
                playback_cache_path=None,
                playback_cache_status=PlaybackCacheStatus.PENDING,
                stitch_status=StitchStatus.UNSTITCHED,
            )
            if was_active:
                self._active_timeline_id = None
            self.load_project_timeline(timeline.id)
            if self._auto_workflows_enabled:
                self._analyze_imported_sources()
            self.statusBar().showMessage(
                (
                    f"Added P{expected[0]:02d}–P{expected[-1]:02d} to {timeline.name}"
                    if automatic
                    else f"Assigned {camera_count} clips to camera slots in {timeline.name}"
                ),
                10000,
            )
        except Exception as error:
            self._error(
                "Add to Timeline",
                "Select the exact 3- or 5-camera count required by the timeline."
                f"\n\n{error}",
            )

    def add_selected_media_to_active_timeline(self) -> None:
        if not self._active_timeline_id:
            self._error("Add to Timeline", "Create or open a timeline first")
            return
        self.add_selected_media_to_timeline(self._active_timeline_id)

    def _unique_timeline_name(self, requested: str) -> str:
        base = requested.strip() or "Timeline"
        project_names = {item.name for item in self.project_store.timelines}
        name = base
        suffix = 2
        while name in project_names:
            name = f"{base} · {suffix}"
            suffix += 1
        return name

    def _save_active_timeline(self, *, strict: bool = False) -> bool:
        if not self._active_timeline_id or self._loading_timeline:
            return True
        active = self._active_timeline_record()
        if active is None:
            return True
        try:
            table_paths = self.source_table.paths()
            sources = self._validate_sources() if any(table_paths) else []
            lower = self.timeline_in.value() if self._tc_alignment else 0
            upper = self.timeline_out.value() if self._tc_alignment else None
            cache_ready = self._playback_path is not None and self._playback_path.is_file()
            config_snapshot = self._collect_config()
            playback_status = (
                PlaybackCacheStatus.READY
                if cache_ready
                else PlaybackCacheStatus.PENDING
                if sources
                else PlaybackCacheStatus.EMPTY
            )
            stitch_status = (
                StitchStatus.READY if self._preview_ready else StitchStatus.UNSTITCHED
            )
            if (
                active.source_paths == tuple(Path(path) for path in sources)
                and active.config_snapshot == config_snapshot
                and active.tc_alignment_snapshot == self._tc_alignment
                and active.in_frame == lower
                and active.out_frame == upper
                and active.playback_cache_path == self._playback_path
                and active.playback_cache_status is playback_status
                and active.stitch_status is stitch_status
            ):
                return True
            self.project_store.update_timeline(
                self._active_timeline_id,
                source_paths=tuple(sources),
                config_snapshot=config_snapshot,
                tc_alignment_snapshot=self._tc_alignment,
                in_frame=lower,
                out_frame=upper,
                playback_cache_path=self._playback_path,
                playback_cache_status=playback_status,
                stitch_status=stitch_status,
            )
            self._refresh_media_tree()
            return True
        except (ProjectError, ValueError, OSError):
            if strict:
                raise
            return False

    def load_project_timeline(self, timeline_id: str) -> None:
        timeline = next(
            (item for item in self.project_store.timelines if item.id == timeline_id),
            None,
        )
        if timeline is None:
            return
        if self.process is not None:
            self.statusBar().showMessage("Finish the current task before changing timeline", 8000)
            return
        self._save_active_timeline()
        self._loading_timeline = True
        try:
            directory = self._working_dir / "timelines" / timeline.id
            directory.mkdir(parents=True, exist_ok=True)
            config_path = directory / "config.json"
            config_path.write_text(
                json.dumps(self._effective_timeline_config(timeline), indent=2),
                encoding="utf-8",
            )
            self.load_config(config_path)
            loaded_cameras = self.config_data.get("cameras")
            if isinstance(loaded_cameras, list):
                self._plate_reset_cameras = json.loads(json.dumps(loaded_cameras))
            if timeline.source_paths:
                self._set_video_sources(
                    [str(path) for path in timeline.source_paths],
                    preserve_order=True,
                )
            else:
                self.clear_sources()
            self._active_timeline_id = timeline.id
            self._active_bin_id = timeline.bin_id
            self._remember_active_timeline()
            if timeline.tc_alignment_snapshot is not None:
                alignment_path = directory / "timecode-alignment.json"
                alignment_path.write_text(
                    json.dumps(timeline.tc_alignment_snapshot, indent=2), encoding="utf-8"
                )
                self._tc_alignment_path = alignment_path
                self._apply_alignment_payload(
                    timeline.tc_alignment_snapshot,
                    timeline.in_frame,
                    timeline.out_frame,
                )
            if timeline.playback_cache_path is not None:
                cache_path = Path(str(timeline.playback_cache_path))
                if cache_path.is_file() and cache_path.stat().st_size > 0:
                    key, _ = self._playback_signature()
                    if cache_path.stem == key:
                        self._load_playback(cache_path, key, autoplay=False)
            self.statusBar().showMessage(f"Opened timeline: {timeline.name}", 8000)
        except Exception as error:
            self._error("Open Timeline", str(error))
        finally:
            self._loading_timeline = False
            self._refresh_media_tree()
        if (
            self._auto_workflows_enabled
            and self._tc_alignment is not None
            and self._playback_path is None
        ):
            self._request_playback_warmup()

    def show_shortcuts(self) -> None:
        self._show_message(
            QMessageBox.Icon.Information,
            "Keyboard Shortcuts",
            "PROJECT / EDIT\n"
            "⌘/Ctrl+S  Save project\n"
            "⌘/Ctrl+Z  Undo\n"
            "⌘/Ctrl+Shift+Z  Redo\n"
            "⌘/Ctrl+C  Copy\n"
            "⌘/Ctrl+V  Paste\n"
            "⌘/Ctrl+A  Select all\n\n"
            "PLAYBACK\n"
            "P  Full screen\nSpace  Play / Pause\n"
            "J  Reverse\nK  Stop\nL  Forward\n← / →  Step one frame",
        )

    def _toggle_inspector(self, checked: bool) -> None:
        self.inspector_panel.setVisible(checked)
        if checked:
            self.right_tabs.setCurrentIndex(0)
            self.jobs_toggle.blockSignals(True)
            self.jobs_toggle.setChecked(False)
            self.jobs_toggle.blockSignals(False)
        self.inspector_toggle.setText("INSPECTOR" if checked else "SHOW INSPECTOR")

    def _toggle_log(self, checked: bool) -> None:
        self.inspector_panel.setVisible(checked)
        if checked:
            self.right_tabs.setCurrentIndex(1)
            self.inspector_toggle.blockSignals(True)
            self.inspector_toggle.setChecked(False)
            self.inspector_toggle.blockSignals(False)
        count = len(self.render_queue.jobs)
        suffix = f" {count}" if count else ""
        self.jobs_toggle.setText(
            f"HIDE JOBS{suffix}" if checked else f"JOBS{suffix}"
        )

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
            # P06–P08 is the three-camera/front configuration.  Use the
            # center three positions from the five-camera 180° rig so the
            # adjacent views retain the calibrated overlap.  Taking the two
            # outer cameras would leave roughly 77° between views and makes
            # the three-camera auto-stitch guard reject otherwise good media.
            center = len(cameras) // 2
            selected = cameras[center - 1 : center + 2]
            for index, camera in enumerate(selected):
                # Keep camera ids stable across 3- and 5-camera timelines so
                # inherited color.match_reference values remain valid.
                camera["name"] = f"cam{index}"
            profile["cameras"] = selected
            self._rig_profiles[3] = json.loads(json.dumps(profile))
            return profile
        raise ValueError(f"Load a calibrated {count}-camera Rig Profile first")

    def _activate_camera_count(self, count: int) -> None:
        current = self.config_data.get("cameras")
        if isinstance(current, list) and len(current) == count:
            cameras = current
        else:
            profile = self._profile_for_count(count)
            cameras = profile.get("cameras")
        if not isinstance(cameras, list) or len(cameras) != count:
            raise ValueError(f"The active Rig Profile does not contain {count} cameras")
        self.config_data["cameras"] = json.loads(json.dumps(cameras))
        self.source_table.blockSignals(True)
        self.source_table.set_rig(self.config_data["cameras"])
        self.source_table.set_camera_numbers(list(PLATE_NUMBERS_BY_COUNT[count]))
        self.source_table.blockSignals(False)
        self.app_subtitle.setText(f"{count}-CAMERA 180° PANORAMA")
        profile_kind = "Auto Profile" if self.config_path and self.config_path.parent == self.project_root / "configs" else "Custom Profile"
        self.profile_label.setText(f"Drive {count}-Cam · {profile_kind}")
        self.setWindowTitle(f"{APP_NAME}  —  {count}-Camera 180°")

    def _set_video_sources(
        self,
        files: list[str],
        *,
        preserve_order: bool = False,
    ) -> None:
        if preserve_order:
            if len(files) not in SUPPORTED_CAMERA_COUNTS:
                raise ValueError("A saved timeline must contain 3 or 5 camera slots")
            ordered = list(files)
            detected = [plate_number(path) for path in ordered]
            expected = list(PLATE_NUMBERS_BY_COUNT[len(ordered)])
            numbers = detected if detected == expected else None
        else:
            ordered, numbers = order_camera_plates(files)
        self._activate_camera_count(len(ordered))
        slot_numbers = list(PLATE_NUMBERS_BY_COUNT[len(ordered)])
        self._plate_numbers = numbers or slot_numbers
        self._source_probes = None
        self._source_fps_error = None
        cameras = self.config_data["cameras"]
        self._source_overrides = {
            path: {
                "input_color_space": camera.get("input_color_space"),
                "input_video_range": camera.get("input_video_range"),
            }
            for path, camera in zip(ordered, cameras, strict=True)
        }
        self.source_table.set_paths(ordered)
        self.source_table.set_camera_numbers(slot_numbers)
        current_output = self.output_path.text().strip()
        if not current_output or current_output == self._last_auto_output:
            self._last_auto_output = self._suggest_output_path(ordered)
            self._set_output_destination(self._last_auto_output)
        self._reset_timing()
        order_note = (
            f"P{numbers[0]:02d} → P{numbers[-1]:02d}"
            if numbers
            else "manual camera-slot order"
        )
        self._append_log(f"Imported {len(ordered)} plates · {order_note}")
        self.source_table.setCurrentCell(0, 0)
        self._selected_camera_row = 0
        self._load_plate_controls(0)

    def _suggest_output_path(self, sources: list[str]) -> str:
        stem = re.sub(
            r"(?i)^P0?[1-8][._ -]*",
            "",
            Path(sources[0]).stem,
        ).strip(" ._-")
        if not stem:
            stem = Path(sources[0]).parent.name or "timeline"
        safe = re.sub(r"[^A-Za-z0-9가-힣._-]+", "_", stem).strip("._-")
        codec = str(self.output_codec.currentData())
        return str(resolved_render_output(self._output_root, f"{safe}_stitched", codec))

    def _output_codec_changed(self) -> None:
        self._update_output_hint()
        self._output_fields_changed()

    def _output_fields_changed(self) -> None:
        if self._updating_output_destination:
            return
        try:
            path = resolved_render_output(
                self.output_directory.text(),
                self.output_name.text(),
                str(self.output_codec.currentData()),
            )
        except ValueError as error:
            self.output_path.clear()
            self.output_path_preview.setText(str(error))
            return
        self.output_path.setText(str(path))
        self.output_path_preview.setText(str(path))

    def _set_output_destination(self, path: str | Path) -> None:
        codec = str(self.output_codec.currentData())
        folder, name = split_render_output(path, codec)
        self._updating_output_destination = True
        self.output_directory.setText(str(folder))
        self.output_name.setText(name)
        self.output_path.setText(str(path))
        self.output_path_preview.setText(str(path))
        self._updating_output_destination = False

    @staticmethod
    def _output_collision_key(path: str | Path) -> str:
        expanded = Path(path).expanduser().resolve(strict=False)
        return os.path.normcase(os.path.normpath(str(expanded)))

    @staticmethod
    def _prepare_output_destination(output: Path, codec: str) -> None:
        if codec not in OUTPUT_SUFFIX_BY_CODEC:
            raise ValueError(f"Unsupported output codec: {codec}")
        output.parent.mkdir(parents=True, exist_ok=True)
        if not output.parent.is_dir():
            raise ValueError(f"Output folder is not a directory: {output.parent}")
        if codec.endswith("sequence"):
            if output.exists() and not output.is_dir():
                raise ValueError(f"Sequence output must be a folder: {output}")
            if output.is_dir() and any(output.iterdir()):
                raise ValueError(f"Sequence output folder is not empty: {output}")
            return
        if output.exists():
            raise ValueError(f"Output already exists: {output}")

    @staticmethod
    def _render_staging_path(output: Path, codec: str, token: str) -> Path:
        safe_token = re.sub(r"[^A-Za-z0-9_-]+", "-", token)[:32]
        if codec.endswith("sequence"):
            return output.with_name(f".{output.name}.vpstitch-part-{safe_token}")
        return output.with_name(
            f".{output.stem}.vpstitch-part-{safe_token}{output.suffix}"
        )

    @staticmethod
    def _discard_render_staging(path: Path) -> None:
        try:
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
        except OSError:
            pass

    @staticmethod
    def _commit_render_staging(staging: Path, output: Path) -> None:
        if not staging.exists():
            raise ValueError(f"Render finished without creating output: {staging}")
        if staging.is_dir():
            if not any(staging.iterdir()):
                raise ValueError("Render finished with an empty sequence folder")
        elif staging.stat().st_size < 1:
            raise ValueError("Render finished with an empty output file")
        if output.exists():
            raise ValueError(f"Output appeared while rendering: {output}")
        os.replace(staging, output)
        try:
            directory_fd = os.open(output.parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        except OSError:
            pass

    def _request_render_destination(
        self,
        *,
        title: str,
        action_label: str,
    ) -> str | None:
        if not self._auto_workflows_enabled:
            self._output_fields_changed()
            return self.output_path.text().strip() or None
        dialog = QDialog(self)
        dialog.setWindowTitle(title)
        dialog.setMinimumSize(700, 340)
        layout = QVBoxLayout(dialog)
        heading = QLabel("RENDER DESTINATION")
        heading.setProperty("sectionTitle", True)
        layout.addWidget(heading)
        note = QLabel(
            "This exact folder and name are stored with the queue item. Render All will not reuse another timeline's destination."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)
        form = QFormLayout()
        folder = QLineEdit(self.output_directory.text())
        folder.setMinimumWidth(380)
        browse = QPushButton("…")
        browse.setObjectName("iconButton")
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(folder)
        folder_layout.addWidget(browse)
        name = QLineEdit(self.output_name.text())
        name.setMinimumWidth(380)
        codec = ChevronComboBox()
        codec.setObjectName("renderCodec")
        for label, value in GUI_MASTER_CODEC_OPTIONS:
            codec.addItem(label, value)
        current_codec = codec.findData(self.output_codec.currentData())
        codec.setCurrentIndex(max(0, current_codec))
        extension = QLabel()
        resolved = QLineEdit()
        resolved.setMinimumWidth(380)
        resolved.setReadOnly(True)
        resolved.setProperty("muted", True)
        form.addRow("Output folder", folder_row)
        form.addRow("File name", name)
        form.addRow("Format", codec)
        form.addRow("Format suffix", extension)
        form.addRow("Resolved path", resolved)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save
            | QDialogButtonBox.StandardButton.Cancel
        )
        accept = buttons.button(QDialogButtonBox.StandardButton.Save)
        accept.setText(action_label)
        accept.setObjectName("primaryButton")
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def refresh() -> None:
            selected_codec = str(codec.currentData())
            extension.setText(
                OUTPUT_SUFFIX_BY_CODEC.get(selected_codec, "") or "Sequence folder"
            )
            try:
                path = resolved_render_output(
                    folder.text(), name.text(), selected_codec
                )
            except ValueError as error:
                resolved.setText(str(error))
                accept.setEnabled(False)
                return
            resolved.setText(str(path))
            resolved.setToolTip(str(path))
            accept.setEnabled(True)

        def browse_folder() -> None:
            selected = QFileDialog.getExistingDirectory(
                dialog,
                "Select render folder",
                folder.text()
                or _preferred_storage_directory(
                    self.settings,
                    "storage/lastRenderDir",
                    self._output_root,
                ),
            )
            if selected:
                folder.setText(selected)
                self.settings.setValue("storage/lastRenderDir", selected)
                _remember_storage_root(self.settings, selected)
                self.settings.sync()

        browse.clicked.connect(browse_folder)
        folder.textChanged.connect(refresh)
        name.textChanged.connect(refresh)
        codec.currentIndexChanged.connect(refresh)
        refresh()
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return None
        selected_codec = str(codec.currentData())
        main_codec_index = self.output_codec.findData(selected_codec)
        if main_codec_index < 0:
            raise ValueError(f"Unsupported output codec: {selected_codec}")
        self.output_codec.setCurrentIndex(main_codec_index)
        path = resolved_render_output(folder.text(), name.text(), selected_codec)
        self._set_output_destination(path)
        self._last_auto_output = None
        return str(path)

    def _update_source_status(self) -> None:
        loaded = sum(bool(path) for path in self.source_table.paths())
        expected = self.source_table.camera_count()
        ready = loaded == expected and expected in SUPPORTED_CAMERA_COUNTS
        for name in ("tc_align_button", "preview_button", "add_queue_button", "render_button"):
            button = getattr(self, name, None)
            if button is not None and self.process is None:
                button.setEnabled(ready and self._active_timeline_id is not None)
        if hasattr(self, "assign_media_button") and self.process is None:
            selected_count = len(self._selected_media_records())
            self.assign_media_button.setEnabled(
                self._active_timeline_id is not None
                and selected_count in SUPPORTED_CAMERA_COUNTS
            )
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
            self.source_status.setText(
                "No plates assigned · select Media Pool clips and add them to this timeline"
                if self._active_timeline_id else
                "Import media, create a timeline, then assign P01–P05 or P06–P08"
            )

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
            "Auto profile: P01–P05 rear rig or P06–P08 front rig."
        )
        profile_note.setWordWrap(True)
        profile_note.setProperty("muted", True)
        profile_layout.addWidget(profile_note)
        align_note = QLabel(
            "Stitch solves one reference frame and keeps that geometry for the timeline."
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
        self.canvas_width.setToolTip(
            "Manual master canvas width; Preview uses the same ratio"
        )
        self.canvas_height = QSpinBox()
        self.canvas_height.setRange(32, MAX_CANVAS_HEIGHT)
        self.canvas_height.setSingleStep(128)
        self.canvas_height.setToolTip(
            "Manual master canvas height; Preview uses the same ratio"
        )
        self.h_fov = QDoubleSpinBox()
        self.h_fov.setRange(1.0, 360.0)
        self.h_fov.setSuffix("°")
        self.v_fov = QDoubleSpinBox()
        self.v_fov.setRange(1.0, 179.0)
        self.v_fov.setSuffix("°")
        self.h_fov.setToolTip("Manual horizontal field of view for Preview and Render")
        self.v_fov.setToolTip("Manual vertical field of view for Preview and Render")
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
        self.canvas_ratio = QLabel()
        self.canvas_ratio.setProperty("muted", True)
        form.addRow("Ratio", self.canvas_ratio)
        for widget in (
            self.canvas_width,
            self.canvas_height,
            self.h_fov,
            self.v_fov,
            self.center_yaw,
            self.center_pitch,
            self.seam_feather,
        ):
            widget.valueChanged.connect(self._canvas_controls_changed)
        layout.addWidget(canvas)

        flow = QGroupBox("PARALLAX REFINEMENT")
        flow_form = QFormLayout(flow)
        flow_form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        flow_form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        flow_form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        flow_form.setHorizontalSpacing(8)
        flow_form.setVerticalSpacing(5)
        self.flow_enabled = QCheckBox("Enable DIS optical flow")
        self.flow_preset = ChevronComboBox()
        self.flow_preset.addItems(["ultrafast", "fast", "medium"])
        self.flow_max = QDoubleSpinBox()
        self.flow_max.setRange(1.0, 256.0)
        self.flow_max.setSuffix(" px")
        self.flow_enabled.toggled.connect(self._stitch_setting_changed)
        self.flow_preset.currentTextChanged.connect(self._stitch_setting_changed)
        self.flow_max.valueChanged.connect(self._stitch_setting_changed)
        flow_form.addRow(self.flow_enabled)
        flow_form.addRow("Quality", self.flow_preset)
        flow_form.addRow("Max displacement", self.flow_max)
        layout.addWidget(flow)

        fit_full = QPushButton("FIT FULL PLATES")
        fit_full.setObjectName("primaryButton")
        fit_full.setToolTip(
            "Fit the complete warped plate boundaries with a 3% safety margin"
        )
        fit_full.clicked.connect(self.fit_full_plates)
        layout.addWidget(fit_full)
        analyze = QPushButton("COVERAGE MASK")
        analyze.setObjectName("secondaryButton")
        analyze.setToolTip(
            "Analyze which parts of the current manual canvas contain image data"
        )
        analyze.clicked.connect(self.analyze_coverage)
        layout.addWidget(analyze)
        layout.addStretch()
        return panel

    def _plate_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(4, 4, 4, 8)
        layout.setSpacing(7)

        self.plate_inspector_title = QLabel("SELECT A TIMELINE PLATE")
        self.plate_inspector_title.setProperty("inspectorTitle", True)
        layout.addWidget(self.plate_inspector_title)
        note = QLabel(
            "Select a timeline plate to tune it. Changes apply to viewer and render."
        )
        note.setWordWrap(True)
        note.setProperty("muted", True)
        layout.addWidget(note)

        transform = QGroupBox("TRANSFORM")
        transform_form = QFormLayout(transform)
        transform_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.plate_position_x = ScrubbableDoubleSpinBox()
        self.plate_position_x.setRange(-180.0, 180.0)
        self.plate_position_x.setDecimals(3)
        self.plate_position_x.setSingleStep(0.05)
        self.plate_position_x.setSuffix("°")
        self.plate_position_x.setToolTip("Horizontal placement; adjusts camera yaw")
        self.plate_position_y = ScrubbableDoubleSpinBox()
        self.plate_position_y.setRange(-89.0, 89.0)
        self.plate_position_y.setDecimals(3)
        self.plate_position_y.setSingleStep(0.05)
        self.plate_position_y.setSuffix("°")
        self.plate_position_y.setToolTip("Vertical placement; adjusts camera pitch")
        self.plate_rotation = ScrubbableDoubleSpinBox()
        self.plate_rotation.setRange(-45.0, 45.0)
        self.plate_rotation.setDecimals(3)
        self.plate_rotation.setSingleStep(0.05)
        self.plate_rotation.setSuffix("°")
        self.plate_scale = ScrubbableDoubleSpinBox()
        self.plate_scale.setRange(10.0, 400.0)
        self.plate_scale.setDecimals(2)
        self.plate_scale.setSingleStep(0.25)
        self.plate_scale.setSuffix("%")
        for label, widget in (
            ("Position X", self.plate_position_x),
            ("Position Y", self.plate_position_y),
            ("Rotation", self.plate_rotation),
            ("Scale", self.plate_scale),
        ):
            transform_form.addRow(label, widget)
        layout.addWidget(transform)

        crop = QGroupBox("SOURCE CROP")
        crop_form = QFormLayout(crop)
        crop_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.plate_crop_left = ScrubbableDoubleSpinBox()
        self.plate_crop_right = ScrubbableDoubleSpinBox()
        self.plate_crop_top = ScrubbableDoubleSpinBox()
        self.plate_crop_bottom = ScrubbableDoubleSpinBox()
        for label, widget in (
            ("Left", self.plate_crop_left),
            ("Right", self.plate_crop_right),
            ("Top", self.plate_crop_top),
            ("Bottom", self.plate_crop_bottom),
        ):
            widget.setRange(0.0, 49.0)
            widget.setDecimals(2)
            widget.setSingleStep(0.25)
            widget.setSuffix("%")
            crop_form.addRow(label, widget)
        layout.addWidget(crop)

        warp = QGroupBox("LENS WARP")
        warp_form = QFormLayout(warp)
        warp_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.plate_warp_controls = tuple(
            ScrubbableDoubleSpinBox() for _ in range(4)
        )
        for index, widget in enumerate(self.plate_warp_controls, start=1):
            widget.setRange(-2.0, 2.0)
            widget.setDecimals(5)
            widget.setSingleStep(0.0005)
            widget.setToolTip(
                "Lens distortion coefficient used by Preview, proxy and final render"
            )
            warp_form.addRow(f"Warp {index}", widget)
        layout.addWidget(warp)

        blend = QGroupBox("EDGE BLEND")
        blend_form = QFormLayout(blend)
        blend_form.setFieldGrowthPolicy(
            QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow
        )
        self.plate_feather_left = ScrubbableDoubleSpinBox()
        self.plate_feather_right = ScrubbableDoubleSpinBox()
        for label, widget in (
            ("Left feather", self.plate_feather_left),
            ("Right feather", self.plate_feather_right),
        ):
            widget.setRange(0.05, 30.0)
            widget.setDecimals(2)
            widget.setSingleStep(0.1)
            widget.setSuffix("°")
            blend_form.addRow(label, widget)
        layout.addWidget(blend)

        for widget in self._plate_control_widgets():
            widget.setToolTip(
                (widget.toolTip() + "\n" if widget.toolTip() else "")
                + "Drag up/down to scrub · hold Shift for 10× finer control"
            )
            widget.valueChanged.connect(self._plate_control_changed)

        actions = QHBoxLayout()
        reset = QPushButton("RESET PLATE")
        reset.setObjectName("secondaryButton")
        reset.clicked.connect(self._reset_selected_plate)
        refresh = QPushButton("REFRESH FRAME")
        refresh.setObjectName("primaryButton")
        refresh.setToolTip(
            "Live Preview updates automatically; use this only to retry the current frame"
        )
        refresh.clicked.connect(self._refresh_plate_preview)
        actions.addWidget(reset)
        actions.addWidget(refresh, 1)
        layout.addLayout(actions)
        layout.addStretch()
        self._set_plate_controls_enabled(False)
        return panel

    def _plate_control_widgets(self) -> tuple[QDoubleSpinBox, ...]:
        return (
            self.plate_position_x,
            self.plate_position_y,
            self.plate_rotation,
            self.plate_scale,
            self.plate_crop_left,
            self.plate_crop_right,
            self.plate_crop_top,
            self.plate_crop_bottom,
            *self.plate_warp_controls,
            self.plate_feather_left,
            self.plate_feather_right,
        )

    def _set_plate_controls_enabled(self, enabled: bool) -> None:
        for widget in self._plate_control_widgets():
            widget.setEnabled(enabled)

    def _source_selection_changed(self) -> None:
        row = self.source_table.currentRow()
        if row < 0 or row >= self.source_table.camera_count():
            self._set_plate_controls_enabled(False)
            self.plate_inspector_title.setText("SELECT A TIMELINE PLATE")
            if self._plate_move_mode:
                self._set_plate_move_mode(False)
            return
        self._selected_camera_row = row
        self._load_plate_controls(row)
        if self._plate_move_mode:
            self.preview.set_move_overlay(True, self._plate_move_label(row))
        if hasattr(self, "_plate_settings_index"):
            self.settings_tabs.setCurrentIndex(self._plate_settings_index)

    def _plate_move_label(self, row: int | None = None) -> str:
        selected = self._selected_camera_row if row is None else row
        number = (
            self._plate_numbers[selected]
            if self._plate_numbers and 0 <= selected < len(self._plate_numbers)
            else selected + 1
        )
        return f"MOVE  P{number:02d}"

    def _set_plate_move_mode(self, enabled: bool) -> None:
        if enabled:
            row = self.source_table.currentRow()
            if row < 0:
                row = self._selected_camera_row
            paths = self.source_table.paths()
            if not 0 <= row < len(paths) or not paths[row]:
                self.statusBar().showMessage(
                    "Select an assigned plate in the active timeline first",
                    5000,
                )
                return
            self._selected_camera_row = row
            self._load_plate_controls(row)
            preserved_frame = self.timeline_playhead.value()
            self._stop_playback(preserve_image=True)
            self._set_playhead(preserved_frame)
            if (
                self._latest_playback_frame is not None
                and not self._latest_playback_frame.isNull()
            ):
                self.preview.set_image(self._latest_playback_frame)
            self.preview_stack.setCurrentWidget(self.preview)
            self._plate_move_mode = True
            self.preview.set_move_overlay(True, self._plate_move_label(row))
            if hasattr(self, "plate_move_action"):
                self.plate_move_action.blockSignals(True)
                self.plate_move_action.setChecked(True)
                self.plate_move_action.blockSignals(False)
            self.shortcut_hint.setText(
                "MOVE MODE  ·  Arrow keys move plate  ·  Shift+Arrow fine  ·  M Exit"
            )
            self.statusBar().showMessage(
                f"{self._plate_move_label(row)} · arrows 0.05° · Shift 0.005°",
                6000,
            )
            self.preview.setFocus()
            return
        was_enabled = self._plate_move_mode
        self._plate_move_mode = False
        self.preview.set_move_overlay(False)
        if hasattr(self, "plate_move_action"):
            self.plate_move_action.blockSignals(True)
            self.plate_move_action.setChecked(False)
            self.plate_move_action.blockSignals(False)
        self.shortcut_hint.setText(
            "Space Play/Pause  ·  J/K/L Transport  ·  M Move Plate  ·  P Full Screen"
        )
        self.statusBar().showMessage("Plate move mode off", 3000)
        if was_enabled:
            # Confirm the exact high-resolution viewer result after the fast
            # movement renderer has kept up with the operator's nudges.
            self._schedule_live_preview("Plate move confirmed", immediate=True)

    def _toggle_plate_move_mode(self) -> None:
        self._set_plate_move_mode(not self._plate_move_mode)

    def _capture_playback_frame(self, frame) -> None:  # type: ignore[no-untyped-def]
        image = frame.toImage()
        if not image.isNull():
            self._latest_playback_frame = image.copy()

    def _nudge_selected_plate(
        self,
        horizontal: int,
        vertical: int,
        *,
        fine: bool = False,
    ) -> None:
        if not self._plate_move_mode:
            return
        step = 0.005 if fine else 0.05
        self._loading_plate_controls = True
        try:
            if horizontal:
                self.plate_position_x.setValue(
                    self.plate_position_x.value() + horizontal * step
                )
            if vertical:
                self.plate_position_y.setValue(
                    self.plate_position_y.value() + vertical * step
                )
        finally:
            self._loading_plate_controls = False
        self._plate_control_changed()
        self.statusBar().showMessage(
            f"{self._plate_move_label()}  ·  X {self.plate_position_x.value():.3f}°"
            f"  Y {self.plate_position_y.value():.3f}°",
            2500,
        )

    def _load_plate_controls(self, row: int) -> None:
        cameras = self.config_data.get("cameras")
        if not isinstance(cameras, list) or not 0 <= row < len(cameras):
            self._set_plate_controls_enabled(False)
            return
        camera = cameras[row]
        if not isinstance(camera, dict):
            self._set_plate_controls_enabled(False)
            return
        paths = self.source_table.paths()
        number = (
            self._plate_numbers[row]
            if self._plate_numbers and row < len(self._plate_numbers)
            else row + 1
        )
        clip = Path(paths[row]).name if row < len(paths) and paths[row] else "UNASSIGNED"
        self.plate_inspector_title.setText(f"P{number:02d}  ·  {clip}")
        self._loading_plate_controls = True
        self.plate_position_x.setValue(float(camera.get("yaw_deg", 0.0)))
        self.plate_position_y.setValue(float(camera.get("pitch_deg", 0.0)))
        self.plate_rotation.setValue(float(camera.get("roll_deg", 0.0)))
        self.plate_scale.setValue(float(camera.get("scale", 1.0)) * 100.0)
        self.plate_crop_left.setValue(float(camera.get("crop_left", 0.0)) * 100.0)
        self.plate_crop_right.setValue(float(camera.get("crop_right", 0.0)) * 100.0)
        self.plate_crop_top.setValue(float(camera.get("crop_top", 0.0)) * 100.0)
        self.plate_crop_bottom.setValue(float(camera.get("crop_bottom", 0.0)) * 100.0)
        lens = camera.get("lens")
        distortion = (
            list(lens.get("distortion", [0.0, 0.0, 0.0, 0.0]))
            if isinstance(lens, dict)
            else [0.0, 0.0, 0.0, 0.0]
        )
        distortion = (distortion + [0.0] * 4)[:4]
        for widget, value in zip(self.plate_warp_controls, distortion, strict=True):
            widget.setValue(float(value))
        global_feather = self.seam_feather.value() if hasattr(self, "seam_feather") else 4.0
        self.plate_feather_left.setValue(
            float(camera.get("feather_left_deg", global_feather))
        )
        self.plate_feather_right.setValue(
            float(camera.get("feather_right_deg", global_feather))
        )
        self._loading_plate_controls = False
        self._set_plate_controls_enabled(bool(paths[row] if row < len(paths) else ""))

    def _plate_control_changed(self) -> None:
        if self._loading_plate_controls or self._loading_config:
            return
        cameras = self.config_data.get("cameras")
        row = self._selected_camera_row
        if not isinstance(cameras, list) or not 0 <= row < len(cameras):
            return
        camera = cameras[row]
        if not isinstance(camera, dict):
            return
        horizontal_crop = (
            self.plate_crop_left.value() + self.plate_crop_right.value()
        ) / 100.0
        vertical_crop = (
            self.plate_crop_top.value() + self.plate_crop_bottom.value()
        ) / 100.0
        if horizontal_crop >= 0.99 or vertical_crop >= 0.99:
            self.statusBar().showMessage("Crop must leave at least 1% of the plate", 5000)
            return
        camera.update(
            {
                "yaw_deg": self.plate_position_x.value(),
                "pitch_deg": self.plate_position_y.value(),
                "roll_deg": self.plate_rotation.value(),
                "scale": self.plate_scale.value() / 100.0,
                "crop_left": self.plate_crop_left.value() / 100.0,
                "crop_right": self.plate_crop_right.value() / 100.0,
                "crop_top": self.plate_crop_top.value() / 100.0,
                "crop_bottom": self.plate_crop_bottom.value() / 100.0,
                "feather_left_deg": self.plate_feather_left.value(),
                "feather_right_deg": self.plate_feather_right.value(),
            }
        )
        lens = camera.setdefault("lens", {})
        if isinstance(lens, dict):
            lens["distortion"] = [
                widget.value() for widget in self.plate_warp_controls
            ]
        update_fine_tune_metadata(camera)
        self.source_table.blockSignals(True)
        self.source_table.set_orientation(
            row,
            camera["yaw_deg"],
            camera["pitch_deg"],
            camera["roll_deg"],
        )
        self.source_table.blockSignals(False)
        self._invalidate_plate_preview()

    def _invalidate_plate_preview(self) -> None:
        self._schedule_live_preview(
            "Plate fine-tune applied",
            immediate=self._plate_move_mode,
        )

    def _reset_selected_plate(self) -> None:
        cameras = self.config_data.get("cameras")
        row = self._selected_camera_row
        profile_cameras = self._plate_reset_cameras
        if (
            not isinstance(cameras, list)
            or not isinstance(profile_cameras, list)
            or not 0 <= row < min(len(cameras), len(profile_cameras))
        ):
            return
        base = json.loads(json.dumps(profile_cameras[row]))
        for key in (
            "yaw_deg", "pitch_deg", "roll_deg", "scale", "crop_left",
            "crop_right", "crop_top", "crop_bottom", "feather_left_deg",
            "feather_right_deg",
        ):
            if key in base:
                cameras[row][key] = base[key]
            else:
                cameras[row].pop(key, None)
        if isinstance(base.get("lens"), dict):
            cameras[row]["lens"] = json.loads(json.dumps(base["lens"]))
        self._load_plate_controls(row)
        self.source_table.blockSignals(True)
        self.source_table.set_orientation(
            row,
            cameras[row].get("yaw_deg", 0.0),
            cameras[row].get("pitch_deg", 0.0),
            cameras[row].get("roll_deg", 0.0),
        )
        self.source_table.blockSignals(False)
        self._invalidate_plate_preview()

    def _refresh_plate_preview(self) -> None:
        if self._last_reference_dir is None:
            self.create_preview()
            return
        self._schedule_live_preview("Plate fine-tune applied", immediate=True)

    def _color_settings(self) -> QWidget:
        panel = QWidget()
        layout = QVBoxLayout(panel)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(8)

        pipeline = QGroupBox("PIPELINE")
        form = QFormLayout(pipeline)
        form.setRowWrapPolicy(QFormLayout.RowWrapPolicy.DontWrapRows)
        form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        form.setLabelAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        form.setHorizontalSpacing(8)
        form.setVerticalSpacing(5)
        self.color_mode = ChevronComboBox()
        self.color_mode.addItem("Passthrough", "passthrough")
        self.color_mode.addItem("OCIO managed", "ocio")
        self.color_mode.currentIndexChanged.connect(self._update_color_controls)
        self.color_mode.currentIndexChanged.connect(self._color_basis_changed)
        self.ocio_config = QLineEdit()
        self.ocio_config.setToolTip("Bundled OCIO identifier or a custom .ocio file")
        self.ocio_config.editingFinished.connect(self._reload_ocio_spaces)
        ocio_row = QWidget()
        ocio_layout = QHBoxLayout(ocio_row)
        ocio_layout.setContentsMargins(0, 0, 0, 0)
        ocio_layout.addWidget(self.ocio_config)
        ocio_button = QPushButton("OPEN")
        ocio_button.setObjectName("secondaryButton")
        ocio_button.setFixedWidth(48)
        ocio_button.clicked.connect(self.choose_ocio)
        ocio_layout.addWidget(ocio_button)
        self.ocio_reload_button = QPushButton("LOAD SPACES")
        self.ocio_reload_button.setObjectName("secondaryButton")
        self.ocio_reload_button.clicked.connect(self._reload_ocio_spaces)
        self.input_space = _new_ocio_space_combo("Camera Rec.709")
        self.working_space = _new_ocio_space_combo("ACEScg")
        self.input_space.currentTextChanged.connect(self._color_basis_changed)
        self.working_space.currentTextChanged.connect(self._color_basis_changed)
        self.output_mode = ChevronComboBox()
        self.output_mode.addItem("Color space / Log", "colorspace")
        self.output_mode.addItem("Display transform", "display_view")
        self.output_mode.currentIndexChanged.connect(self._update_delivery_controls)
        self.output_mode.currentIndexChanged.connect(self._color_pipeline_setting_changed)
        self.output_space = _new_ocio_space_combo(
            "Gamma 2.4 Encoded Rec.709", output=True
        )
        self.output_space.currentTextChanged.connect(self._color_pipeline_setting_changed)
        self.output_display = ChevronComboBox()
        self.output_display.currentIndexChanged.connect(self._delivery_display_changed)
        self.output_display.currentIndexChanged.connect(
            self._color_pipeline_setting_changed
        )
        self.output_view = ChevronComboBox()
        self.output_view.currentIndexChanged.connect(self._color_pipeline_setting_changed)
        self.output_space_label = QLabel("Output color space")
        self.output_display_label = QLabel("Delivery display")
        self.output_view_label = QLabel("Delivery view")
        self.viewer_monitor = ChevronComboBox()
        for key, (label, _display, _view) in VIEWER_MONITOR_TRANSFORMS.items():
            self.viewer_monitor.addItem(label, key)
        self.viewer_monitor.addItem("Match delivery target", "delivery")
        saved_viewer = str(self.settings.value("viewer/monitor", "sdr-rec709"))
        self.viewer_monitor.setCurrentIndex(
            max(0, self.viewer_monitor.findData(saved_viewer))
        )
        self.viewer_monitor.currentIndexChanged.connect(
            self._viewer_monitor_changed
        )
        self.viewer_status = QLabel(
            "Managed Rec.709 viewer · delivery and Render Queue stay unchanged."
        )
        self.viewer_status.setProperty("muted", True)
        self.viewer_status.setWordWrap(True)
        self.integer_dither = QCheckBox("TPDF dither for integer masters")
        self.delivery_status = QLabel("Scene/log or managed display output.")
        self.delivery_status.setProperty("muted", True)
        self.delivery_status.setWordWrap(True)
        self.ocio_space_status = QLabel("Choose an OCIO config, then load its spaces")
        self.ocio_space_status.setProperty("muted", True)
        self.ocio_space_status.setWordWrap(True)
        form.addRow("Mode", self.color_mode)
        form.addRow("OCIO config", ocio_row)
        form.addRow(self.ocio_reload_button)
        form.addRow("Input transform", self.input_space)
        form.addRow("Working space", self.working_space)
        pipeline_separator = QFrame()
        pipeline_separator.setObjectName("formSeparator")
        pipeline_separator.setFrameShape(QFrame.Shape.HLine)
        form.addRow(pipeline_separator)
        form.addRow("Delivery method", self.output_mode)
        form.addRow(self.output_space_label, self.output_space)
        form.addRow(self.output_display_label, self.output_display)
        form.addRow(self.output_view_label, self.output_view)
        form.addRow(self.integer_dither)
        form.addRow(self.delivery_status)
        viewer_separator = QFrame()
        viewer_separator.setObjectName("formSeparator")
        viewer_separator.setFrameShape(QFrame.Shape.HLine)
        form.addRow(viewer_separator)
        form.addRow("Viewer monitor", self.viewer_monitor)
        form.addRow(self.viewer_status)
        form.addRow(self.ocio_space_status)
        layout.addWidget(pipeline)

        match = QGroupBox("CAMERA MATCH")
        match_form = QFormLayout(match)
        match_form.setHorizontalSpacing(8)
        match_form.setVerticalSpacing(5)
        self.color_match_enabled = QCheckBox("Apply match")
        self.color_match_enabled.toggled.connect(self._color_match_setting_changed)
        self.color_match_reference = ChevronComboBox()
        self.color_match_reference.currentIndexChanged.connect(
            self._color_match_reference_changed
        )
        self.color_match_strength = QSpinBox()
        self.color_match_strength.setRange(0, 100)
        self.color_match_strength.setSuffix("%")
        self.color_match_strength.setValue(100)
        self.color_match_strength.setToolTip(
            "Blend the saved white-point correction without changing exposure"
        )
        self.color_match_strength.valueChanged.connect(
            self._color_match_setting_changed
        )
        match_actions = QWidget()
        match_actions_layout = QHBoxLayout(match_actions)
        match_actions_layout.setContentsMargins(0, 0, 0, 0)
        match_actions_layout.setSpacing(5)
        self.color_match_button = QPushButton("MATCH")
        self.color_match_button.setObjectName("primaryButton")
        self.color_match_button.setToolTip(
            "Match camera white points from the current Quick Preview overlaps"
        )
        self.color_match_button.clicked.connect(self.match_cameras)
        self.color_match_reset_button = QPushButton("RESET")
        self.color_match_reset_button.setObjectName("secondaryButton")
        self.color_match_reset_button.clicked.connect(self.reset_color_match)
        match_actions_layout.addWidget(self.color_match_button, 1)
        match_actions_layout.addWidget(self.color_match_reset_button)
        self.color_match_status = QLabel("Create a Quick Preview, then match")
        self.color_match_status.setProperty("muted", True)
        self.color_match_status.setWordWrap(True)
        match_form.addRow(self.color_match_enabled)
        match_form.addRow("Reference", self.color_match_reference)
        match_form.addRow("Strength", self.color_match_strength)
        match_form.addRow(match_actions)
        match_form.addRow(self.color_match_status)
        layout.addWidget(match)

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
        self.output_codec = ChevronComboBox()
        for label, value in GUI_MASTER_CODEC_OPTIONS:
            self.output_codec.addItem(label, value)
        self.output_codec.currentIndexChanged.connect(self._output_codec_changed)
        self.output_path = QLineEdit()
        self.output_path.hide()
        self.output_directory = QLineEdit(str(self._output_root))
        folder_row = QWidget()
        folder_layout = QHBoxLayout(folder_row)
        folder_layout.setContentsMargins(0, 0, 0, 0)
        folder_layout.addWidget(self.output_directory)
        path_button = QPushButton("…")
        path_button.setObjectName("iconButton")
        path_button.setFixedWidth(34)
        path_button.clicked.connect(self.choose_output)
        folder_layout.addWidget(path_button)
        self.output_name = QLineEdit("stitched")
        self.output_name.setPlaceholderText("Timeline or take name")
        self.output_directory.textChanged.connect(self._output_fields_changed)
        self.output_name.textChanged.connect(self._output_fields_changed)
        self.output_path_preview = QLabel()
        self.output_path_preview.setWordWrap(True)
        self.output_path_preview.setProperty("muted", True)
        self.fps_mode = ChevronComboBox()
        self.fps_mode.setObjectName("fpsMode")
        self.fps_mode.addItem("MATCH PLATE", FPS_MODE_MATCH_SOURCE)
        self.fps_mode.addItem("CUSTOM CONFORM", FPS_MODE_CUSTOM)
        self.fps = QDoubleSpinBox()
        self.fps.setRange(1.0, 240.0)
        self.fps.setDecimals(6)
        self.fps.setSingleStep(0.001)
        self.fps_mode.currentIndexChanged.connect(
            lambda _index: self.fps.setEnabled(
                self.fps_mode.currentData() == FPS_MODE_CUSTOM
            )
        )
        self.frame_limit = QSpinBox()
        self.frame_limit.setRange(0, 10_000_000)
        self.frame_limit.setSpecialValueText("FULL CLIP")
        form.addRow("Codec", self.output_codec)
        form.addRow("Output folder", folder_row)
        form.addRow("File name", self.output_name)
        form.addRow("Resolved path", self.output_path_preview)
        form.addRow("Frame rate", self.fps_mode)
        form.addRow("Output FPS", self.fps)
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
        self._output_fields_changed()
        self._last_auto_output = self.output_path.text().strip() or None
        return panel

    def _apply_style(self) -> None:
        self.setStyleSheet(
            """
            QMainWindow, QWidget {
                background:#08090a;
                color:#f7f8f8;
                font-family:'Inter Variable','Inter','SF Pro Text','-apple-system','Segoe UI';
                font-size:11px;
            }
            QLabel { background:transparent; }
            QFrame#topBar { background:#0f1011; border-bottom:1px solid #23252a; }
            QLabel#appTitle { color:#f7f8f8; font-size:15px; font-weight:590; }
            QLabel#appSubtitle { color:#62666d; font-size:9px; letter-spacing:.8px; }
            QLabel#projectTitle { color:#f7f8f8; font-size:11px; font-weight:590; }
            QLabel#profileLabel { color:#d0d6e0; font-size:10px; }
            QLabel#statusPill {
                color:#10b981;
                border:1px solid #23252a;
                border-radius:6px;
                padding:3px 7px;
                font-size:8px;
                font-weight:590;
                letter-spacing:1px;
            }
            QFrame#inspectorPanel, QFrame#previewPanel,
            QFrame#timingPanel, QFrame#logPanel {
                background:#0f1011;
                border:1px solid rgba(255,255,255,0.08);
                border-radius:6px;
            }
            QFrame#libraryPanel { background:transparent; border:0; }
            QFrame#librarySection {
                background:#0f1011;
                border:0;
                border-bottom:1px solid rgba(255,255,255,0.08);
                border-radius:0;
            }
            QFrame#previewPanel { background:#0f1011; }
            QFrame#actionBar {
                background:#0f1011;
                border:0;
                border-top:1px solid rgba(255,255,255,0.08);
                border-radius:0;
            }
            QFrame#inspectorSection { background:transparent; border:0; border-bottom:1px solid #23252a; }
            QFrame#formSeparator {
                background:#23252a;
                border:0;
                min-height:1px;
                max-height:1px;
                margin:3px 0;
            }
            QGroupBox {
                background:transparent;
                border:0;
                border-top:1px solid #23252a;
                border-radius:0;
                margin-top:10px;
                padding:12px 2px 5px;
                color:#d0d6e0;
                font-weight:510;
            }
            QGroupBox::title { subcontrol-origin:margin; left:2px; padding:0 4px; color:#d0d6e0; }
            QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit, QTableWidget, QTreeWidget {
                background:#191a1b;
                border:1px solid #34343a;
                border-radius:5px;
                padding:4px;
                selection-background-color:#5e6ad2;
                selection-color:#f7f8f8;
            }
            QLineEdit:focus, QSpinBox:focus, QDoubleSpinBox:focus, QComboBox:focus { border-color:#7170ff; }
            QComboBox::drop-down {
                width:22px;
                border:0;
                border-left:1px solid #34343a;
                background:#191a1b;
            }
            QComboBox::drop-down:hover { background:#28282c; }
            QComboBox::down-arrow { width:9px; height:7px; }
            QComboBox QAbstractItemView {
                background:#191a1b;
                border:1px solid #34343a;
                outline:0;
                padding:3px;
                selection-background-color:#5e6ad2;
            }
            QPushButton {
                background:#191a1b;
                color:#d0d6e0;
                border:1px solid #23252a;
                border-radius:6px;
                padding:5px 8px;
                font-weight:510;
            }
            QPushButton:hover { background:#28282c; border-color:#3e3e44; color:#f7f8f8; }
            QPushButton:pressed { background:#0f1011; }
            QPushButton:checked { color:#f7f8f8; border-color:#7170ff; background:#28282c; }
            QPushButton:disabled { color:#62666d; background:#0f1011; border-color:#23252a; }
            QPushButton#topButton { background:transparent; padding:4px 7px; color:#d0d6e0; }
            QPushButton#primaryButton { background:#5e6ad2; color:#f7f8f8; border-color:#7170ff; }
            QPushButton#primaryButton:hover { background:#828fff; }
            QPushButton#secondaryButton, QPushButton#quietButton { background:#191a1b; color:#d0d6e0; }
            QPushButton#quietButton { padding:6px 10px; }
            QPushButton#iconButton { padding:5px 9px; min-width:28px; }
            QPushButton#dangerButton { background:#1c2026; color:#bd8998; border-color:#48333a; }
            QPushButton#dangerButton:hover { background:#2b2025; color:#f0b3c3; border-color:#80505e; }
            QPushButton#dangerButton:disabled { color:#5f565b; background:#171a1f; border-color:#29272a; }
            QPushButton#layoutChoice {
                background:#191a1b;
                color:#d0d6e0;
                border:1px solid #34343a;
                padding:10px 12px;
                text-align:left;
            }
            QPushButton#layoutChoice:hover { background:#28282c; border-color:#3e3e44; }
            QPushButton#layoutChoice:checked {
                background:#28282c;
                color:#f7f8f8;
                border:1px solid #7170ff;
            }
            QFrame#selectedMediaCard {
                background:#141516;
                border:1px solid #34343a;
                border-radius:7px;
            }
            QFrame#selectedMediaCard[state='ready'] { border-color:#3f6255; }
            QFrame#selectedMediaCard[state='warning'] { border-color:#735744; }
            QLabel#selectedMediaState {
                color:#8a8f98;
                font-size:9px;
                font-weight:650;
                letter-spacing:.5px;
            }
            QLabel#selectedMediaState[state='ready'] { color:#7fc5a9; }
            QLabel#selectedMediaState[state='warning'] { color:#d6a274; }
            QLabel#selectedMediaFiles {
                color:#d0d6e0;
                background:#0f1011;
                border-top:1px solid #23252a;
                border-bottom:1px solid #23252a;
                padding:7px 8px;
                font-family:'Cascadia Mono','SF Mono','Menlo';
                font-size:10px;
            }
            QPushButton#workflowButton {
                background:rgba(255,255,255,0.03);
                color:#d0d6e0;
                border-color:rgba(255,255,255,0.08);
                font-size:10px;
                letter-spacing:.3px;
            }
            QPushButton#workflowButton:hover { border-color:#7170ff; background:#28282c; }
            QPushButton#cancelButton { color:#e4a2b9; border-color:#694050; max-width:90px; }
            QHeaderView::section {
                background:#0f1011;
                color:#8a8f98;
                border:0;
                border-bottom:1px solid #23252a;
                padding:5px 4px;
                font-size:9px;
                font-weight:590;
            }
            QTableWidget { border:0; background:#0f1011; }
            QTableWidget::item { border-bottom:1px solid #23252a; padding:3px; }
            QTableWidget::item:selected { background:#28282c; color:#f7f8f8; }
            QTreeWidget#mediaTree, QTreeWidget#timelineTree {
                border:0;
                border-top:1px solid rgba(255,255,255,0.08);
                border-radius:0;
                background:#0f1011;
                padding:5px 0 2px;
                show-decoration-selected:1;
            }
            QTreeWidget#mediaTree::item {
                min-height:26px;
                padding:2px 5px;
                border:0;
                border-bottom:1px solid rgba(255,255,255,0.055);
            }
            QTreeWidget#timelineTree::item { min-height:24px; padding:2px 4px; border:0; }
            QTreeWidget#mediaTree::item:hover { background:#1b1c20; color:#ffffff; }
            QTreeWidget#mediaTree::item:selected, QTreeWidget#timelineTree::item:selected { background:#28282c; color:#f7f8f8; }
            QTabWidget::pane { border:0; }
            QTabBar::tab {
                background:#0f1011;
                color:#62666d;
                border:0;
                border-bottom:2px solid transparent;
                padding:7px 11px;
                font-weight:510;
            }
            QTabBar::tab:selected { color:#f7f8f8; border-bottom:2px solid #7170ff; }
            QLabel#durationBadge { color:#d0d6e0; padding:2px 5px; font-weight:590; }
            QLabel#previewLimit {
                color:#8a8f98;
                border:1px solid #34343a;
                border-radius:6px;
                padding:3px 7px;
                font-size:9px;
                font-weight:590;
            }
            QLabel#playheadTime {
                color:#d0d6e0;
                padding:2px 5px;
                font-family:'Cascadia Mono','SF Mono','Menlo';
                font-size:10px;
            }
            QLabel#sourceStatus { color:#8a8f98; font-size:10px; }
            QLabel#autosaveStatus { color:#10b981; font-size:9px; padding:0 8px; }
            QScrollArea { border:0; background:transparent; }
            QSplitter#workspaceSplitter::handle:horizontal {
                background:rgba(255,255,255,0.08);
                margin:9px 2px;
                border-radius:2px;
            }
            QSplitter#workspaceSplitter::handle:horizontal:hover {
                background:#7170ff;
            }
            QSplitter#librarySplitter::handle:vertical {
                background:transparent;
                border-top:1px solid #34343a;
                margin:3px 10px;
            }
            QSplitter#librarySplitter::handle:vertical:hover {
                border-top:2px solid #7170ff;
            }
            QProgressBar {
                border:1px solid #34343a;
                border-radius:6px;
                background:#191a1b;
                text-align:center;
                color:#d0d6e0;
            }
            QProgressBar::chunk { background:#5e6ad2; border-radius:5px; }
            QLabel[muted='true'] { color:#8a8f98; }
            QLabel[sectionTitle='true'] { color:#f7f8f8; font-size:11px; font-weight:590; letter-spacing:.7px; }
            QLabel[inspectorTitle='true'] { color:#d0d6e0; font-size:10px; font-weight:590; letter-spacing:.6px; }
            QStatusBar { background:#0f1011; border-top:1px solid #23252a; color:#8a8f98; }
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
        try:
            is_working_snapshot = path.resolve().is_relative_to(
                self._working_dir.resolve()
            )
        except OSError:
            is_working_snapshot = False
        if not is_working_snapshot or len(cameras) not in self._rig_profiles:
            if len(cameras) == 5:
                self._rig_profiles.pop(3, None)
            self._rig_profiles[len(cameras)] = json.loads(json.dumps(raw))
        if not is_working_snapshot:
            self._plate_reset_cameras = json.loads(json.dumps(cameras))
        self._plate_numbers = None
        self._source_probes = None
        self._source_fps_error = None
        self._source_overrides = {}
        self._tc_alignment = None
        self._tc_alignment_path = None
        self.source_table.blockSignals(True)
        self.source_table.set_rig(cameras)
        self.source_table.set_camera_numbers(
            list(PLATE_NUMBERS_BY_COUNT[len(cameras)])
        )
        self.source_table.blockSignals(False)
        output = raw.setdefault("output", {})
        self._loading_config = True
        self.canvas_width.setValue(int(output.get("width", 15360)))
        self.canvas_height.setValue(int(output.get("height", 3968)))
        self.h_fov.setValue(float(output.get("horizontal_fov_deg", 180.0)))
        self.v_fov.setValue(float(output.get("vertical_fov_deg", 52.0)))
        self.center_yaw.setValue(float(output.get("center_yaw_deg", 0.0)))
        self.center_pitch.setValue(float(output.get("center_pitch_deg", 0.0)))
        self.seam_feather.setValue(float(output.get("seam_feather_deg", 4.0)))
        self._loading_config = False
        self._update_canvas_ratio()
        flow = raw.setdefault("flow", {})
        self.flow_enabled.setChecked(bool(flow.get("enabled", False)))
        self.flow_preset.setCurrentText(str(flow.get("preset", "medium")))
        self.flow_max.setValue(float(flow.get("max_displacement_px", 32.0)))
        color = raw.setdefault("color", {})
        video_settings = raw.get("video")
        repaired_p3_pq = repair_legacy_p3_pq_target(
            color,
            video_settings if isinstance(video_settings, dict) else {},
        )
        if repaired_p3_pq:
            self._append_log(
                "Recovered legacy P3 target: Apple EDR → ST2084 P3-D65 PQ."
            )
        mode = str(color.get("mode", "passthrough"))
        self._loading_config = True
        self.color_mode.setCurrentIndex(max(0, self.color_mode.findData(mode)))
        self.ocio_config.setText(str(color.get("ocio_config") or ""))
        self.ocio_config.setCursorPosition(0)
        camera_space = next((str(camera.get("colorspace")) for camera in cameras if camera.get("colorspace")), "")
        _request_ocio_combo_value(self.input_space, camera_space)
        _request_ocio_combo_value(
            self.working_space, str(color.get("working_space") or "")
        )
        _request_ocio_combo_value(
            self.output_space, str(color.get("output_space") or "")
        )
        output_mode = str(color.get("output_mode") or "colorspace")
        self.output_mode.setCurrentIndex(max(0, self.output_mode.findData(output_mode)))
        if mode == "ocio":
            self._reload_ocio_spaces(quiet=True)
            self._reload_ocio_delivery(
                display=str(color.get("display") or ""),
                view=str(color.get("view") or ""),
                quiet=True,
            )
        self.integer_dither.setChecked(bool(color.get("integer_dither", True)))
        self._update_color_match_cameras(cameras, color)
        self._loading_config = False
        video = raw.setdefault("video", {"fps": 29.97})
        codec = str(video.get("output_codec", "prores-hq"))
        codec_index = self.output_codec.findData(codec)
        self.output_codec.setCurrentIndex(
            self.output_codec.findData("prores-hq") if codec_index < 0 else codec_index
        )
        fps_mode = _frame_rate_mode(raw)
        self.fps_mode.setCurrentIndex(max(0, self.fps_mode.findData(fps_mode)))
        self.fps.setValue(float(video.get("fps", 29.97)))
        self.fps.setEnabled(fps_mode == FPS_MODE_CUSTOM)
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

    def _update_canvas_ratio(self) -> None:
        width = self.canvas_width.value()
        height = self.canvas_height.value()
        ratio = width / max(1, height)
        self.canvas_ratio.setText(f"{ratio:.3f}:1  ·  MANUAL")

    def _canvas_controls_changed(self) -> None:
        self._update_canvas_ratio()
        if self._loading_config or not self.config_data:
            return
        self._schedule_live_preview("Canvas framing updated")

    def _stitch_setting_changed(self, *_args) -> None:
        if self._loading_config or not self.config_data:
            return
        self._schedule_live_preview("Stitch refinement updated")

    def fit_full_plates(self) -> None:
        try:
            config_path = self._write_working_config()
            fitted = recommend_full_plate_canvas(parse_config(config_path))
        except Exception as error:
            self._error("Full Plate Fit", str(error))
            return
        self._loading_config = True
        self.canvas_width.setValue(fitted.width)
        self.canvas_height.setValue(fitted.height)
        self.h_fov.setValue(fitted.horizontal_fov_deg)
        self.v_fov.setValue(fitted.vertical_fov_deg)
        self._loading_config = False
        self._canvas_controls_changed()
        self.canvas_ratio.setText(
            f"{fitted.width / fitted.height:.3f}:1  ·  FULL PLATES"
        )
        self._append_log(
            "Full Plate Fit: "
            f"{fitted.width}×{fitted.height} · "
            f"H {fitted.horizontal_fov_deg:.2f}° · V {fitted.vertical_fov_deg:.2f}°"
        )
        self.statusBar().showMessage(
            "Full plate boundaries fitted · create Preview to inspect the uncropped canvas",
            12000,
        )

    def _ensure_ocio_controls_valid(self) -> None:
        identifier = self.ocio_config.text().strip()
        if not identifier:
            raise ValueError("OCIO config is required")
        if (
            getattr(self, "_loaded_ocio_identifier", None) != identifier
            or not getattr(self, "_loaded_ocio_spaces", ())
        ):
            if not self._reload_ocio_spaces(quiet=True):
                raise ValueError("The selected OCIO config could not be loaded")
        spaces = tuple(getattr(self, "_loaded_ocio_spaces", ()))
        for label, combo in (
            ("Input transform", self.input_space),
            ("Working space", self.working_space),
            ("Output transform", self.output_space),
        ):
            value = combo.currentText().strip()
            if value not in spaces:
                resolved = _populate_ocio_combo(combo, spaces, value)
                if not resolved:
                    raise ValueError(f"{label} is not available in the OCIO config")
        if self.output_mode.currentData() == "display_view":
            displays = getattr(self, "_delivery_views", {})
            display = _delivery_display_value(self.output_display)
            view = self.output_view.currentText().strip()
            if display not in displays or view not in displays.get(display, ()):
                if not self._reload_ocio_delivery(
                    display=display,
                    view=view,
                    quiet=True,
                ):
                    raise ValueError("The selected OCIO output display/view is unavailable")

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
        previous_color = raw.get("color")
        previous_color = previous_color if isinstance(previous_color, dict) else {}
        mode = str(self.color_mode.currentData())
        if mode == "passthrough":
            raw["color"] = {
                "mode": "passthrough",
                "match_enabled": False,
                "integer_dither": self.integer_dither.isChecked(),
                "dither_seed": int(raw.get("color", {}).get("dither_seed", 7349)),
            }
            for camera in cameras:
                camera.pop("colorspace", None)
        else:
            self._ensure_ocio_controls_valid()
            required = [
                self.ocio_config.text(),
                self.input_space.currentText(),
                self.working_space.currentText(),
            ]
            output_mode = str(self.output_mode.currentData())
            if output_mode == "display_view":
                required.extend(
                    [_delivery_display_value(self.output_display), self.output_view.currentText()]
                )
            else:
                required.append(self.output_space.currentText())
            if not all(value.strip() for value in required):
                raise ValueError("OCIO config and the selected delivery fields are required")
            raw["color"] = {
                "mode": "ocio",
                "ocio_config": self.ocio_config.text().strip(),
                "working_space": self.working_space.currentText().strip(),
                "output_mode": output_mode,
                "match_enabled": self.color_match_enabled.isChecked(),
                "match_reference": self.color_match_reference.currentData(),
                "match_strength": self.color_match_strength.value() / 100.0,
                "preserve_luminance": True,
                "integer_dither": self.integer_dither.isChecked(),
                "dither_seed": int(raw.get("color", {}).get("dither_seed", 7349)),
            }
            match_space = str(previous_color.get("match_space", "")).strip()
            if self.color_match_enabled.isChecked() and match_space:
                raw["color"]["match_space"] = match_space
            if output_mode == "display_view":
                raw["color"].update(
                    {
                        "display": _delivery_display_value(self.output_display),
                        "view": self.output_view.currentText().strip(),
                    }
                )
            else:
                raw["color"]["output_space"] = self.output_space.currentText().strip()
            for camera in cameras:
                camera["colorspace"] = self.input_space.currentText().strip()
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
        frame_rate_mode = str(self.fps_mode.currentData() or FPS_MODE_MATCH_SOURCE)
        metadata = raw.setdefault("_vpstitch", {})
        if not isinstance(metadata, dict):
            metadata = {}
            raw["_vpstitch"] = metadata
        metadata["fps_mode"] = frame_rate_mode
        if self._source_fps_error:
            raise ValueError(self._source_fps_error)
        source_fps = _matching_source_frame_rate(self._source_probes or [])
        if source_fps is not None:
            metadata["source_fps"] = source_fps
            if frame_rate_mode == FPS_MODE_MATCH_SOURCE:
                video["fps"] = source_fps
        if mode == "ocio":
            for key in ("color_primaries", "color_trc", "colorspace", "color_range"):
                video.pop(key, None)
            if str(self.output_mode.currentData()) == "display_view":
                video.update(
                    _display_view_video_tags(
                        _delivery_display_value(self.output_display),
                        self.output_view.currentText().strip(),
                    )
                )
            elif "rec.709" in self.output_space.currentText().casefold():
                video.update(
                    {
                        "color_primaries": "bt709",
                        "color_trc": "bt709",
                        "colorspace": "bt709",
                        "color_range": "tv",
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

    def import_media_paths(
        self,
        files: list[str],
        *,
        destination_bin_id: str | None | object = _MEDIA_BIN_UNSET,
    ) -> list[MediaRecord]:
        existing = {str(item.path) for item in self.project_store.media}
        ordered = sorted(
            files,
            key=lambda path: (
                plate_number(path) if plate_number(path) is not None else 99,
                Path(path).name.lower(),
            ),
        )
        if destination_bin_id is _MEDIA_BIN_UNSET:
            bin_id = self._media_tree_destination_bin(default_to_first=True)
        else:
            bin_id = (
                None
                if destination_bin_id is None
                else str(destination_bin_id)
            )
        if bin_id is not None and not any(
            folder.id == bin_id for folder in self.project_store.bins
        ):
            raise ProjectError("The selected import folder no longer exists.")
        records: list[MediaRecord] = []
        next_order = len(self.project_store.list_media(bin_id))
        for path in ordered:
            if str(Path(path)) in existing:
                continue
            item = MediaRecord.create(
                path,
                bin_id=bin_id,
                order=next_order,
            )
            records.append(item)
            existing.add(str(item.path))
            next_order += 1
        added = list(self.project_store.add_media_many(records)) if records else []
        self._refresh_media_tree()
        added_ids = {item.id for item in added}
        iterator = QTreeWidgetItemIterator(self.media_tree)
        while iterator.value() is not None:
            item = iterator.value()
            if item.data(0, Qt.ItemDataRole.UserRole + 1) in added_ids:
                item.setSelected(True)
            iterator += 1
        if added and self._auto_workflows_enabled:
            self._queue_source_proxies(added)
        return added

    def _queue_source_proxies(self, records: list[MediaRecord]) -> None:
        current_id = (
            self._source_proxy_current[0]
            if self._source_proxy_current is not None
            else None
        )
        changed = False
        for record in records:
            try:
                plan = plan_source_proxy(record.path, self._cache_dir)
            except (FileNotFoundError, OSError, ValueError) as error:
                self.project_store.update_media(
                    record.id,
                    source_cache_path=None,
                    source_cache_status=MediaCacheStatus.FAILED,
                    source_cache_error=str(error),
                )
                changed = True
                continue
            if source_proxy_ready(plan):
                self.project_store.update_media(
                    record.id,
                    source_cache_path=plan.output,
                    source_cache_status=MediaCacheStatus.READY,
                    source_cache_error=None,
                )
                changed = True
                continue
            if record.id != current_id and record.id not in self._source_proxy_queue:
                self.project_store.update_media(
                    record.id,
                    source_cache_path=plan.output,
                    source_cache_status=MediaCacheStatus.PENDING,
                    source_cache_error=None,
                )
                self._source_proxy_queue.append(record.id)
                changed = True
        if changed:
            self._refresh_media_tree()
        self._start_next_source_proxy()

    def _cancel_source_proxy_items(self, media_ids: set[str] | None = None) -> None:
        """Cancel queued/background source proxies without deleting reusable cache files."""
        if media_ids is None:
            self._source_proxy_queue.clear()
        else:
            self._source_proxy_queue = [
                media_id
                for media_id in self._source_proxy_queue
                if media_id not in media_ids
            ]
        current = self._source_proxy_current
        if current is None or (
            media_ids is not None and current[0] not in media_ids
        ):
            return
        self._source_proxy_current = None
        self._source_proxy_attempts.clear()
        self._source_proxy_backend = "unknown"
        if self._source_proxy_process.state() != QProcess.ProcessState.NotRunning:
            self._source_proxy_process.kill()
            self._source_proxy_process.waitForFinished(3000)
        current[1].temporary.unlink(missing_ok=True)
        self._source_proxy_output.clear()

    def _start_next_source_proxy(self) -> None:
        if self._closing or self._source_proxy_current is not None:
            return
        if self._source_proxy_process.state() != QProcess.ProcessState.NotRunning:
            return
        while self._source_proxy_queue:
            media_id = self._source_proxy_queue.pop(0)
            record = next(
                (item for item in self.project_store.media if item.id == media_id),
                None,
            )
            if record is None:
                continue
            try:
                plan = plan_source_proxy(record.path, self._cache_dir)
                if source_proxy_ready(plan):
                    self.project_store.update_media(
                        media_id,
                        source_cache_path=plan.output,
                        source_cache_status=MediaCacheStatus.READY,
                        source_cache_error=None,
                    )
                    continue
                attempts = list(source_proxy_commands(plan))
            except (FileNotFoundError, OSError, ValueError) as error:
                self.project_store.update_media(
                    media_id,
                    source_cache_status=MediaCacheStatus.FAILED,
                    source_cache_error=str(error),
                )
                continue
            self._source_proxy_current = (media_id, plan)
            self._source_proxy_attempts = attempts
            self.project_store.update_media(
                media_id,
                source_cache_path=plan.output,
                source_cache_status=MediaCacheStatus.BUILDING,
                source_cache_error=None,
            )
            self._refresh_media_tree()
            if not self._start_source_proxy_attempt():
                error = (
                    self._source_proxy_process.errorString()
                    or "no source proxy encoder could start"
                )
                self._source_proxy_current = None
                self.project_store.update_media(
                    media_id,
                    source_cache_status=MediaCacheStatus.FAILED,
                    source_cache_error=error,
                )
                self._refresh_media_tree()
                continue
            return
        self._refresh_media_tree()

    def _start_source_proxy_attempt(self) -> bool:
        current = self._source_proxy_current
        while current is not None and self._source_proxy_attempts:
            command = self._source_proxy_attempts.pop(0)
            self._source_proxy_backend = command.encoder
            self._source_proxy_output.clear()
            current[1].temporary.unlink(missing_ok=True)
            self._append_log(
                f"SOURCE PROXY [{command.encoder}]: "
                f"{current[1].source.name} → {current[1].output.name}"
            )
            self._source_proxy_process.start(command.program, list(command.arguments))
            if self._source_proxy_process.waitForStarted(3000):
                return True
            self._append_log(
                f"SOURCE PROXY ENCODER UNAVAILABLE: {command.encoder}: "
                f"{self._source_proxy_process.errorString()}"
            )
        return False

    def _read_source_proxy_process(self) -> None:
        chunk = bytes(self._source_proxy_process.readAllStandardOutput())
        if not chunk:
            return
        self._source_proxy_output.extend(chunk)
        if len(self._source_proxy_output) > 256 * 1024:
            del self._source_proxy_output[: len(self._source_proxy_output) - 256 * 1024]
        current = self._source_proxy_current
        if current is None:
            return
        text = chunk.decode("utf-8", errors="replace")
        frames = re.findall(r"(?m)^frame=(\d+)\s*$", text)
        if frames:
            self.statusBar().showMessage(
                f"Caching {current[1].source.name} · frame {frames[-1]}",
                1200,
            )

    def _source_proxy_finished(self, exit_code: int, _status) -> None:  # type: ignore[no-untyped-def]
        current = self._source_proxy_current
        if current is None:
            return
        media_id, plan = current
        if self._closing:
            self._source_proxy_current = None
            plan.temporary.unlink(missing_ok=True)
            return
        if exit_code != 0 and self._source_proxy_attempts:
            detail = self._source_proxy_output.decode(
                "utf-8", errors="replace"
            ).strip()
            self._append_log(
                f"SOURCE PROXY FALLBACK: {self._source_proxy_backend} failed: "
                f"{detail[-500:] or f'exit {exit_code}'}"
            )
            if self._start_source_proxy_attempt():
                return
        self._source_proxy_current = None
        try:
            if exit_code != 0:
                detail = self._source_proxy_output.decode(
                    "utf-8", errors="replace"
                ).strip()
                raise OSError(detail[-1200:] or f"FFmpeg exited with code {exit_code}")
            output = finalize_source_proxy(plan, encoder=self._source_proxy_backend)
            self.project_store.update_media(
                media_id,
                source_cache_path=output,
                source_cache_status=MediaCacheStatus.READY,
                source_cache_error=None,
            )
            self._append_log(f"SOURCE PROXY READY: {output}")
            self.statusBar().showMessage(f"Source proxy ready · {plan.source.name}", 2500)
        except (OSError, ProjectError) as error:
            plan.temporary.unlink(missing_ok=True)
            try:
                self.project_store.update_media(
                    media_id,
                    source_cache_status=MediaCacheStatus.FAILED,
                    source_cache_error=str(error),
                )
            except ProjectError:
                pass
            self._append_log(f"SOURCE PROXY FAILED: {plan.source.name}: {error}")
        self._source_proxy_output.clear()
        self._source_proxy_attempts.clear()
        self._source_proxy_backend = "unknown"
        self._refresh_media_tree()
        QTimer.singleShot(0, self._start_next_source_proxy)

    def _cached_playback_sources(self, sources: list[str]) -> list[str]:
        records = {
            str(Path(str(item.path))): item
            for item in self.project_store.media_for_paths(sources)
        }
        cached: list[str] = []
        for source in sources:
            record = records.get(str(Path(source)))
            cache_path = (
                None
                if record is None or record.source_cache_path is None
                else Path(str(record.source_cache_path))
            )
            if (
                record is None
                or record.source_cache_status is not MediaCacheStatus.READY
                or cache_path is None
                or not cache_path.is_file()
            ):
                return sources
            cached.append(str(cache_path))
        return cached

    def _cached_proxy_records(
        self, sources: list[str]
    ) -> list[MediaRecord] | None:
        """Return media records when every source is its ready low-res cache."""
        by_cache_path = {
            str(Path(str(item.source_cache_path)).resolve()): item
            for item in self.project_store.media
            if item.source_cache_status is MediaCacheStatus.READY
            and item.source_cache_path is not None
        }
        records: list[MediaRecord] = []
        for source in sources:
            record = by_cache_path.get(str(Path(source).resolve()))
            if record is None:
                return None
            records.append(record)
        return records

    @staticmethod
    def _match_camera_geometry_to_proxy_dimensions(
        raw: dict,
        dimensions: list[tuple[int, int]],
    ) -> None:
        """Retarget camera intrinsics to native proxy frames without changing FOV."""
        cameras = raw.get("cameras")
        if not isinstance(cameras, list) or len(cameras) != len(dimensions):
            raise ValueError("proxy dimensions do not match the rig camera count")
        for camera, (width, height) in zip(cameras, dimensions, strict=True):
            old_width = int(camera["width"])
            old_height = int(camera["height"])
            if min(old_width, old_height, width, height) < 1:
                raise ValueError("proxy camera dimensions must be positive")
            scale_x = width / old_width
            scale_y = height / old_height
            camera["width"] = int(width)
            camera["height"] = int(height)
            lens = camera["lens"]
            lens["fx"] = float(lens["fx"]) * scale_x
            lens["cx"] = float(lens["cx"]) * scale_x
            lens["fy"] = float(lens["fy"]) * scale_y
            lens["cy"] = float(lens["cy"]) * scale_y
            if lens.get("circle_radius") is not None:
                lens["circle_radius"] = float(lens["circle_radius"]) * (
                    (scale_x + scale_y) * 0.5
                )

    def choose_videos(self) -> None:
        destination_bin_id = self._media_tree_destination_bin(
            default_to_first=True
        )
        destination_label = self._bin_display_path(destination_bin_id)
        if self._import_dialog is None:
            dialog = QFileDialog(self)
            dialog.setWindowTitle("Import Media · Select Camera Clips")
            dialog.setFileMode(QFileDialog.FileMode.ExistingFiles)
            dialog.setAcceptMode(QFileDialog.AcceptMode.AcceptOpen)
            dialog.setNameFilter(VIDEO_FILTER)
            self._import_dialog = dialog
        initial_dir = _preferred_storage_directory(
            self.settings,
            "lastImportDir",
            QStandardPaths.writableLocation(
                QStandardPaths.StandardLocation.DownloadLocation
            ),
        )
        if initial_dir:
            self._import_dialog.setDirectory(initial_dir)
        if self._import_dialog.exec() != QDialog.DialogCode.Accepted:
            self._import_dialog.hide()
            return
        files = self._import_dialog.selectedFiles()
        self._import_dialog.hide()
        if files:
            import_root = Path(files[0]).parent
            self.settings.setValue("lastImportDir", str(import_root))
            _remember_storage_root(self.settings, import_root)
            self.settings.sync()
            try:
                added = self.import_media_paths(
                    files,
                    destination_bin_id=destination_bin_id,
                )
                self.statusBar().showMessage(
                    f"Imported {len(added)} media clips into {destination_label} · "
                    "right-click the selected clips to add them to a timeline",
                    12000,
                )
            except Exception as error:
                self._error("Import Media", str(error))

    def clear_sources(self) -> None:
        self.source_table.set_paths([""] * self.source_table.camera_count())
        self.source_table.set_camera_numbers(
            list(PLATE_NUMBERS_BY_COUNT[self.source_table.camera_count()])
        )
        self._plate_numbers = None
        self._source_probes = None
        self._source_fps_error = None
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
        self._live_preview_timer.stop()
        self._live_preview_pending = False
        self._auto_cache_requested = False
        self._auto_cache_in_progress = False
        self._pending_playback_request = False
        self._stop_playback(clear=True)
        self._cleanup_reference_dir(self._last_reference_dir)
        self._tc_alignment = None
        self._tc_alignment_path = None
        self._last_reference_dir = None
        self._last_reference_config_path = None
        self._reference_frame_index = None
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
        self.preview_note.setText(
            "Quick Preview checks one frame · playback proxy prewarms after TC Align"
        )
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
            self._stop_playback(preserve_image=True)
            self._set_timeline_range(lower, upper)
            self._seek_loaded_playback(
                self.timeline_playhead.value(), show_video=False
            )
            self._save_active_timeline()

    def _timeline_spin_changed(self) -> None:
        if not self._timeline_updating:
            self._stop_playback(preserve_image=True)
            self._set_timeline_range(self.timeline_in.value(), self.timeline_out.value())
            self._seek_loaded_playback(
                self.timeline_playhead.value(), show_video=False
            )
            self._save_active_timeline()

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
        if self._seek_loaded_playback(frame):
            return
        if self._request_live_proxy_frame(frame, "Timeline frame"):
            return
        self._scrub_preview()

    def _handle_preview_command(self, command: str) -> None:
        if command == "fullscreen":
            self.toggle_preview_fullscreen()
        elif command == "plate-move":
            self._toggle_plate_move_mode()
        elif command == "play-pause":
            self.toggle_playback()
        elif command == "reverse":
            self.play_reverse()
        elif command == "stop":
            self.stop_playback()
        elif command == "forward":
            self.play_forward()
        elif command == "step-back":
            if self._plate_move_mode:
                self._nudge_selected_plate(-1, 0)
            else:
                self.step_playback(-1)
        elif command == "step-forward":
            if self._plate_move_mode:
                self._nudge_selected_plate(1, 0)
            else:
                self.step_playback(1)
        elif command == "move-up":
            self._nudge_selected_plate(0, 1)
        elif command == "move-down":
            self._nudge_selected_plate(0, -1)
        elif command == "move-fine-left":
            if self._plate_move_mode:
                self._nudge_selected_plate(-1, 0, fine=True)
            else:
                self.step_playback(-1)
        elif command == "move-fine-right":
            if self._plate_move_mode:
                self._nudge_selected_plate(1, 0, fine=True)
            else:
                self.step_playback(1)
        elif command == "move-fine-up":
            self._nudge_selected_plate(0, 1, fine=True)
        elif command == "move-fine-down":
            self._nudge_selected_plate(0, -1, fine=True)

    def _handle_preview_shortcut(self, command: str) -> None:
        plate_commands = {
            "plate-move",
            "step-back",
            "step-forward",
            "move-up",
            "move-down",
            "move-fine-left",
            "move-fine-right",
            "move-fine-up",
            "move-fine-down",
        }
        if command in plate_commands and (
            command == "plate-move" or self._plate_move_mode
        ):
            focus = QApplication.focusWidget()
            if not isinstance(
                focus,
                (QLineEdit, QSpinBox, QDoubleSpinBox, QComboBox, QPlainTextEdit),
            ):
                self._handle_preview_command(command)
            return
        if command == "fullscreen" or not self._playback_focus_uses_space():
            self._handle_preview_command(command)

    def toggle_preview_fullscreen(self) -> None:
        if self._fullscreen_preview is not None:
            self._fullscreen_preview.close()
            return
        dialog = QDialog(
            None,
            Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint,
        )
        dialog.setWindowTitle("VP Stitch Preview")
        dialog.setStyleSheet("background:#05070a;")
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        layout = QVBoxLayout(dialog)
        layout.setContentsMargins(0, 0, 0, 0)
        playback_ready = False
        if self._playback_path is not None and self._playback_path.is_file():
            try:
                current_key, _ = self._playback_signature()
                playback_ready = current_key == self._playback_key
            except (OSError, ValueError):
                pass
        if playback_ready:
            fullscreen_video = PlaybackVideoWidget()
            fullscreen_video.setStyleSheet("background:#05070a;")
            fullscreen_video.commandRequested.connect(self._handle_preview_command)
            layout.addWidget(fullscreen_video)
            self._fullscreen_video = fullscreen_video
        else:
            pixmap = self.preview.current_pixmap() or self.preview.viewport().grab()
            self._fullscreen_live_label = FullscreenPreviewLabel(pixmap)
            layout.addWidget(self._fullscreen_live_label)

        def restore_preview_output(_result: int) -> None:
            if self._fullscreen_video is not None:
                if not self._closing:
                    self.media_player.setVideoOutput(self.video_preview)
                self._fullscreen_video = None
            self._fullscreen_live_label = None
            self._fullscreen_preview = None

        escape_shortcut = QShortcut(QKeySequence("Escape"), dialog)
        escape_shortcut.activated.connect(dialog.close)
        for key, command in (
            ("Space", "play-pause"),
            ("J", "reverse"),
            ("K", "stop"),
            ("L", "forward"),
            (Qt.Key.Key_Left, "step-back"),
            (Qt.Key.Key_Right, "step-forward"),
        ):
            shortcut = QShortcut(QKeySequence(key), dialog)
            shortcut.activated.connect(
                lambda selected=command: self._handle_preview_command(selected)
            )
        dialog.finished.connect(restore_preview_output)
        self._fullscreen_preview = dialog
        screen = self.preview.screen() or QApplication.primaryScreen()
        if screen is not None:
            dialog.setGeometry(screen.geometry())
        dialog.showFullScreen()
        if screen is not None and dialog.windowHandle() is not None:
            dialog.windowHandle().setScreen(screen)
        if self._fullscreen_video is not None:
            QTimer.singleShot(
                0,
                lambda: (
                    self.media_player.setVideoOutput(self._fullscreen_video)
                    if self._fullscreen_video is not None
                    else None
                ),
            )
        dialog.raise_()
        dialog.activateWindow()

    def play_forward(self) -> None:
        self._reverse_timer.stop()
        if self._playback_path is not None and self._playback_path.is_file():
            try:
                current_key, _ = self._playback_signature()
            except (OSError, ValueError):
                current_key = None
            if current_key == self._playback_key:
                if (
                    self.media_player.duration() > 0
                    and self.media_player.position() >= self.media_player.duration() - 50
                ):
                    self.media_player.setPosition(0)
                self.preview_stack.setCurrentWidget(self.video_preview)
                self.media_player.play()
                return
        if self._request_live_proxy_frame(
            self.timeline_playhead.value(),
            "Live playback",
            playing=True,
            direction=1,
        ):
            self.playback_button.setText("Ⅱ  PAUSE")
            return
        self.toggle_playback()

    def play_reverse(self) -> None:
        if self._playback_path is None or not self._playback_path.is_file():
            if self._request_live_proxy_frame(
                self.timeline_playhead.value(),
                "Live reverse playback",
                playing=True,
                direction=-1,
            ):
                self.playback_button.setText("◀  REVERSE")
                return
            self.statusBar().showMessage("Playback cache is not ready yet", 5000)
            return
        try:
            current_key, _ = self._playback_signature()
        except (OSError, ValueError):
            current_key = None
        if current_key != self._playback_key:
            if self._request_live_proxy_frame(
                self.timeline_playhead.value(),
                "Live reverse playback",
                playing=True,
                direction=-1,
            ):
                self.playback_button.setText("◀  REVERSE")
                return
            self.statusBar().showMessage("Playback cache is stale", 5000)
            return
        self.media_player.pause()
        fps = float(self._tc_alignment["fps"]) if self._tc_alignment else self.fps.value()
        self._reverse_timer.setInterval(max(10, int(round(1000 / max(1.0, fps)))))
        self._reverse_timer.start()
        self.preview_stack.setCurrentWidget(self.video_preview)
        self.playback_button.setText("◀  REVERSE")

    def _reverse_tick(self) -> None:
        if self._playback_path is None or not self._playback_path.is_file():
            self._reverse_timer.stop()
            return
        if self.media_player.position() <= 0:
            self._reverse_timer.stop()
            self.media_player.pause()
            return
        self.step_playback(-1, continuous=True)

    def stop_playback(self) -> None:
        self._stop_live_proxy_playback()
        self._reverse_timer.stop()
        self.media_player.pause()
        self.playback_button.setText("▶  PLAY")

    def step_playback(self, direction: int, *, continuous: bool = False) -> None:
        if not continuous:
            self._reverse_timer.stop()
            self.media_player.pause()
        fps = float(self._tc_alignment["fps"]) if self._tc_alignment else self.fps.value()
        frame_ms = max(1, int(round(1000 / max(1.0, fps))))
        if self._playback_path is not None and self._playback_path.is_file():
            try:
                current_key, _ = self._playback_signature()
            except (OSError, ValueError):
                current_key = None
        else:
            current_key = None
        if current_key == self._playback_key and self._playback_path is not None:
            if self._tc_alignment:
                lower, upper = self.timeline_bar.values()
                frame = max(
                    lower,
                    min(upper - 1, self.timeline_playhead.value() + direction),
                )
                self._set_playhead(frame)
                self.media_player.setPosition(
                    max(0, int(round(frame * 1000 / max(1.0, fps))))
                )
            else:
                self.media_player.setPosition(
                    max(
                        0,
                        min(
                            self.media_player.duration(),
                            self.media_player.position() + direction * frame_ms,
                        ),
                    )
                )
            self.preview_stack.setCurrentWidget(self.video_preview)
            return
        if self._tc_alignment:
            lower, upper = self.timeline_bar.values()
            frame = max(lower, min(upper - 1, self.timeline_playhead.value() + direction))
            self._set_playhead(frame)
            if self._request_live_proxy_frame(frame, "Timeline frame"):
                return
            self._scrub_preview()

    def _playback_focus_uses_space(self) -> bool:
        focus = QApplication.focusWidget()
        return isinstance(
            focus,
            (
                QLineEdit,
                QSpinBox,
                QDoubleSpinBox,
                QComboBox,
                QPlainTextEdit,
                QPushButton,
                QCheckBox,
                QTableWidget,
            ),
        )

    def _toggle_playback_shortcut(self) -> None:
        if not self._playback_focus_uses_space():
            self.toggle_playback()

    def _playback_signature(self) -> tuple[str, list[str]]:
        sources = self._validate_sources()
        playback_sources = self._cached_playback_sources(sources)
        config = self._collect_config()
        if self._tc_alignment and self._timeline_maximum > 0:
            # Playback is baked once for the complete TC-aligned common range.
            # Timeline IN/OUT only controls navigation and final delivery.
            config.setdefault("video", {})["frames"] = self._timeline_maximum
        fingerprints = []
        for source in sources:
            stat = Path(source).stat()
            fingerprints.append((source, stat.st_size, stat.st_mtime_ns))
        playback_fingerprints = []
        for source in playback_sources:
            stat = Path(source).stat()
            playback_fingerprints.append((source, stat.st_size, stat.st_mtime_ns))
        payload = {
            "playback_pipeline": 2,
            "config": config,
            "viewer_monitor": str(self.viewer_monitor.currentData() or "sdr-rec709"),
            "sources": fingerprints,
            "playback_sources": playback_fingerprints,
            "alignment": self._tc_alignment,
        }
        encoded = json.dumps(
            payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()[:20], playback_sources

    def _create_live_playback_session(
        self,
        playback_key: str,
        playback_sources: list[str],
    ) -> LivePlaybackSession:
        if not self._tc_alignment:
            raise ValueError("TC alignment is not available")
        full_config = self._write_working_config()
        directory = self._working_dir / "live-playback"
        directory.mkdir(parents=True, exist_ok=True)
        preview_config = directory / f"{playback_key}.json"
        max_width, max_height = live_playback_limits(len(playback_sources))
        width, height = preview_dimensions(
            self.canvas_width.value(),
            self.canvas_height.value(),
            max_width=max_width,
            max_height=max_height,
        )
        self._write_preview_config(
            full_config,
            preview_config,
            width,
            height,
            scale_cameras=True,
            viewer_transform=True,
        )
        config = parse_config(preview_config)
        alignment_path = self._playback_alignment_plan(
            playback_sources,
            f"live-{playback_key}",
        )
        payload = json.loads(alignment_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict) or config.video is None:
            raise ValueError("live playback config is invalid")
        plan = AlignedFramePlan.from_payload(
            payload,
            playback_sources,
            config.cameras,
            config.video.fps,
        )
        return LivePlaybackSession(
            playback_sources,
            config,
            plan,
            max_width=max_width,
            max_height=max_height,
        )

    def _request_live_proxy_frame(
        self,
        frame: int,
        message: str,
        *,
        playing: bool = False,
        direction: int = 1,
        draft: bool = False,
    ) -> bool:
        if self._closing or not self._tc_alignment:
            return False
        try:
            playback_key, playback_sources = self._playback_signature()
        except Exception:
            return False
        lower, upper = self.timeline_bar.values()
        frame = max(lower, min(int(frame), upper - 1))
        self._live_playback_revision += 1
        revision = self._live_playback_revision
        self._live_playing = playing
        self._live_direction = -1 if direction < 0 else 1
        if self._live_playback_future is not None:
            self._live_playback_pending = (
                frame,
                message,
                playing,
                self._live_direction,
                draft,
            )
            return True
        old_session: LivePlaybackSession | None = None
        try:
            if (
                self._live_playback_session is None
                or self._live_playback_key != playback_key
            ):
                candidate = self._create_live_playback_session(
                    playback_key,
                    playback_sources,
                )
                current_session = self._live_playback_session
                if current_session is not None and current_session.can_reconfigure(
                    candidate.sources,
                    candidate.config,
                    candidate.plan,
                ):
                    current_session.reconfigure(candidate.config)
                    candidate.close()
                    self._live_playback_session = current_session
                else:
                    old_session = current_session
                    self._live_playback_session = candidate
                self._live_playback_key = playback_key
            session = self._live_playback_session
            assert session is not None
        except Exception as error:
            self._append_log(f"LIVE PLAYBACK: {error}")
            return False
        self._live_close_pending = False
        started = time.monotonic()

        def render_live_frame() -> object:
            if old_session is not None:
                old_session.close()
            bundle, image = session.render_frame(frame, draft=draft)
            return (
                bundle.timeline_frame,
                image,
                message,
                time.monotonic() - started,
                draft,
            )

        future = self._live_playback_executor.submit(render_live_frame)
        self._live_playback_future = future
        activity = (
            "reusing stitched memory frame"
            if session.has_rendered_frame(frame, draft=draft)
            else "applying settings to memory frame"
            if session.has_cached_frame(frame)
            else "decoding frame in background"
        )
        self.preview_note.setText(f"{message} · {activity} {frame}…")

        def complete(result: Future[object]) -> None:
            try:
                payload = result.result()
                error = ""
            except Exception as caught:
                payload = None
                error = str(caught)
            if self._closing:
                session.close()
                return
            try:
                self._live_playback_signals.finished.emit(revision, payload, error)
            except RuntimeError:
                pass

        future.add_done_callback(complete)
        return True

    def _live_proxy_frame_finished(
        self,
        revision: int,
        payload: object,
        error: str,
    ) -> None:
        self._live_playback_future = None
        current = revision == self._live_playback_revision and not self._closing
        valid_payload = (
            not error
            and isinstance(payload, tuple)
            and len(payload) in {4, 5}
        )
        if valid_payload:
            frame, image, message, elapsed = payload[:4]
            draft = bool(payload[4]) if len(payload) == 5 else False
            progressive_move = (
                draft
                and self._plate_move_mode
                and not self._closing
                and int(frame) == self.timeline_playhead.value()
            )
        else:
            frame = image = message = elapsed = None
            draft = False
            progressive_move = False
        if (current or progressive_move) and valid_payload:
            if isinstance(image, np.ndarray):
                self.preview.set_array(image)
                self.preview_stack.setCurrentWidget(self.preview)
                if current:
                    self._set_playhead(int(frame))
                self._preview_ready = True
                pixmap = self.preview.current_pixmap()
                if pixmap is not None and self._fullscreen_live_label is not None:
                    self._fullscreen_live_label.set_source(pixmap)
                active_renderer = (
                    self._live_playback_session.draft_renderer
                    if draft and self._live_playback_session is not None
                    else self._live_playback_session.renderer
                    if self._live_playback_session is not None
                    else None
                )
                backend = (
                    "GPU/OpenCL"
                    if active_renderer is not None
                    and active_renderer.hardware_accelerated
                    else "CPU fallback"
                )
                live_size = (
                    f"{self._live_playback_session.config.output.width}×"
                    f"{self._live_playback_session.config.output.height}"
                    if self._live_playback_session is not None
                    else "adaptive"
                )
                self.preview_note.setText(
                    f"{message} · "
                    f"{'MOVE DRAFT' if draft else 'LIVE DRAFT'} {live_size} · {backend}"
                )
                if current and self._live_playing:
                    lower, upper = self.timeline_bar.values()
                    next_frame = int(frame) + self._live_direction
                    if lower <= next_frame < upper:
                        fps = float(self._tc_alignment["fps"])
                        delay = max(
                            0,
                            int(round(1000.0 / max(fps, 1.0) - float(elapsed) * 1000)),
                        )
                        QTimer.singleShot(
                            delay,
                            lambda: self._request_live_proxy_frame(
                                next_frame,
                                "Live playback",
                                playing=True,
                                direction=self._live_direction,
                            ),
                        )
                    else:
                        self._live_playing = False
                        self.playback_button.setText("▶  PLAY")
        elif current and error:
            self._live_playing = False
            self.playback_button.setText("▶  PLAY")
            self.preview_note.setText(f"Live playback unavailable · {error}")
            self._append_log(f"LIVE PLAYBACK: {error}")

        pending = self._live_playback_pending
        self._live_playback_pending = None
        if pending is not None and not self._closing:
            self._request_live_proxy_frame(
                pending[0],
                pending[1],
                playing=pending[2],
                direction=pending[3],
                draft=pending[4],
            )
        elif self._live_close_pending:
            self._close_live_playback_session()

    def _close_live_playback_session(self) -> None:
        session, self._live_playback_session = self._live_playback_session, None
        self._live_playback_key = None
        self._live_close_pending = False
        if session is not None:
            try:
                self._live_playback_executor.submit(session.close)
            except RuntimeError:
                session.close()

    def _stop_live_proxy_playback(self, *, close_session: bool = False) -> None:
        self._live_playing = False
        self._live_playback_pending = None
        self._live_playback_revision += 1
        if close_session:
            if self._live_playback_future is None:
                self._close_live_playback_session()
            else:
                self._live_close_pending = True

    def _seek_loaded_playback(self, frame: int, *, show_video: bool = True) -> bool:
        if self._playback_path is None or not self._playback_path.is_file():
            return False
        try:
            current_key, _ = self._playback_signature()
        except (OSError, ValueError):
            return False
        if current_key != self._playback_key:
            return False
        fps = float(self._tc_alignment["fps"]) if self._tc_alignment else self.fps.value()
        self.media_player.setPosition(max(0, int(round(frame * 1000 / fps))))
        if show_video:
            self.preview_stack.setCurrentWidget(self.video_preview)
        return True

    def _playback_position_changed(self, position_ms: int) -> None:
        if self._playback_path is None or not self._tc_alignment:
            return
        lower, upper = self.timeline_bar.values()
        frame = int(round(position_ms * float(self._tc_alignment["fps"]) / 1000))
        if frame >= upper:
            if (
                self.media_player.playbackState()
                == QMediaPlayer.PlaybackState.PlayingState
            ):
                self.media_player.pause()
            self._set_playhead(upper - 1)
            return
        self._set_playhead(max(lower, frame))

    def _playback_state_changed(self, state: QMediaPlayer.PlaybackState) -> None:
        playing = state == QMediaPlayer.PlaybackState.PlayingState
        self.playback_button.setText("Ⅱ  PAUSE" if playing else "▶  PLAY")
        if playing:
            self.preview_stack.setCurrentWidget(self.video_preview)
            self.preview_note.setText("Playback proxy · Space to pause")

    def _playback_error(self, _error, message: str) -> None:  # type: ignore[no-untyped-def]
        if message:
            self._append_log(f"PLAYBACK ERROR: {message}")
            self.preview_note.setText("Playback failed · rebuild the proxy or open Jobs")

    def _stop_playback(
        self,
        *,
        clear: bool = False,
        preserve_image: bool = False,
    ) -> None:
        if not hasattr(self, "media_player"):
            return
        preserved_frame = (
            self.timeline_playhead.value()
            if hasattr(self, "timeline_playhead") and self._tc_alignment
            else None
        )
        self._stop_live_proxy_playback(close_session=clear)
        self._reverse_timer.stop()
        can_block = hasattr(self.media_player, "blockSignals")
        if can_block:
            self.media_player.blockSignals(True)
        if preserve_image:
            self.media_player.pause()
        else:
            self.media_player.stop()
            self.preview_stack.setCurrentWidget(self.preview)
        if clear:
            if not preserve_image:
                self.media_player.setSource(QUrl())
            self._playback_path = None
            self._playback_key = None
            self._latest_playback_frame = None
        if can_block:
            self.media_player.blockSignals(False)
        if preserved_frame is not None:
            self._set_playhead(preserved_frame)

    def toggle_playback(self) -> None:
        self._reverse_timer.stop()
        if self._live_playing:
            self._stop_live_proxy_playback()
            self.playback_button.setText("▶  PLAY")
            if self._auto_cache_requested:
                self._request_playback_warmup(delay_ms=0)
            return
        if self.media_player.playbackState() == QMediaPlayer.PlaybackState.PlayingState:
            self.media_player.pause()
            return
        if self._playback_path is not None and self._playback_path.is_file():
            try:
                current_key, _ = self._playback_signature()
            except (OSError, ValueError):
                current_key = None
            if current_key == self._playback_key:
                if self._tc_alignment:
                    lower, upper = self.timeline_bar.values()
                    fps = float(self._tc_alignment["fps"])
                    current_frame = int(
                        round(self.media_player.position() * fps / 1000)
                    )
                    at_file_end = (
                        self.media_player.duration() > 0
                        and self.media_player.position()
                        >= self.media_player.duration() - 50
                    )
                    if current_frame < lower or current_frame >= upper or at_file_end:
                        self.media_player.setPosition(
                            max(0, int(round(lower * 1000 / fps)))
                        )
                        self._set_playhead(lower)
                elif (
                    self.media_player.duration() > 0
                    and self.media_player.position()
                    >= self.media_player.duration() - 50
                ):
                    self.media_player.setPosition(0)
                self.preview_stack.setCurrentWidget(self.video_preview)
                self.media_player.play()
                return
        if self._request_live_proxy_frame(
            self.timeline_playhead.value(),
            "Live playback",
            playing=True,
            direction=1,
        ):
            self.playback_button.setText("Ⅱ  PAUSE")
            return
        if not self._tc_alignment:
            self._error("Playback", "Run TC ALIGN before building synchronized playback")
            return
        if self.process is not None:
            if self._auto_cache_in_progress:
                self._playback_autostart = True
                message = "Playback proxy is building · it will play when ready"
            else:
                self._pending_playback_request = True
                self._auto_cache_requested = True
                message = "Playback queued · it will start after the current frame finishes"
            self.preview_note.setText(message)
            self.statusBar().showMessage(message, 8000)
            return
        try:
            playback_key, sources = self._playback_signature()
        except Exception as error:
            self._error("Playback", str(error))
            return
        playback_dir = self._cache_dir / "playback"
        playback_dir.mkdir(parents=True, exist_ok=True)
        playback_path = playback_dir / f"{playback_key}.mp4"
        if playback_path.is_file() and playback_path.stat().st_size > 0:
            self._load_playback(playback_path, playback_key, autoplay=True)
            return
        self._build_playback(playback_path, playback_key, sources)

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
            self._stop_playback(clear=True)
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
        self._stop_playback(clear=True)
        inputs = payload["inputs"]
        common_frames = int(payload["common_frames"])
        if not isinstance(inputs, list) or common_frames < 1:
            raise ValueError("invalid timecode alignment report")
        self._tc_alignment = payload
        self.source_table.set_timing(inputs)
        self._timeline_maximum = self._effective_common_frames(payload)
        source_fps = _canonical_frame_rate(float(payload["fps"]))
        metadata = self.config_data.setdefault("_vpstitch", {})
        if isinstance(metadata, dict):
            metadata["source_fps"] = source_fps
        if self.fps_mode.currentData() == FPS_MODE_MATCH_SOURCE:
            self.fps.setValue(source_fps)
            video = self.config_data.setdefault("video", {})
            if isinstance(video, dict):
                video["fps"] = source_fps
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
        self._save_active_timeline()

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
                self._save_active_timeline()
                self.preview_note.setText(
                    "TC aligned · prewarming playback proxy in the background…"
                )
                self._request_playback_warmup()
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
            self._reload_ocio_spaces()

    def _reload_ocio_spaces(self, *_args, quiet: bool = False) -> bool:
        identifier = self.ocio_config.text().strip()
        if not identifier:
            self.ocio_space_status.setText("OCIO config is required")
            return False
        current = (
            str(self.input_space.property("ocioRequested") or self.input_space.currentText()).strip(),
            str(self.working_space.property("ocioRequested") or self.working_space.currentText()).strip(),
            str(self.output_space.property("ocioRequested") or self.output_space.currentText()).strip(),
        )
        try:
            spaces = ocio_space_names(identifier)
            if not spaces:
                raise ValueError("config contains no named color spaces")
        except Exception as error:
            self.ocio_space_status.setText(f"Could not read OCIO config · {error}")
            if not quiet:
                self.statusBar().showMessage(
                    "OCIO color-space scan failed · check the config path",
                    10000,
                )
            return False
        recoveries: list[tuple[str, str]] = []
        for combo, value in zip(
            (self.input_space, self.working_space, self.output_space),
            current,
            strict=True,
        ):
            resolved = _populate_ocio_combo(combo, spaces, value)
            if value and resolved != value:
                recoveries.append((value, resolved))
        self._loaded_ocio_identifier = identifier
        self._loaded_ocio_spaces = spaces
        if recoveries:
            old, new = recoveries[-1]
            self.ocio_space_status.setText(
                f"{len(spaces)} OCIO spaces · corrected “{old}” → “{new}”"
            )
            notice = tuple(recoveries)
            if notice != getattr(self, "_last_ocio_recovery_notice", None):
                self._append_log(
                    "OCIO selection recovered: "
                    + "; ".join(f"{old} → {new}" for old, new in recoveries)
                )
                self._last_ocio_recovery_notice = notice
        else:
            self.ocio_space_status.setText(f"{len(spaces)} OCIO spaces loaded")
        for combo in (self.input_space, self.working_space, self.output_space):
            combo.setToolTip(f"{len(spaces)} spaces loaded from {identifier}")
        self._reload_ocio_delivery(quiet=quiet)
        return True

    def _reload_ocio_delivery(
        self,
        *,
        display: str | None = None,
        view: str | None = None,
        quiet: bool = True,
    ) -> bool:
        identifier = self.ocio_config.text().strip()
        if not identifier:
            return False
        selected_display = display or _delivery_display_value(self.output_display)
        selected_view = view or self.output_view.currentText().strip()
        try:
            displays = ocio_display_views(identifier)
            if not displays:
                raise ValueError("config contains no display views")
        except Exception as error:
            self.delivery_status.setText(f"Could not read delivery views · {error}")
            if not quiet:
                self.statusBar().showMessage(
                    "OCIO display/view scan failed · check the config", 10000
                )
            return False
        self._delivery_views = displays
        self._loaded_ocio_delivery_identifier = identifier
        _populate_delivery_display_combo(
            self.output_display, tuple(displays), selected_display
        )
        chosen = _delivery_display_value(self.output_display)
        _populate_combo(self.output_view, displays.get(chosen, ()), selected_view)
        self._update_delivery_controls()
        return True

    def _delivery_display_changed(self, *_args) -> None:
        displays = getattr(self, "_delivery_views", {})
        if not displays:
            return
        selected = _delivery_display_value(self.output_display)
        _populate_combo(self.output_view, displays.get(selected, ()), "")
        self._update_delivery_controls()

    def _update_delivery_controls(self, *_args) -> None:
        ocio_enabled = self.color_mode.currentData() == "ocio"
        display_view = self.output_mode.currentData() == "display_view"
        self.output_space.setVisible(not display_view)
        self.output_space_label.setVisible(not display_view)
        self.output_display.setVisible(display_view)
        self.output_display_label.setVisible(display_view)
        self.output_view.setVisible(display_view)
        self.output_view_label.setVisible(display_view)
        for widget in (
            self.output_mode,
            self.output_space,
            self.output_display,
            self.output_view,
            self.viewer_monitor,
        ):
            widget.setEnabled(ocio_enabled)
        if not ocio_enabled:
            self.delivery_status.setText("OCIO mode is required for managed delivery")
        elif display_view:
            display = _delivery_display_value(self.output_display)
            view = self.output_view.currentText().strip()
            tags = _display_view_video_tags(display, view)
            if tags.get("color_trc") in {"smpte2084", "arib-std-b67"}:
                self.delivery_status.setText(
                    "Managed HDR transform is applied after stitching."
                )
            elif display == "Display P3 HDR - Display":
                self.delivery_status.setText(
                    "Apple EDR target · this is not ST2084/PQ delivery."
                )
            else:
                self.delivery_status.setText(
                    "Managed SDR display transform is applied after stitching."
                )
        else:
            self.delivery_status.setText(
                "Scene/log encoding is applied after stitching."
            )

    def _viewer_monitor_changed(self, *_args) -> None:
        if not hasattr(self, "viewer_monitor"):
            return
        key = str(self.viewer_monitor.currentData() or "sdr-rec709")
        self.settings.setValue("viewer/monitor", key)
        self.settings.sync()
        if key == "delivery":
            self.viewer_status.setText(
                "Raw delivery target · HDR/log will look incorrect on this Rec.709 screen."
            )
        else:
            self.viewer_status.setText(
                "Managed Rec.709 viewer · delivery and Render Queue stay unchanged."
            )
        if self._loading_config or not self.config_data:
            return
        self._schedule_live_preview("Viewer monitor updated", immediate=True)

    def _apply_viewer_transform(
        self, raw: dict[str, object]
    ) -> dict[str, object]:
        """Apply the local monitor transform to a preview-only config copy."""
        preview = json.loads(json.dumps(raw))
        key = str(self.viewer_monitor.currentData() or "sdr-rec709")
        color = preview.get("color")
        if (
            key == "delivery"
            or not isinstance(color, dict)
            or color.get("mode") != "ocio"
        ):
            return preview
        _label, display, view = VIEWER_MONITOR_TRANSFORMS.get(
            key, VIEWER_MONITOR_TRANSFORMS["sdr-rec709"]
        )
        displays = ocio_display_views(str(color.get("ocio_config") or ""))
        if display not in displays or view not in displays.get(display, ()):
            raise ValueError(
                f"Viewer transform is unavailable in the OCIO config: {display} / {view}"
            )
        color["output_mode"] = "display_view"
        color["display"] = display
        color["view"] = view
        color.pop("output_space", None)
        video = preview.setdefault("video", {})
        if isinstance(video, dict):
            video.update(
                {
                    "color_primaries": "bt709",
                    "color_trc": "bt709",
                    "colorspace": "bt709",
                    "color_range": "tv",
                }
            )
        return preview

    def _update_color_match_cameras(
        self,
        cameras: list[dict[str, object]],
        color: dict[str, object],
    ) -> None:
        selected = str(color.get("match_reference") or "")
        self.color_match_reference.blockSignals(True)
        self.color_match_reference.clear()
        plate_numbers = self._plate_numbers or list(
            PLATE_NUMBERS_BY_COUNT.get(len(cameras), tuple(range(1, len(cameras) + 1)))
        )
        for index, camera in enumerate(cameras):
            name = str(camera.get("name") or f"cam{index}")
            plate = plate_numbers[index] if index < len(plate_numbers) else index + 1
            self.color_match_reference.addItem(f"P{plate:02d} · {name}", name)
        current = self.color_match_reference.findData(selected)
        self.color_match_reference.setCurrentIndex(max(0, current))
        self.color_match_reference.blockSignals(False)
        enabled = bool(color.get("match_enabled", False))
        self.color_match_enabled.blockSignals(True)
        self.color_match_enabled.setChecked(enabled)
        self.color_match_enabled.blockSignals(False)
        self.color_match_strength.blockSignals(True)
        self.color_match_strength.setValue(
            int(round(float(color.get("match_strength", 1.0)) * 100.0))
        )
        self.color_match_strength.blockSignals(False)
        confidences = {
            str(camera.get("name") or f"cam{index}"): float(
                camera.get("color_match_confidence") or 0.0
            )
            for index, camera in enumerate(cameras)
        }
        if enabled and selected:
            selected_index = self.color_match_reference.findData(selected)
            reference_label = (
                self.color_match_reference.itemText(selected_index).split(" · ", 1)[0]
                if selected_index >= 0
                else selected
            )
            matched = sum(value > 0.0 for value in confidences.values())
            measured = [
                value
                for name, value in confidences.items()
                if name != selected and value > 0.0
            ]
            average = (
                sum(measured) / len(measured)
                if measured
                else 0.0
            )
            self.color_match_status.setText(
                f"{reference_label} reference · {matched}/{len(cameras)} connected · {average:.0%} confidence"
            )
        else:
            self.color_match_status.setText("Create a Quick Preview, then match")

    def _color_match_reference_changed(self, *_args) -> None:
        if self._loading_config:
            return
        self.color_match_enabled.blockSignals(True)
        self.color_match_enabled.setChecked(False)
        self.color_match_enabled.blockSignals(False)
        self.color_match_status.setText("Reference changed · run MATCH")
        self._color_match_setting_changed()

    def _color_match_setting_changed(self, *_args) -> None:
        if self._loading_config or not self.config_data:
            return
        self._schedule_live_preview("Camera match updated")

    def reset_color_match(self) -> None:
        cameras = self.config_data.get("cameras")
        if not isinstance(cameras, list):
            return
        for camera in cameras:
            if isinstance(camera, dict):
                camera["color_gain"] = [1.0, 1.0, 1.0]
                camera.pop("color_match_confidence", None)
        self.color_match_enabled.blockSignals(True)
        self.color_match_enabled.setChecked(False)
        self.color_match_enabled.blockSignals(False)
        self.color_match_status.setText("Camera match reset")
        self._stop_playback(clear=True)
        self._save_active_timeline()
        self._restitch_color_reference("Camera match reset")

    def match_cameras(self) -> None:
        if self.color_mode.currentData() != "ocio":
            self._error("Camera Match", "Camera matching requires OCIO mode")
            return
        if self._last_reference_dir is None or self._last_reference_config_path is None:
            self._error("Camera Match", "Create a Quick Preview first")
            return
        try:
            raw = json.loads(
                self._last_reference_config_path.read_text(encoding="utf-8")
            )
            images = [
                str(self._last_reference_dir / f"{camera['name']}.png")
                for camera in raw["cameras"]
            ]
            if any(not Path(path).is_file() for path in images):
                raise ValueError("Reference images are missing; create Quick Preview again")
            reference = str(self.color_match_reference.currentData() or "")
            if not reference:
                raise ValueError("Choose a reference camera")
        except Exception as error:
            self._error("Camera Match", str(error))
            return
        report = self._last_reference_dir / "color-match.json"
        self.color_match_status.setText("Analyzing scene-linear overlaps …")

        def apply_match() -> None:
            try:
                payload = json.loads(report.read_text(encoding="utf-8"))
                solved = {
                    str(item["name"]): item
                    for item in payload.get("cameras", [])
                    if isinstance(item, dict)
                }
                cameras = self.config_data.get("cameras")
                if not isinstance(cameras, list) or len(solved) != len(cameras):
                    raise ValueError("Color-match report does not match this rig")
                color = self.config_data.setdefault("color", {})
                if not isinstance(color, dict):
                    raise ValueError("Color settings are invalid")
                match_space = str(payload.get("match_space", "")).strip()
                if not match_space:
                    raise ValueError("Color-match report is missing its match space")
                color["match_space"] = match_space
                connected = 0
                confidences: list[float] = []
                for camera in cameras:
                    if not isinstance(camera, dict):
                        continue
                    item = solved[str(camera.get("name"))]
                    gain = [float(value) for value in item["gain"]]
                    if len(gain) != 3:
                        raise ValueError("Color-match gain must contain RGB values")
                    confidence = float(item.get("confidence", 0.0))
                    camera["color_gain"] = gain
                    camera["color_match_confidence"] = confidence
                    if bool(item.get("connected", False)):
                        connected += 1
                        if str(camera.get("name")) != reference:
                            confidences.append(confidence)
                self.color_match_enabled.blockSignals(True)
                self.color_match_enabled.setChecked(True)
                self.color_match_enabled.blockSignals(False)
                average = sum(confidences) / len(confidences) if confidences else 0.0
                reference_label = self.color_match_reference.currentText().split(
                    " · ", 1
                )[0]
                self.color_match_status.setText(
                    f"{reference_label} reference · {connected}/{len(cameras)} connected · {average:.0%} confidence"
                )
                self._stop_playback(clear=True)
                self._save_active_timeline()
                self._restitch_color_reference("Camera match applied")
            except Exception as error:
                self._error("Camera Match", str(error))

        def match_failed() -> None:
            self.color_match_status.setText("Camera match failed · see Task Log")

        self._run_cli(
            "MATCH CAMERA COLOR",
            [
                "match-color",
                "--config",
                str(self._last_reference_config_path),
                "--reference-camera",
                reference,
                "--output",
                str(report),
                *images,
            ],
            apply_match,
            match_failed,
        )

    def _restitch_color_reference(self, message: str) -> None:
        self._schedule_live_preview(message, immediate=True)

    def _schedule_live_preview(
        self,
        message: str,
        *,
        immediate: bool = False,
    ) -> None:
        """Debounce inspector changes and update the in-memory draft frame."""
        if self._loading_config or self._closing or not self.config_data:
            return
        if (
            self.process is not None
            and self._process_task_name == "BUILD PLAYBACK PROXY"
        ):
            self._playback_cache_cancelled_for_interaction = True
            self._auto_cache_requested = False
            self._pending_playback_request = False
            self._playback_autostart = False
            self.process.kill()
        # Entering move mode already pauses playback. Repeating this operation
        # on every key repeat needlessly tears down viewer state and makes the
        # UI feel blocked while the in-memory renderer is working.
        if not self._plate_move_mode:
            self._stop_playback(preserve_image=True)
        self._playback_key = None
        self._live_preview_revision += 1
        self._live_preview_message = message
        self._live_preview_pending = True
        if self._last_reference_dir is None or self._last_reference_config_path is None:
            self.preview_note.setText(f"{message} · create Quick Preview once")
        else:
            self.preview_note.setText(f"{message} · updating draft…")
        self._live_preview_timer.start(0 if immediate else 40)
        if self._tc_alignment and not self._plate_move_mode:
            # Rebuild only after the controls have been idle for a moment. This
            # keeps slider/drag feedback interactive while ensuring Space does
            # not remain on the slower frame-by-frame fallback indefinitely.
            self._request_playback_warmup(delay_ms=700)

    def _sync_cached_preview_config(self) -> tuple[list[str], Path]:
        reference = self._last_reference_dir
        preview_config = self._last_reference_config_path
        if reference is None or preview_config is None or not preview_config.is_file():
            raise ValueError("Create Quick Preview once to enable live updates")

        full = self._collect_config()
        preview_raw = json.loads(preview_config.read_text(encoding="utf-8"))
        full_output = full["output"]
        preview_output = preview_raw["output"]
        width, height = preview_dimensions(
            int(full_output["width"]),
            int(full_output["height"]),
            max_width=2048,
            max_height=1152,
        )

        full_cameras = {
            str(camera["name"]): camera for camera in full["cameras"]
        }
        preview_cameras = preview_raw["cameras"]
        if set(full_cameras) != {
            str(camera["name"]) for camera in preview_cameras
        }:
            raise ValueError("Cached preview does not match the active camera set")

        camera_scale = min(
            min(
                float(camera["width"])
                / max(1.0, float(full_cameras[str(camera["name"])]["width"])),
                float(camera["height"])
                / max(1.0, float(full_cameras[str(camera["name"])]["height"])),
            )
            for camera in preview_cameras
        )
        for camera in preview_cameras:
            scaled_width = camera["width"]
            scaled_height = camera["height"]
            scaled_lens = camera["lens"]
            source = json.loads(json.dumps(full_cameras[str(camera["name"])]))
            source_lens = source.get("lens")
            if isinstance(scaled_lens, dict) and isinstance(source_lens, dict):
                scaled_lens["distortion"] = json.loads(
                    json.dumps(source_lens.get("distortion", [0.0] * 4))
                )
            camera.clear()
            camera.update(source)
            camera["width"] = scaled_width
            camera["height"] = scaled_height
            camera["lens"] = scaled_lens

        preview_output.update(json.loads(json.dumps(full_output)))
        preview_output["width"] = width
        preview_output["height"] = height
        preview_output["tile_width"] = min(
            int(preview_output.get("tile_width", 1024)), width
        )
        preview_output["tile_height"] = min(
            int(preview_output.get("tile_height", 512)), height
        )
        viewer_full = self._apply_viewer_transform(full)
        preview_raw["color"] = json.loads(json.dumps(viewer_full["color"]))
        if isinstance(viewer_full.get("video"), dict):
            preview_raw["video"] = json.loads(json.dumps(viewer_full["video"]))
        if "flow" in full:
            preview_raw["flow"] = json.loads(json.dumps(full["flow"]))
            flow = preview_raw["flow"]
            if isinstance(flow, dict) and flow.get("max_displacement_px") is not None:
                flow["max_displacement_px"] = max(
                    1.0,
                    float(flow["max_displacement_px"]) * camera_scale,
                )

        preview_config.write_text(
            json.dumps(preview_raw, indent=2), encoding="utf-8"
        )
        images = [
            str(reference / f"{camera['name']}.png")
            for camera in preview_cameras
        ]
        if any(not Path(path).is_file() for path in images):
            raise ValueError("Cached reference images are missing; create Quick Preview again")
        return images, reference / "stitched-preview.png"

    def _run_live_preview(self) -> None:
        if not self._live_preview_pending or self._closing:
            return
        if self.process is not None:
            return
        self._live_preview_pending = False
        revision = self._live_preview_revision
        message = self._live_preview_message
        # The mutable config is authoritative during movement. Persist once
        # movement ends, rather than serializing the project on every nudge.
        if not self._plate_move_mode:
            self._save_active_timeline()
        current_frame = self.timeline_playhead.value() if self._tc_alignment else 0
        if self._tc_alignment and self._request_live_proxy_frame(
            current_frame,
            message,
            draft=self._plate_move_mode,
        ):
            return
        if self._tc_alignment and self._reference_frame_index != current_frame:
            self.preview_note.setText(
                f"{message} · loading synchronized frame {current_frame}…"
            )
            self.create_preview(preserve_view=True)
            return
        if self._last_reference_dir is None or self._last_reference_config_path is None:
            self.preview_note.setText(f"{message} · create Quick Preview once")
            return
        try:
            images, _ = self._sync_cached_preview_config()
            preview_config = self._last_reference_config_path
            assert preview_config is not None
            config = parse_config(preview_config)
        except Exception as error:
            self.preview_note.setText(f"Live preview unavailable · {error}")
            return
        self._interactive_request = (revision, config, images, message)
        self._start_interactive_preview()

    def _start_interactive_preview(self) -> None:
        if self._closing or self._interactive_request is None:
            return
        if self._interactive_future is not None and not self._interactive_future.done():
            return
        revision, config, images, message = self._interactive_request
        self._interactive_request = None
        future = self._interactive_executor.submit(
            self._interactive_renderer.render,
            config,
            images,
        )
        self._interactive_future = future

        def complete(result: Future[np.ndarray]) -> None:
            try:
                image = result.result()
                error = ""
            except Exception as caught:
                image = None
                error = str(caught)
            try:
                self._interactive_signals.finished.emit(
                    revision,
                    (image, message),
                    error,
                )
            except RuntimeError:
                pass

        future.add_done_callback(complete)

    def _interactive_preview_finished(
        self,
        revision: int,
        payload: object,
        error: str,
    ) -> None:
        self._interactive_future = None
        image, message = payload if isinstance(payload, tuple) else (None, "Preview")
        if not self._closing and revision == self._live_preview_revision:
            if error or not isinstance(image, np.ndarray):
                self.preview_note.setText(
                    f"Live preview unavailable · {error or 'invalid image'}"
                )
            else:
                self.preview.set_array(image)
                self.preview_stack.setCurrentWidget(self.preview)
                self._preview_ready = True
                self.rig_align_button.setEnabled(True)
                self._update_color_controls()
                backend = (
                    "GPU/OpenCL"
                    if self._interactive_renderer.hardware_accelerated
                    else "CPU fallback"
                )
                self.preview_note.setText(
                    f"{message} · INTERACTIVE PREVIEW · {backend}"
                )
                self.statusBar().showMessage(
                    f"Interactive preview updated · {backend} · render remains full quality",
                    1800,
                )
        if self._interactive_request is not None:
            self._start_interactive_preview()

    def apply_aces_preset(self) -> None:
        self.color_mode.setCurrentIndex(self.color_mode.findData("ocio"))
        self.ocio_config.setText(BUILTIN_ACES_STUDIO)
        _request_ocio_combo_value(self.input_space, "Camera Rec.709")
        _request_ocio_combo_value(self.working_space, "ACEScg")
        _request_ocio_combo_value(
            self.output_space, "Gamma 2.4 Encoded Rec.709"
        )
        self.output_mode.setCurrentIndex(self.output_mode.findData("colorspace"))
        self._reload_ocio_spaces(quiet=True)
        self._update_color_controls()
        self._append_log("Applied bundled ACES 2.0 Studio preset: Camera Rec.709 → ACEScg → Gamma 2.4 Rec.709")

    def choose_output(self) -> None:
        path = QFileDialog.getExistingDirectory(
            self,
            "Select render folder",
            self.output_directory.text() or str(self._output_root),
        )
        if path:
            self.output_directory.setText(path)
            self._last_auto_output = None

    def _validate_sources(self) -> list[str]:
        paths = self.source_table.paths()
        if len(paths) not in SUPPORTED_CAMERA_COUNTS or any(not path for path in paths):
            raise ValueError("Select all 3 or all 5 camera plates")
        missing = [path for path in paths if not Path(path).is_file()]
        if missing:
            raise ValueError("Missing video: " + missing[0])
        return paths

    def _source_frame_rate(self, sources: list[str]) -> float:
        probes = self._source_probes
        cached_paths = [str(probe.get("path") or "") for probe in probes or []]
        if not probes or cached_paths != sources or any(probe.get("fps") is None for probe in probes):
            probes = [probe_video(source).to_dict() for source in sources]
        fps = _matching_source_frame_rate(probes)
        if fps is None:
            raise ValueError("Could not detect plate frame rate")
        return fps

    def _lock_render_frame_rate(
        self, config: dict[str, object], sources: list[str]
    ) -> float:
        source_fps = self._source_frame_rate(sources)
        metadata = config.setdefault("_vpstitch", {})
        if not isinstance(metadata, dict):
            metadata = {}
            config["_vpstitch"] = metadata
        mode = _frame_rate_mode(config)
        metadata.update(
            {
                "fps_mode": mode,
                "source_fps": source_fps,
                "timeline_id": self._active_timeline_id,
                "timeline_name": (
                    self._active_timeline_record().name
                    if self._active_timeline_record() is not None
                    else None
                ),
            }
        )
        video = config.setdefault("video", {})
        if not isinstance(video, dict):
            raise ValueError("Render config video settings are invalid")
        if mode == FPS_MODE_MATCH_SOURCE:
            video["fps"] = source_fps
        else:
            output_fps = _canonical_frame_rate(float(video.get("fps") or 0.0))
            video["fps"] = output_fps
        return float(video["fps"])

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
        try:
            source_fps = _matching_source_frame_rate(probes)
            self._source_fps_error = None
        except ValueError as error:
            self._source_fps_error = str(error)
            self.preview_note.setText(f"FPS MISMATCH · {error}")
            self._save_active_timeline()
            return
        if source_fps is not None and self.fps_mode.currentData() == FPS_MODE_MATCH_SOURCE:
            self.fps.setValue(source_fps)
            video = self.config_data.setdefault("video", {})
            if isinstance(video, dict):
                video["fps"] = source_fps
            metadata = self.config_data.setdefault("_vpstitch", {})
            if isinstance(metadata, dict):
                metadata.update(
                    {"fps_mode": FPS_MODE_MATCH_SOURCE, "source_fps": source_fps}
                )
        minimum = min(int(probe["bit_depth"]) for probe in probes)
        if minimum < 10:
            self.preview_note.setText(
                f"SOURCE {minimum}-bit · preview allowed · master encoded at 10/12-bit"
            )
        else:
            self.preview_note.setText(
                f"SOURCE {minimum}-bit detected · 10/12-bit master pipeline ready"
            )
        self._save_active_timeline()
        if (
            self._auto_workflows_enabled
            and self._active_timeline_id
            and self._tc_alignment is None
            and self._source_fps_error is None
            and self.process is None
        ):
            self.preview_note.setText("Source analyzed · automatic TC align starting…")
            QTimer.singleShot(0, self.align_timecode)

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
        decode_interpretation_changed = any(
            override.get("input_video_range") != values.get("input_video_range")
            for override in overrides
        )
        for path in selected_paths:
            self._source_overrides[path] = dict(values)
        if self._source_probes:
            self.source_table.set_probe_data(
                self._source_probes, self._source_overrides
            )
        self._update_source_status()
        if decode_interpretation_changed:
            self._cleanup_reference_dir(self._last_reference_dir)
            self._last_reference_dir = None
            self._last_reference_config_path = None
            self._reference_frame_index = None
            self._preview_ready = False
            self._preview_in_progress = False
            self._pending_scrub_frame = None
            self.rig_align_button.setEnabled(False)
            self.preview_note.setText(
                "Input video range updated · create preview to decode the frame again"
            )
            self._save_active_timeline()
        else:
            self._schedule_live_preview(
                "Plate input color space updated",
                immediate=True,
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
        *,
        scale_cameras: bool = True,
        viewer_transform: bool = False,
    ) -> float:
        raw = json.loads(source.read_text(encoding="utf-8"))
        if viewer_transform:
            raw = self._apply_viewer_transform(raw)
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
        if scale_cameras:
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

    def _build_playback(
        self,
        playback_path: Path,
        playback_key: str,
        sources: list[str],
        *,
        autoplay: bool = True,
    ) -> None:
        render_sources = list(sources)
        using_source_proxies = False
        try:
            full_config = self._write_working_config()
            config_dir = self._working_dir / "playback"
            config_dir.mkdir(parents=True, exist_ok=True)
            playback_config = config_dir / f"{playback_key}.json"
            width, height = preview_dimensions(
                self.canvas_width.value(),
                self.canvas_height.value(),
                max_width=1920,
                max_height=1080,
            )
            decode_scale = self._write_preview_config(
                full_config,
                playback_config,
                width,
                height,
                scale_cameras=False,
                viewer_transform=True,
            )
            raw = json.loads(playback_config.read_text(encoding="utf-8"))
            proxy_records = self._cached_proxy_records(render_sources)
            if proxy_records is not None:
                try:
                    proxy_probes = [probe_video(source) for source in render_sources]
                    proxy_dimensions = [
                        (probe.width, probe.height) for probe in proxy_probes
                    ]
                    self._match_camera_geometry_to_proxy_dimensions(
                        raw, proxy_dimensions
                    )
                    decode_scale = 1.0
                    using_source_proxies = True
                except (OSError, ValueError) as error:
                    # A stale or unreadable low-res cache must not break playback.
                    # Fall back to the authoritative originals and keep the normal
                    # decode scale/configuration written above.
                    render_sources = [str(record.path) for record in proxy_records]
                    self._append_log(f"SOURCE PROXY READ FALLBACK: {error}")
            cache_frames = (
                self._timeline_maximum
                if self._tc_alignment and self._timeline_maximum > 0
                else int(raw.get("video", {}).get("frames") or 1)
            )
            raw.setdefault("video", {})["frames"] = cache_frames
            raw["video"]["output_codec"] = "h264-proxy"
            raw.setdefault("flow", {})["enabled"] = False
            playback_config.write_text(json.dumps(raw, indent=2), encoding="utf-8")
        except Exception as error:
            self._error("Playback setup", str(error))
            return

        arguments = [
            "stitch-video",
            "--allow-low-bit-depth",
            "--config",
            str(playback_config),
            "--output",
            str(playback_path),
            "--map-cache",
            str(self._cache_dir / "playback-maps"),
            "--start-frame",
            "0",
            "--decode-scale",
            f"{decode_scale:.9f}",
        ]
        if self._tc_alignment_path:
            try:
                alignment_path = self._playback_alignment_plan(
                    render_sources,
                    playback_key,
                )
            except (OSError, ValueError) as error:
                self._error("Playback alignment", str(error))
                return
            arguments.extend(["--alignment-plan", str(alignment_path)])
        arguments.extend(render_sources)
        self.preview_note.setText(
            f"Building playback proxy {width}×{height}"
            + (" from source cache" if using_source_proxies else " from originals")
            + " · geometry is reused for every frame"
        )
        self._playback_autostart = autoplay
        self._auto_cache_in_progress = True

        def playback_failed() -> None:
            self._playback_autostart = False
            self._auto_cache_in_progress = False
            try:
                playback_path.unlink(missing_ok=True)
            except OSError:
                pass
            if self._playback_cache_cancelled_for_interaction:
                self._playback_cache_cancelled_for_interaction = False
                self.preview_note.setText(
                    "Playback cache paused · Inspector preview stays interactive"
                )
                if self._active_timeline_id:
                    try:
                        self.project_store.update_timeline(
                            self._active_timeline_id,
                            playback_cache_status=PlaybackCacheStatus.PENDING,
                        )
                    except ProjectError:
                        pass
                return
            self.preview_note.setText("Playback proxy failed · open Jobs for details")
            if self._active_timeline_id:
                try:
                    self.project_store.update_timeline(
                        self._active_timeline_id,
                        playback_cache_status=PlaybackCacheStatus.FAILED,
                    )
                except ProjectError:
                    pass

        def playback_ready() -> None:
            self._auto_cache_in_progress = False
            try:
                current_key, _ = self._playback_signature()
            except Exception:
                current_key = None
            if current_key != playback_key:
                playback_path.unlink(missing_ok=True)
                self._request_playback_warmup(autoplay=self._playback_autostart)
                return
            self._load_playback(
                playback_path,
                playback_key,
                autoplay=self._playback_autostart,
            )
            self._save_active_timeline()

        self._run_cli(
            "BUILD PLAYBACK PROXY",
            arguments,
            playback_ready,
            playback_failed,
        )

    def _playback_alignment_plan(
        self,
        playback_sources: list[str],
        playback_key: str,
    ) -> Path:
        if self._tc_alignment_path is None:
            raise ValueError("TC alignment is not available")
        payload = json.loads(self._tc_alignment_path.read_text(encoding="utf-8"))
        inputs = payload.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != len(playback_sources):
            raise ValueError("TC alignment input count does not match playback sources")
        for item, source in zip(inputs, playback_sources, strict=True):
            if not isinstance(item, dict):
                raise ValueError("TC alignment input entry is invalid")
            item["path"] = str(Path(source).resolve())
        probes = payload.get("probes")
        if isinstance(probes, list) and len(probes) == len(playback_sources):
            for item, source in zip(probes, playback_sources, strict=True):
                if isinstance(item, dict):
                    item["path"] = str(Path(source).resolve())
        directory = self._working_dir / "playback"
        directory.mkdir(parents=True, exist_ok=True)
        destination = directory / f"{playback_key}-alignment.json"
        destination.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return destination

    def _request_playback_warmup(
        self,
        *,
        autoplay: bool = False,
        delay_ms: int = 0,
    ) -> None:
        """Coalesce a low-resolution proxy build after interactive work finishes."""
        if self._closing or not self._tc_alignment:
            return
        self._auto_cache_requested = True
        if autoplay:
            self._pending_playback_request = True
        if self.process is None:
            self._playback_warmup_timer.start(max(0, int(delay_ms)))

    def _warm_playback_cache(self) -> None:
        self._playback_warmup_timer.stop()
        if self._closing:
            self._auto_cache_requested = False
            self._pending_playback_request = False
            return
        if not self._tc_alignment or self._loading_timeline:
            self._auto_cache_requested = False
            self._pending_playback_request = False
            return
        if self.process is not None:
            self._auto_cache_requested = True
            return
        if (
            self._live_preview_pending
            or (
                self._interactive_future is not None
                and not self._interactive_future.done()
            )
            or self._live_playback_future is not None
            or self._live_playing
        ):
            self._auto_cache_requested = True
            self._playback_warmup_timer.start(800 if self._live_playing else 220)
            return
        autoplay = self._pending_playback_request
        try:
            playback_key, sources = self._playback_signature()
        except Exception as error:
            self._auto_cache_requested = False
            self._pending_playback_request = False
            self._append_log(f"AUTO CACHE: {error}")
            return
        playback_dir = self._cache_dir / "playback"
        playback_dir.mkdir(parents=True, exist_ok=True)
        playback_path = playback_dir / f"{playback_key}.mp4"
        self._auto_cache_requested = False
        self._pending_playback_request = False
        if playback_path.is_file() and playback_path.stat().st_size > 0:
            self._load_playback(playback_path, playback_key, autoplay=autoplay)
            self._save_active_timeline()
            return
        if self._active_timeline_id:
            try:
                self.project_store.update_timeline(
                    self._active_timeline_id,
                    playback_cache_status=PlaybackCacheStatus.BUILDING,
                )
            except ProjectError:
                pass
        self._refresh_media_tree()
        self._build_playback(
            playback_path, playback_key, sources, autoplay=autoplay
        )

    def _load_playback(
        self,
        path: Path,
        playback_key: str,
        *,
        autoplay: bool,
    ) -> None:
        requested_frame = self.timeline_playhead.value()
        self._playback_autostart = False
        self._playback_path = path
        self._playback_key = playback_key
        self.media_player.setSource(QUrl.fromLocalFile(str(path)))
        # QMediaPlayer emits positionChanged(0) while replacing its source.
        # Keep the user's paused timeline frame authoritative, then seek the
        # freshly loaded cache back to that exact frame.
        if self._tc_alignment:
            self._set_playhead(requested_frame)
        self._seek_loaded_playback(requested_frame, show_video=autoplay)
        if not autoplay:
            self.preview_stack.setCurrentWidget(self.preview)
        self.preview_note.setText("Playback cached · Space to play / pause")
        self._save_active_timeline()
        if autoplay:
            self.media_player.play()

    def create_preview(self, *, preserve_view: bool = False) -> None:
        requested_frame = self.timeline_playhead.value() if self._tc_alignment else 0
        self._stop_playback(preserve_image=preserve_view)
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
                self.canvas_width.value(),
                self.canvas_height.value(),
                max_width=2048,
                max_height=1152,
            )
            preview_config = reference / "preview-config.json"
            preview_scale = self._write_preview_config(
                config,
                preview_config,
                width,
                height,
                viewer_transform=True,
            )
        except Exception as error:
            self._cleanup_reference_dir(reference)
            self._error("Preview setup", str(error))
            return
        previous_reference = self._last_reference_dir
        previous_reference_frame = self._reference_frame_index
        self._preview_in_progress = True
        self.preview.show_message("LOADING ONE SYNCHRONIZED FRAME …")
        timeline_start = requested_frame
        reference_time = 0.0

        def preview_failed() -> None:
            self._preview_in_progress = False
            self._pending_scrub_frame = None
            self._cleanup_reference_dir(reference)
            self._reference_frame_index = previous_reference_frame
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
                    self.preview_stack.setCurrentWidget(self.preview)
                    self._last_reference_dir = reference
                    self._last_reference_config_path = preview_config
                    self._reference_frame_index = timeline_start
                    self._preview_ready = True
                    self._cleanup_reference_dir(previous_reference)
                    self.rig_align_button.setEnabled(True)
                    self._update_color_controls()
                    self.preview_note.setText(
                        "Quick Preview · one 2K frame · Auto Stitch can refine this alignment"
                    )
                    self.statusBar().showMessage(
                        f"Preview ready: {width}×{height} · full canvas",
                        10000,
                    )
                    self._finish_preview_frame(timeline_start)
                    self._save_active_timeline()
                    self._request_playback_warmup()
                except Exception as error:
                    preview_failed()
                    self._error("Preview load", str(error))

            self.preview.show_message("STITCHING QUICK 2K PREVIEW …")
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
        self._run_cli(
            "EXTRACT REFERENCES",
            arguments,
            stitch_reference,
            preview_failed,
        )

    def auto_align(self) -> None:
        if self._last_reference_dir is None or self._last_reference_config_path is None:
            self._error("Auto align", "Create a preview/reference frame first")
            return
        try:
            config = self._write_working_config()
            preview_config = self._last_reference_config_path
            raw = json.loads(preview_config.read_text(encoding="utf-8"))
            profile = self._rig_profiles.get(len(raw["cameras"]))
            if not isinstance(profile, dict):
                raise ValueError("The original rig profile is unavailable")
            calibration_raw = prepare_auto_stitch_config(raw, profile)
            calibration_config = self._working_dir / "auto-stitch-input.json"
            calibration_config.write_text(
                json.dumps(calibration_raw, indent=2), encoding="utf-8"
            )
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
            try:
                full_raw = json.loads(config.read_text(encoding="utf-8"))
                solved_raw = json.loads(
                    calibration_output.read_text(encoding="utf-8")
                )
                report_raw = json.loads(report.read_text(encoding="utf-8"))
                aligned, validation = apply_auto_stitch_solution(
                    full_raw,
                    solved_raw,
                    profile,
                    report_raw,
                )
                report_raw["validation"] = validation
                report.write_text(json.dumps(report_raw, indent=2), encoding="utf-8")
                output.write_text(json.dumps(aligned, indent=2), encoding="utf-8")
            except Exception as error:
                self._error("Auto Stitch", str(error))
                return
            current_paths = self.source_table.paths()
            plate_numbers = self._plate_numbers
            source_probes = self._source_probes
            source_overrides = self._source_overrides
            tc_alignment = self._tc_alignment
            tc_alignment_path = self._tc_alignment_path
            timeline_range = self.timeline_bar.values()
            self.load_config(output)
            loaded_cameras = self.config_data.get("cameras")
            if isinstance(loaded_cameras, list):
                self._plate_reset_cameras = json.loads(json.dumps(loaded_cameras))
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
            review_count = sum(
                pair["status"] == "review" for pair in validation["pairs"]
            )
            seam_status = (
                f"{review_count} seam(s) need review"
                if review_count
                else "all adjacent seams passed"
            )
            self.statusBar().showMessage(
                f"Auto Stitch applied · manual fine tune reset · {seam_status}",
                15000,
            )
            self.preview_note.setText(
                f"Auto Stitch complete · {seam_status} · refreshing preview…"
            )
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

    def _selected_queue_job(self) -> RenderJob | None:
        row = self.queue_table.currentRow()
        if row < 0:
            return None
        item = self.queue_table.item(row, 0)
        if item is None:
            return None
        job_id = item.data(Qt.ItemDataRole.UserRole)
        return next(
            (job for job in self.render_queue.jobs if job.id == job_id),
            None,
        )

    def _update_queue_action_state(self) -> None:
        job = self._selected_queue_job()
        self.render_selected_queue_button.setEnabled(
            job is not None and job.status is not RenderStatus.RENDERING
        )
        self.render_all_queue_button.setEnabled(
            bool(self.render_queue.jobs) and not self._queue_running
        )

    def _queue_table_menu(self, position) -> None:  # type: ignore[no-untyped-def]
        item = self.queue_table.itemAt(position)
        if item is not None:
            self.queue_table.setCurrentCell(item.row(), 0)
        job = self._selected_queue_job()
        menu = QMenu(self)
        load = menu.addAction("Load Timeline", self.load_selected_queue_job)
        render = menu.addAction("Render Selected", self.render_selected_queue_job)
        menu.addSeparator()
        remove = menu.addAction("Remove from Queue", self.remove_selected_queue_job)
        enabled = job is not None
        load.setEnabled(enabled)
        render.setEnabled(enabled and job.status is not RenderStatus.RENDERING)
        remove.setEnabled(enabled and job.status is not RenderStatus.RENDERING)
        menu.exec(self.queue_table.viewport().mapToGlobal(position))

    def _refresh_queue_table(self) -> None:
        if not hasattr(self, "queue_table"):
            return
        selected = self._selected_queue_job()
        selected_id = selected.id if selected else None
        jobs = self.render_queue.jobs
        self.queue_status.setText(
            f"{len(jobs)} timeline{'s' if len(jobs) != 1 else ''} queued"
            if jobs
            else "No timelines queued"
        )
        self.queue_table.setRowCount(len(jobs))
        selected_row = -1
        for row, job in enumerate(jobs):
            output = job.config_snapshot.get("output", {})
            video = job.config_snapshot.get("video", {})
            metadata = job.config_snapshot.get("_vpstitch", {})
            metadata = metadata if isinstance(metadata, dict) else {}
            width = int(output.get("width", 0))
            height = int(output.get("height", 0))
            frame_range = (
                f"{job.in_frame}–{job.out_frame}"
                if job.out_frame is not None
                else f"{job.in_frame}–END"
            )
            values = (
                job.name,
                _format_frame_rate(video.get("fps")),
                QUEUE_CODEC_LABELS.get(
                    str(video.get("output_codec", "")),
                    str(video.get("output_codec", "Unknown")),
                ),
                render_queue_status_text(
                    job.status,
                    self._queue_progress.get(job.id),
                    elapsed_seconds=(
                        self._render_elapsed_seconds()
                        if job.id == self._queue_current_id
                        else job.elapsed_seconds
                    ),
                    phase=self._process_phase if job.id == self._queue_current_id else "",
                    map_progress=(
                        self._render_map_progress
                        if job.id == self._queue_current_id
                        else None
                    ),
                ),
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(value)
                if column == 0:
                    item.setData(Qt.ItemDataRole.UserRole, job.id)
                    item.setToolTip(
                        f"{job.name}\nRange {frame_range}\nCanvas {width}×{height}"
                        f"\nFPS {_format_frame_rate(video.get('fps'))}"
                        f" · {str(metadata.get('fps_mode') or FPS_MODE_MATCH_SOURCE)}"
                        f"\nOutput {job.output_path}"
                        f"\nSettings lock {job.snapshot_digest}"
                    )
                elif column == 1:
                    source_fps = metadata.get("source_fps")
                    item.setToolTip(
                        f"Output {_format_frame_rate(video.get('fps'))} fps"
                        + (
                            f"\nPlate {_format_frame_rate(source_fps)} fps"
                            if source_fps is not None
                            else ""
                        )
                    )
                elif column == 2:
                    codec = str(video.get("output_codec", ""))
                    bit_depth = GUI_MASTER_BIT_DEPTHS.get(codec)
                    item.setToolTip(
                        f"{codec} · {bit_depth}-bit" if bit_depth else codec
                    )
                if column == 3:
                    colors = {
                        RenderStatus.QUEUED: "#c8c3df",
                        RenderStatus.RENDERING: "#e5c878",
                        RenderStatus.DONE: "#74d89a",
                        RenderStatus.FAILED: "#e37d83",
                    }
                    item.setForeground(QColor(colors[job.status]))
                    if job.error:
                        tooltip = job.error
                    elif job.status is RenderStatus.DONE:
                        tooltip = f"Output: {job.output_path}"
                        if job.elapsed_seconds is not None:
                            tooltip += (
                                "\nCompleted in "
                                + format_render_duration(job.elapsed_seconds)
                            )
                    else:
                        tooltip = (
                            f"Output: {job.output_path}\n"
                            "Completed frames and estimated remaining render time"
                        )
                    item.setToolTip(tooltip)
                self.queue_table.setItem(row, column, item)
            if job.id == selected_id:
                selected_row = row
        if selected_row >= 0:
            self.queue_table.selectRow(selected_row)
        suffix = f" {len(jobs)}" if jobs else ""
        queue_visible = (
            self.inspector_panel.isVisible() and self.right_tabs.currentIndex() == 1
        )
        self.jobs_toggle.setText(
            f"HIDE JOBS{suffix}" if queue_visible else f"JOBS{suffix}"
        )
        self._update_queue_action_state()

    def _timeline_name(self, sources: list[str]) -> str:
        active = self._active_timeline_record()
        if active is not None:
            base = active.name
        else:
            parent = Path(sources[0]).parent.name.strip()
            stem = re.sub(
                r"(?i)^P0?(?:1|6)[._ -]*",
                "",
                Path(sources[0]).stem,
            ).strip(" ._-")
            base = stem or parent or "Timeline"
        existing = {job.name for job in self.render_queue.jobs}
        if base not in existing:
            return base
        number = 2
        while f"{base} · {number}" in existing:
            number += 1
        return f"{base} · {number}"

    def add_current_to_queue(self) -> None:
        try:
            sources = self._validate_sources()
            output = self._request_render_destination(
                title="Add Timeline to Render Queue",
                action_label="ADD TO QUEUE",
            )
            if output is None:
                return
            config = self._collect_config()
            self._lock_render_frame_rate(config, sources)
            lower = self.timeline_in.value() if self._tc_alignment else 0
            upper = (
                self.timeline_out.value()
                if self._tc_alignment
                else (
                    lower + self.frame_limit.value()
                    if self.frame_limit.value()
                    else None
                )
            )
            if upper is not None:
                config.setdefault("video", {})["frames"] = upper - lower
            job = RenderJob.create(
                name=self._timeline_name(sources),
                source_paths=sources,
                config_snapshot=config,
                tc_alignment_snapshot=self._tc_alignment,
                tc_alignment_path=self._tc_alignment_path,
                in_frame=lower,
                out_frame=upper,
                output_path=output,
            )
            output_key = self._output_collision_key(output)
            duplicates = [
                queued
                for queued in self.render_queue.jobs
                if self._output_collision_key(str(queued.output_path)) == output_key
            ]
            if duplicates:
                raise ValueError("Another queued timeline already uses this output path")
            self.render_queue.add(job)
        except Exception as error:
            self._error("Render queue", str(error))
            return
        self._refresh_queue_table()
        self.jobs_toggle.setChecked(True)
        self._toggle_log(True)
        self.right_tabs.setCurrentIndex(1)
        self.queue_table.selectRow(len(self.render_queue.jobs) - 1)
        self.statusBar().showMessage(
            f"Added timeline to render queue: {job.name}", 8000
        )

    def add_selected_timeline_to_queue(self) -> None:
        kind, timeline_id = self._selected_timeline_item()
        if kind != "timeline" or not timeline_id:
            timeline_id = self._active_timeline_id
        if not timeline_id:
            self._error("Render queue", "Create or select a timeline first")
            return
        if timeline_id != self._active_timeline_id:
            self.load_project_timeline(timeline_id)
        if self._active_timeline_id == timeline_id:
            self.add_current_to_queue()
        return

    def remove_selected_queue_job(self) -> None:
        job = self._selected_queue_job()
        if job is None:
            self.statusBar().showMessage("Select a timeline in Render Queue", 5000)
            return
        if job.status is RenderStatus.RENDERING:
            self._error("Render queue", "The active render cannot be removed")
            return
        self.render_queue.remove(job.id)
        self._refresh_queue_table()

    def load_selected_queue_job(self, *_args) -> None:  # type: ignore[no-untyped-def]
        job = self._selected_queue_job()
        if job is None:
            self.statusBar().showMessage("Select a timeline in Render Queue", 5000)
            return
        if self.process is not None:
            self._error("Render queue", "Finish the current task before loading a timeline")
            return
        try:
            job_dir = self._working_dir / "queue" / job.id
            job_dir.mkdir(parents=True, exist_ok=True)
            config_path = job_dir / "config.json"
            config_path.write_text(
                json.dumps(job.config_snapshot, indent=2), encoding="utf-8"
            )
            self.load_config(config_path)
            loaded_cameras = self.config_data.get("cameras")
            if isinstance(loaded_cameras, list):
                self._plate_reset_cameras = json.loads(json.dumps(loaded_cameras))
            sources = [str(path) for path in job.source_paths]
            self._set_video_sources(sources, preserve_order=True)
            alignment = job.tc_alignment_snapshot
            if alignment is not None:
                alignment_path = job_dir / "timecode-alignment.json"
                alignment_path.write_text(
                    json.dumps(alignment, indent=2), encoding="utf-8"
                )
                self._tc_alignment_path = alignment_path
                self._apply_alignment_payload(
                    alignment,
                    lower=job.in_frame,
                    upper=job.out_frame,
                )
                probes = alignment.get("probes")
                if isinstance(probes, list):
                    self._apply_source_probe_payload(
                        {"inputs": probes, "issues": []}
                    )
            self._set_output_destination(str(job.output_path))
            self._last_auto_output = None
        except Exception as error:
            self._error("Load timeline", str(error))
            return
        self.statusBar().showMessage(f"Loaded timeline: {job.name}", 8000)

    def _materialize_queue_job(
        self, job: RenderJob
    ) -> tuple[Path, Path | None, list[str]]:
        job_dir = self._working_dir / "queue" / job.id
        job_dir.mkdir(parents=True, exist_ok=True)
        config = json.loads(json.dumps(job.config_snapshot))
        if job.out_frame is not None:
            config.setdefault("video", {})["frames"] = job.out_frame - job.in_frame
        config_path = job_dir / "config.json"
        config_path.write_text(json.dumps(config, indent=2), encoding="utf-8")
        if json.loads(config_path.read_text(encoding="utf-8")) != config:
            raise ValueError(
                f"Render settings lock verification failed: {job.snapshot_digest}"
            )
        alignment_path = None
        if job.tc_alignment_snapshot is not None:
            alignment_path = job_dir / "timecode-alignment.json"
            alignment_path.write_text(
                json.dumps(job.tc_alignment_snapshot, indent=2), encoding="utf-8"
            )
        return config_path, alignment_path, [str(path) for path in job.source_paths]

    def _start_queue_job(self, job: RenderJob) -> None:
        try:
            output = Path(job.output_path)
            codec = str(job.config_snapshot.get("video", {}).get("output_codec", ""))
            self._prepare_output_destination(output, codec)
            staging = self._render_staging_path(output, codec, job.id)
            self._discard_render_staging(staging)
            for source in job.source_paths:
                if not Path(source).is_file():
                    raise ValueError(f"Source is missing: {source}")
            config_path, alignment_path, sources = self._materialize_queue_job(job)
        except Exception as error:
            self.render_queue.update(
                job.id, status=RenderStatus.FAILED, error=str(error)
            )
            self._refresh_queue_table()
            if self._queue_running:
                QTimer.singleShot(0, self._run_next_queue_job)
            return

        self._queue_current_id = job.id
        total_frames = (
            max(0, job.out_frame - job.in_frame)
            if job.out_frame is not None
            else int(job.config_snapshot.get("video", {}).get("frames") or 0)
        )
        self._queue_progress[job.id] = (0, total_frames, None)
        self._begin_render_progress(total_frames)
        self._append_log(
            f"Queue settings locked: {job.snapshot_digest} · {job.name}"
        )
        self.render_queue.update(
            job.id,
            status=RenderStatus.RENDERING,
            error=None,
            elapsed_seconds=None,
        )
        self._refresh_queue_table()
        arguments = [
            "stitch-video",
            "--allow-low-bit-depth",
            "--config",
            str(config_path),
            "--output",
            str(staging),
            "--map-cache",
            str(self._cache_dir),
            "--start-frame",
            str(job.in_frame),
        ]
        if alignment_path is not None:
            arguments.extend(["--alignment-plan", str(alignment_path)])
        arguments.extend(sources)

        def completed() -> None:
            self._commit_render_staging(staging, output)
            progress = self._queue_progress.get(job.id)
            if progress is not None:
                self._queue_progress[job.id] = (progress[1], progress[1], 0.0)
            self.render_queue.update(
                job.id,
                status=RenderStatus.DONE,
                error=None,
                elapsed_seconds=self._last_render_elapsed_seconds,
            )
            self._queue_current_id = None
            self._refresh_queue_table()
            if self._queue_running:
                QTimer.singleShot(0, self._run_next_queue_job)

        def failed() -> None:
            self._discard_render_staging(staging)
            current = next(
                (queued for queued in self.render_queue.jobs if queued.id == job.id),
                None,
            )
            if (
                current is not None
                and current.status is RenderStatus.QUEUED
                and current.error == "Cancelled by user"
            ):
                self._refresh_queue_table()
                return
            self.render_queue.update(
                job.id,
                status=RenderStatus.FAILED,
                error="Render process failed; inspect Task Log",
            )
            self._queue_current_id = None
            self._refresh_queue_table()
            if self._queue_running:
                QTimer.singleShot(0, self._run_next_queue_job)

        self._run_cli(f"QUEUE · {job.name}", arguments, completed, failed)

    def render_selected_queue_job(self) -> None:
        if self.process is not None:
            self._error("Render queue", "Another task is already running")
            return
        job = self._selected_queue_job()
        if job is None:
            self.statusBar().showMessage("Select a timeline in Render Queue", 5000)
            return
        self._queue_running = False
        job = self.render_queue.update(
            job.id,
            status=RenderStatus.QUEUED,
            error=None,
            elapsed_seconds=None,
        )
        self._start_queue_job(job)

    def render_all_queue_jobs(self) -> None:
        if self.process is not None:
            self._error("Render queue", "Another task is already running")
            return
        for job in self.render_queue.jobs:
            if job.status is RenderStatus.FAILED:
                self.render_queue.update(
                    job.id, status=RenderStatus.QUEUED, error=None
                )
        self._queue_running = True
        self._run_next_queue_job()

    def _run_next_queue_job(self) -> None:
        if not self._queue_running or self.process is not None:
            return
        job = self.render_queue.next_queued()
        if job is None:
            self._queue_running = False
            self._queue_current_id = None
            self._refresh_queue_table()
            self.statusBar().showMessage("Render queue complete", 15000)
            return
        self._start_queue_job(job)

    def render(self) -> None:
        try:
            sources = self._validate_sources()
            output = self._request_render_destination(
                title="Render Current Timeline",
                action_label="RENDER NOW",
            )
            if output is None:
                return
            codec = str(self.output_codec.currentData())
            if codec not in GUI_MASTER_BIT_DEPTHS:
                raise ValueError("Choose a 10-bit or 12-bit master codec")
            output_path = Path(output)
            self._prepare_output_destination(output_path, codec)
            staging = self._render_staging_path(
                output_path,
                codec,
                f"current-{time.time_ns()}",
            )
            self._discard_render_staging(staging)
            config = self._write_working_config()
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
            str(staging),
            "--map-cache",
            str(self._cache_dir),
            "--start-frame",
            str(self.timeline_in.value() if self._tc_alignment else 0),
        ]
        if self._tc_alignment_path:
            arguments.extend(["--alignment-plan", str(self._tc_alignment_path)])
        arguments.extend(sources)
        configured_frames = int(
            json.loads(config.read_text(encoding="utf-8"))
            .get("video", {})
            .get("frames")
            or 0
        )
        total_frames = (
            max(0, self.timeline_out.value() - self.timeline_in.value())
            if self._tc_alignment
            else int(self.frame_limit.value() or configured_frames)
        )
        self._begin_render_progress(total_frames)

        def render_complete() -> None:
            self._commit_render_staging(staging, output_path)
            elapsed = self._last_render_elapsed_seconds
            duration = (
                ""
                if elapsed is None
                else f"\n\nRender time: {format_render_duration(elapsed)}"
            )
            self._show_message(
                QMessageBox.Icon.Information,
                "Render complete",
                f"Output written to:\n{output}{duration}",
            )

        self._run_cli(
            "FINAL RENDER",
            arguments,
            render_complete,
            lambda: self._discard_render_staging(staging),
        )

    def _run_cli(
        self,
        task: str,
        arguments: list[str],
        success: Callable[[], None] | None = None,
        failure: Callable[[], None] | None = None,
        *,
        interactive: bool = False,
    ) -> None:
        interactive = interactive or task in {
            "TC ALIGN",
            "ANALYZE INPUTS",
            "BUILD PLAYBACK PROXY",
            "STITCH PREVIEW",
            "EXTRACT REFERENCES",
        }
        if self._closing:
            return
        if self.process is not None:
            self._error("Busy", "Another task is already running")
            return
        self._process_success = success
        self._process_failure = failure
        self._process_interactive = interactive
        self._process_task_name = task
        self._process_output_buffer = ""
        self._process_phase = ""
        self.process = QProcess(self)
        self.process.setWorkingDirectory(str(self._working_dir))
        self.process.setProcessChannelMode(QProcess.ProcessChannelMode.MergedChannels)
        self.process.readyReadStandardOutput.connect(self._read_process)
        self.process.finished.connect(self._process_finished)
        self.task_label.setText(task)
        self.log_status.setText(f"{task} · running")
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
                process = self.process
                self.process = None
                self._process_success = None
                self._process_failure = None
                self._process_interactive = False
                self._process_task_name = ""
                self._set_busy_ui(False)
                self._end_render_progress()
                self._error("Packaged CLI missing", f"Expected bundled helper at:\n{cli_program}")
                process.deleteLater()
                if failure:
                    failure()
                return
            program = str(cli_program)
            process_arguments = list(arguments)
        else:
            program = sys.executable
            process_arguments = ["-m", "vpstitch.cli", *arguments]
        if task == "BUILD PLAYBACK PROXY" and os.name != "nt":
            nice = shutil.which("nice")
            if nice:
                process_arguments = ["-n", "10", program, *process_arguments]
                program = nice
        self.process.start(program, process_arguments)
        if not self.process.waitForStarted(3000):
            error = self.process.errorString() or "process did not start"
            process = self.process
            self.process = None
            self._process_success = None
            self._process_failure = None
            self._process_interactive = False
            self._process_task_name = ""
            self.progress.setRange(0, 100)
            self.progress.setValue(0)
            self.task_label.setText("FAILED")
            self.cancel_button.setVisible(False)
            self._set_busy_ui(False)
            self._end_render_progress()
            self._append_log(f"✕ {task} could not start: {error}")
            process.deleteLater()
            if failure:
                failure()
            self.statusBar().showMessage("Task could not start · see Task Log", 10000)
        elif self._render_progress_started_at is not None:
            self._refresh_render_clock()

    def _set_busy_ui(self, busy: bool) -> None:
        sources_ready = all(self.source_table.paths())
        inspector_live = busy and self._process_interactive
        self.import_button.setEnabled(not busy)
        self.assign_media_button.setEnabled(
            not busy
            and self._active_timeline_id is not None
            and len(self._selected_media_records()) in SUPPORTED_CAMERA_COUNTS
        )
        self.new_timeline_button.setEnabled(not busy)
        self.media_tree.setEnabled(not busy)
        self.timeline_tree.setEnabled(not busy)
        self.clear_button.setEnabled(not busy)
        self.source_table.setEnabled(not busy)
        self.settings_tabs.setEnabled(not busy or inspector_live)
        self.tc_align_button.setEnabled(not busy and sources_ready)
        self.preview_button.setEnabled(not busy and sources_ready)
        self.playback_button.setEnabled(not busy or inspector_live)
        self.add_queue_button.setEnabled(not busy and sources_ready)
        self.render_button.setEnabled(not busy and sources_ready)
        self.rig_align_button.setEnabled(not busy and self._preview_ready)
        self.progress.setVisible(busy)
        self.color_match_button.setEnabled(
            not busy
            and self._preview_ready
            and self.color_mode.currentData() == "ocio"
        )
        self.timeline_in.setEnabled(
            (not busy or inspector_live) and self._tc_alignment is not None
        )
        self.timeline_out.setEnabled(
            (not busy or inspector_live) and self._tc_alignment is not None
        )
        self.reset_timeline_button.setEnabled(
            (not busy or inspector_live) and self._tc_alignment is not None
        )

    def _read_process(self) -> None:
        process = self.sender()
        if self.process is None or (process is not None and process is not self.process):
            return
        text = bytes(self.process.readAllStandardOutput()).decode("utf-8", errors="replace")
        normalized = text.replace("\r", "\n")
        progress_value: tuple[int, int] | None = None
        frame_progress: tuple[int, int] | None = None
        frame_value: str | None = None
        for line in normalized.splitlines():
            phase = re.fullmatch(r"\s*phase\s+([a-z-]+)\s*", line)
            if phase:
                next_phase = phase.group(1)
                if (
                    next_phase == "render"
                    and self._process_phase != "render"
                    and self._render_progress_started_at is not None
                ):
                    # Projection-map startup is part of elapsed runtime, but it
                    # must not inflate the per-frame ETA estimate.
                    self._render_progress_last_at = time.monotonic()
                    self._render_progress_last_done = 0
                    self._render_seconds_per_frame = None
                    self._render_frame_samples.clear()
                    self._render_eta_warmup_remaining = 1
                    self._render_map_progress = None
                self._process_phase = next_phase
            match = re.search(r"tiles\s+(\d+)/(\d+)", line)
            if match:
                progress_value = int(match.group(1)), int(match.group(2))
            completed = re.search(r"progress\s+frames\s+(\d+)/(\d+)", line)
            if completed:
                frame_progress = int(completed.group(1)), int(completed.group(2))
            frame = re.search(r"frame\s+(\d+)", line)
            if frame:
                frame_value = frame.group(1)
            progress_only = re.fullmatch(
                r"\s*(?:phase\s+[a-z-]+|tiles\s+\d+/\d+|frame\s+\d+|progress\s+frames\s+\d+/\d+)\s*",
                line,
                flags=re.IGNORECASE,
            )
            if line.strip() and progress_only is None:
                self._append_log(line)
        if progress_value is not None:
            done, total = progress_value
            if self._process_phase == "projection-cache":
                if self._render_progress_started_at is not None:
                    self._render_map_progress = (done, total)
                self.progress.setRange(0, total)
                self.progress.setValue(done)
                if self._render_progress_started_at is not None:
                    self._refresh_render_clock()
                else:
                    self.task_label.setText(f"PREPARING MAPS {done}/{total}")
            elif frame_progress is None and self._queue_current_id is None:
                self.progress.setRange(0, total)
                self.progress.setValue(done)
        if frame_progress is not None:
            self._update_render_progress(*frame_progress)
        if (
            frame_value is not None
            and frame_progress is None
            and self._render_progress_started_at is None
        ):
            prefix = "CACHE" if self._process_task_name == "BUILD PLAYBACK PROXY" else "FRAME"
            self.task_label.setText(f"{prefix} {frame_value}")

    def _update_render_progress(self, done: int, total: int) -> None:
        done = min(max(0, int(done)), max(0, int(total)))
        total = max(0, int(total))
        self._render_progress_total = total
        now = time.monotonic()
        if (
            done > self._render_progress_last_done
            and self._render_progress_last_at is not None
        ):
            sample = (now - self._render_progress_last_at) / (
                done - self._render_progress_last_done
            )
            if self._render_eta_warmup_remaining > 0:
                # Initial frames upload fixed Metal maps, allocate the reusable
                # frame buffer, and warm GPU/VM pages. Skip that cold frame,
                # then begin ETA from the first steady inter-frame interval.
                self._render_eta_warmup_remaining = max(
                    0,
                    self._render_eta_warmup_remaining
                    - (done - self._render_progress_last_done),
                )
            elif np.isfinite(sample) and sample > 0.0:
                self._render_frame_samples.append(float(sample))
                del self._render_frame_samples[:-21]
                self._render_seconds_per_frame = robust_render_seconds_per_frame(
                    self._render_frame_samples,
                    self._render_seconds_per_frame,
                )
            self._render_progress_last_at = now
            self._render_progress_last_done = done
        eta = (
            None
            if self._render_seconds_per_frame is None
            else self._render_seconds_per_frame * max(0, total - done)
        )
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)
        if self._queue_current_id is not None:
            self._queue_progress[self._queue_current_id] = (done, total, eta)
        if self._render_progress_started_at is not None:
            self._refresh_render_clock()
        else:
            self.task_label.setText(
                f"FRAME {done}/{total} · {render_progress_text(done, total, eta)}"
            )
            if self._queue_current_id is not None:
                self._refresh_queue_table()

    def _begin_render_progress(self, total_frames: int) -> None:
        now = time.monotonic()
        self._render_progress_started_at = now
        self._render_progress_last_at = now
        self._render_progress_last_done = 0
        self._render_seconds_per_frame = None
        self._render_frame_samples.clear()
        self._render_eta_warmup_remaining = 1
        self._render_progress_total = max(0, int(total_frames))
        self._render_map_progress = None
        self._last_render_elapsed_seconds = None
        self._render_progress_timer.start()
        self._refresh_render_clock()

    def _end_render_progress(self) -> None:
        self._render_progress_timer.stop()
        self._render_progress_started_at = None
        self._render_progress_last_at = None
        self._render_progress_last_done = 0
        self._render_seconds_per_frame = None
        self._render_frame_samples.clear()
        self._render_eta_warmup_remaining = 1
        self._render_progress_total = 0
        self._render_map_progress = None

    def _render_elapsed_seconds(self) -> float | None:
        if self._render_progress_started_at is None:
            return None
        return max(0.0, time.monotonic() - self._render_progress_started_at)

    def _refresh_render_clock(self) -> None:
        elapsed = self._render_elapsed_seconds()
        if elapsed is None:
            self._render_progress_timer.stop()
            return
        if self._queue_current_id is not None:
            progress = self._queue_progress.get(self._queue_current_id)
        else:
            eta = (
                None
                if self._render_seconds_per_frame is None
                else self._render_seconds_per_frame
                * max(0, self._render_progress_total - self._render_progress_last_done)
            )
            progress = (
                self._render_progress_last_done,
                self._render_progress_total,
                eta,
            )
        done, total, eta = progress or (0, self._render_progress_total, None)
        if eta is not None and self._render_progress_last_at is not None:
            eta = max(0.0, eta - (time.monotonic() - self._render_progress_last_at))
        if self._process_phase == "projection-cache":
            if self._render_map_progress is None:
                stage = "PREPARING MAPS"
            else:
                map_done, map_total = self._render_map_progress
                stage = f"PREPARING MAPS {map_done}/{map_total}"
            self.task_label.setText(
                f"{stage} · {format_render_duration(elapsed)} ELAPSED"
            )
        elif done <= 0:
            self.task_label.setText(
                f"STARTING RENDER · {format_render_duration(elapsed)} ELAPSED"
            )
        elif total > 0 and done >= total:
            self.task_label.setText(
                f"FINALIZING · {format_render_duration(elapsed)} ELAPSED"
            )
        else:
            self.task_label.setText(
                f"FRAME {done}/{total} · {render_progress_text(done, total, eta)}"
                f" · {format_render_duration(elapsed)} ELAPSED"
            )
        if self._queue_current_id is not None:
            self._refresh_active_queue_status((done, total, eta))

    def _refresh_active_queue_status(
        self,
        display_progress: tuple[int, int, float | None] | None = None,
    ) -> None:
        """Update only the running row so the one-second clock never rebuilds the table."""
        if self._queue_current_id is None or not hasattr(self, "queue_table"):
            return
        job = next(
            (
                candidate
                for candidate in self.render_queue.jobs
                if candidate.id == self._queue_current_id
            ),
            None,
        )
        if job is None:
            return
        status = render_queue_status_text(
            job.status,
            display_progress or self._queue_progress.get(job.id),
            elapsed_seconds=self._render_elapsed_seconds(),
            phase=self._process_phase,
            map_progress=self._render_map_progress,
        )
        for row in range(self.queue_table.rowCount()):
            identity = self.queue_table.item(row, 0)
            if identity is None or identity.data(Qt.ItemDataRole.UserRole) != job.id:
                continue
            item = self.queue_table.item(row, 3)
            if item is not None:
                item.setText(status)
            return

    def _process_finished(self, exit_code: int, _status) -> None:  # type: ignore[no-untyped-def]
        sender = self.sender()
        if self.process is None or (sender is not None and sender is not self.process):
            return
        callback = self._process_success
        failure = self._process_failure
        process = self.process
        task = self._process_task_name or self.task_label.text()
        render_elapsed = self._render_elapsed_seconds()
        self._last_render_elapsed_seconds = render_elapsed
        cancelled_for_interaction = bool(
            task == "BUILD PLAYBACK PROXY"
            and self._playback_cache_cancelled_for_interaction
        )
        self.process = None
        self._process_success = None
        self._process_failure = None
        self._process_interactive = False
        self._process_task_name = ""
        self._end_render_progress()
        try:
            process.readyReadStandardOutput.disconnect(self._read_process)
            process.finished.disconnect(self._process_finished)
        except (RuntimeError, TypeError):
            pass
        if self._closing:
            process.deleteLater()
            return
        self.progress.setRange(0, 100)
        successful_or_cancelled = exit_code == 0 or cancelled_for_interaction
        self.progress.setValue(100 if exit_code == 0 else 0)
        self.task_label.setText("IDLE" if successful_or_cancelled else "FAILED")
        self.log_status.setText(
            f"{task} · {'complete' if successful_or_cancelled else 'failed'}"
        )
        self.status_pill.setText("READY" if successful_or_cancelled else "FAILED")
        self.cancel_button.setVisible(False)
        self._set_busy_ui(False)
        if process is not None:
            process.deleteLater()
        if exit_code == 0:
            self._append_log(f"✓ {task} complete")
            if render_elapsed is not None:
                self._append_log(
                    f"  elapsed {format_render_duration(render_elapsed)}"
                )
            if callback:
                try:
                    callback()
                except Exception as error:
                    self.progress.setValue(0)
                    self.task_label.setText("FAILED")
                    self.status_pill.setText("FAILED")
                    self._append_log(f"✕ {task} finalize failed: {error}")
                    self.statusBar().showMessage(
                        "Render finalization failed · original destination was not replaced",
                        12000,
                    )
                    if failure:
                        try:
                            failure()
                        except Exception as failure_error:
                            self._append_log(
                                f"✕ {task} cleanup failed: {failure_error}"
                            )
        elif cancelled_for_interaction:
            self._append_log("↷ PLAYBACK CACHE paused for Inspector adjustment")
            if failure:
                try:
                    failure()
                except Exception as error:
                    self._append_log(f"✕ playback cache cleanup failed: {error}")
        else:
            self._append_log(f"✕ {task} failed with exit code {exit_code}")
            self.statusBar().showMessage("Task failed — see Task Log", 10000)
            if failure:
                try:
                    failure()
                except Exception as error:
                    self._append_log(f"✕ {task} cleanup failed: {error}")
        if self._live_preview_pending and self.process is None:
            QTimer.singleShot(0, self._run_live_preview)
            if self._auto_cache_requested:
                self._playback_warmup_timer.start(700)
        elif self._auto_cache_requested and self.process is None:
            self._request_playback_warmup(delay_ms=0)

    def cancel_task(self) -> None:
        if self.process is not None:
            if self._queue_current_id is not None:
                self._queue_running = False
                try:
                    self.render_queue.update(
                        self._queue_current_id,
                        status=RenderStatus.QUEUED,
                        error="Cancelled by user",
                    )
                except (KeyError, RenderQueueError):
                    pass
                self._queue_current_id = None
                self._refresh_queue_table()
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

    def _color_basis_changed(self, *_args) -> None:
        if self._loading_config or not self.config_data:
            return
        cameras = self.config_data.get("cameras")
        if isinstance(cameras, list):
            for camera in cameras:
                if isinstance(camera, dict):
                    camera["color_gain"] = [1.0, 1.0, 1.0]
                    camera.pop("color_match_confidence", None)
        self.color_match_enabled.blockSignals(True)
        self.color_match_enabled.setChecked(False)
        self.color_match_enabled.blockSignals(False)
        self.color_match_status.setText("Input/working space changed · run MATCH again")
        self._color_pipeline_setting_changed()

    def _color_pipeline_setting_changed(self, *_args) -> None:
        if self._loading_config or not self.config_data:
            return
        self._schedule_live_preview("Color pipeline updated")

    def _update_color_controls(self) -> None:
        enabled = self.color_mode.currentData() == "ocio"
        for widget in (
            self.ocio_config,
            self.ocio_reload_button,
            self.input_space,
            self.working_space,
            self.color_match_enabled,
            self.color_match_reference,
            self.color_match_strength,
            self.color_match_reset_button,
        ):
            widget.setEnabled(enabled)
        self.color_match_button.setEnabled(enabled and self._preview_ready)
        self._update_delivery_controls()

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
                added = self.import_media_paths(videos)
                self.statusBar().showMessage(
                    f"Imported {len(added)} media clips · add a complete set to a timeline",
                    10000,
                )
            except Exception as error:
                self._error("Import Media", str(error))
        event.acceptProposedAction()

    def closeEvent(self, event: QCloseEvent) -> None:
        if self._fullscreen_preview is not None:
            self._fullscreen_preview.close()
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
            self._queue_running = False
            if self._queue_current_id is not None:
                try:
                    self.render_queue.update(
                        self._queue_current_id,
                        status=RenderStatus.QUEUED,
                        error="Application closed during render",
                    )
                except (KeyError, RenderQueueError):
                    pass
                self._queue_current_id = None
            self.media_player.stop()
            self._reverse_timer.stop()
            self._live_preview_timer.stop()
            self._playback_warmup_timer.stop()
            self._live_preview_pending = False
            self._auto_cache_requested = False
            self._pending_playback_request = False
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
            self._queue_running = False
            self.media_player.stop()
            self._reverse_timer.stop()
            self._live_preview_timer.stop()
            self._playback_warmup_timer.stop()
            self._live_preview_pending = False
            self._auto_cache_requested = False
            self._pending_playback_request = False
            self._log_flush_timer.stop()
            self._pending_log_lines.clear()
        self._cancel_source_proxy_items()
        self._autosave_timer.stop()
        self._interactive_request = None
        self._stop_live_proxy_playback(close_session=True)
        # If a worker is blocked reading an FFmpeg proxy frame, actively close
        # its decoder before waiting. ThreadPoolExecutor workers are non-daemon;
        # leaving one behind can make the window disappear while the process
        # remains alive indefinitely.
        live_session, self._live_playback_session = (
            self._live_playback_session,
            None,
        )
        self._live_playback_key = None
        self._live_close_pending = False
        if live_session is not None:
            live_session.close()
        if self._live_playback_future is not None:
            self._live_playback_future.cancel()
        self._live_playback_executor.shutdown(wait=True, cancel_futures=True)
        self._live_playback_future = None
        self._interactive_executor.shutdown(wait=False, cancel_futures=True)
        self._autosave_project_snapshot(force=True)
        self._save_workspace_layout()
        event.accept()


def _configure_application_attributes() -> None:
    if (
        sys.platform == "darwin"
        and os.environ.get("VPSTITCH_NON_NATIVE_DIALOGS") == "1"
    ):
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
        settings = _application_settings()
        if (
            sys.platform == "darwin"
            and not settings.value(_STORAGE_SETUP_KEY, False, type=bool)
        ):
            StorageAccessDialog(first_run=True).exec()
            settings.setValue(_STORAGE_SETUP_KEY, True)
            settings.sync()
        launcher = ProjectManagerDialog()
        if launcher.exec() != QDialog.DialogCode.Accepted or launcher.project_path is None:
            return 0
        window = MainWindow(launcher.project_path)
        window.show()
        return app.exec()
    except Exception:
        traceback.print_exc()
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
