from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Callable, Iterable, Mapping, TypeVar


STORE_VERSION = 1
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_PLATE_NUMBER = re.compile(r"(?:^|[^A-Z0-9])P(0[1-8])(?=[^0-9]|$)", re.IGNORECASE)
_T = TypeVar("_T")


def _path_to_json(value: PurePath) -> str:
    if not value.is_absolute() and not value.drive:
        return PurePosixPath(*value.parts).as_posix()
    return str(value)


class ProjectError(ValueError):
    """Raised when project data is invalid or cannot be persisted."""


class PlaybackCacheStatus(str, Enum):
    EMPTY = "empty"
    PENDING = "pending"
    BUILDING = "building"
    READY = "ready"
    FAILED = "failed"


class StitchStatus(str, Enum):
    UNSTITCHED = "unstitched"
    ALIGNING = "aligning"
    READY = "ready"
    FAILED = "failed"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _validate_timestamp(value: Any, field: str) -> str:
    if not isinstance(value, str):
        raise ProjectError(f"{field} must be an ISO timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ProjectError(f"{field} must be an ISO timestamp") from exc
    if parsed.tzinfo is None:
        raise ProjectError(f"{field} must include a timezone")
    return value


def _require_keys(payload: Mapping[str, Any], allowed: set[str], *, field: str) -> None:
    unknown = set(payload) - allowed
    if unknown:
        raise ProjectError(f"{field} contains unknown fields: {', '.join(sorted(unknown))}")


def _json_value(value: Any, *, field: str) -> Any:
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, PurePath):
        return _path_to_json(value)
    if isinstance(value, Enum):
        return _json_value(value.value, field=field)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise ProjectError(f"{field} keys must be strings")
            result[key] = _json_value(item, field=field)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field=field) for item in value]
    raise ProjectError(f"{field} contains a non-JSON value: {type(value).__name__}")


def _snapshot(value: Mapping[str, Any], *, field: str) -> dict[str, Any]:
    converted = _json_value(value, field=field)
    assert isinstance(converted, dict)
    return converted


def _coerce_path(value: str | PurePath) -> PurePath:
    if isinstance(value, PurePath):
        return value
    if not isinstance(value, str):
        raise ProjectError("paths must be strings or pathlib paths")
    return _path_from_json(value)


def _path_from_json(value: Any) -> PurePath:
    if not isinstance(value, str) or not value:
        raise ProjectError("stored paths must be non-empty strings")
    if _WINDOWS_PATH.match(value) or "\\" in value:
        return PureWindowsPath(value)
    if value.startswith("/"):
        return PurePosixPath(value)
    return Path(value)


def _plate_number(path: PurePath) -> int | None:
    match = _PLATE_NUMBER.search(path.name)
    return None if match is None else int(match.group(1))


@dataclass(frozen=True, slots=True)
class ProjectSettings:
    name: str
    settings_snapshot: dict[str, Any]

    def __post_init__(self) -> None:
        name = str(self.name).strip()
        if not name:
            raise ProjectError("project name cannot be empty")
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self,
            "settings_snapshot",
            _snapshot(self.settings_snapshot, field="settings_snapshot"),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "settings_snapshot": _json_value(
                self.settings_snapshot, field="settings_snapshot"
            ),
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> ProjectSettings:
        _require_keys(payload, {"name", "settings_snapshot"}, field="settings")
        snapshot = payload.get("settings_snapshot")
        if not isinstance(snapshot, Mapping):
            raise ProjectError("settings_snapshot must be an object")
        if "name" not in payload:
            raise ProjectError("settings is missing field: name")
        return cls(name=payload["name"], settings_snapshot=dict(snapshot))


@dataclass(frozen=True, slots=True)
class Bin:
    id: str
    name: str
    parent_id: str | None = None
    order: int = 0

    def __post_init__(self) -> None:
        bin_id = str(self.id).strip()
        name = str(self.name).strip()
        if not bin_id or not name:
            raise ProjectError("bin id and name cannot be empty")
        if self.parent_id is not None and not str(self.parent_id).strip():
            raise ProjectError("parent_id cannot be empty")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise ProjectError("bin order must be a non-negative integer")
        object.__setattr__(self, "id", bin_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(
            self, "parent_id", None if self.parent_id is None else str(self.parent_id).strip()
        )

    @classmethod
    def create(
        cls, name: str, *, parent_id: str | None = None, order: int = 0, bin_id: str | None = None
    ) -> Bin:
        return cls(id=bin_id or uuid.uuid4().hex, name=name, parent_id=parent_id, order=order)

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "parent_id": self.parent_id, "order": self.order}

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> Bin:
        allowed = {"id", "name", "parent_id", "order"}
        _require_keys(payload, allowed, field="bin")
        missing = allowed - set(payload)
        if missing:
            raise ProjectError(f"bin is missing fields: {', '.join(sorted(missing))}")
        return cls(**{key: payload[key] for key in allowed})


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """One imported plate set and its independent stitch/playback state."""

    id: str
    name: str
    bin_id: str | None
    source_paths: tuple[PurePath, ...]
    config_snapshot: dict[str, Any]
    tc_alignment_snapshot: dict[str, Any] | None = None
    in_frame: int = 0
    out_frame: int | None = None
    playback_cache_path: PurePath | None = None
    playback_cache_status: PlaybackCacheStatus = PlaybackCacheStatus.EMPTY
    stitch_status: StitchStatus = StitchStatus.UNSTITCHED
    order: int = 0
    created_at: str = ""
    updated_at: str = ""

    def __post_init__(self) -> None:
        timeline_id = str(self.id).strip()
        name = str(self.name).strip()
        if not timeline_id or not name:
            raise ProjectError("timeline id and name cannot be empty")
        if self.bin_id is not None and not str(self.bin_id).strip():
            raise ProjectError("bin_id cannot be empty")
        sources = tuple(_coerce_path(path) for path in self.source_paths)
        observed = tuple(_plate_number(path) for path in sources)
        if observed not in ((1, 2, 3, 4, 5), (6, 7, 8)):
            raise ProjectError("source_paths must be ordered P01-P05 or P06-P08")
        if isinstance(self.in_frame, bool) or not isinstance(self.in_frame, int) or self.in_frame < 0:
            raise ProjectError("in_frame must be a non-negative integer")
        if self.out_frame is not None:
            if isinstance(self.out_frame, bool) or not isinstance(self.out_frame, int):
                raise ProjectError("out_frame must be an integer or None")
            if self.out_frame <= self.in_frame:
                raise ProjectError("out_frame must be greater than in_frame")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise ProjectError("timeline order must be a non-negative integer")
        try:
            playback_status = PlaybackCacheStatus(self.playback_cache_status)
            stitch_status = StitchStatus(self.stitch_status)
        except ValueError as exc:
            raise ProjectError(f"invalid timeline status: {exc}") from exc
        created = _validate_timestamp(self.created_at or _now(), "created_at")
        updated = _validate_timestamp(self.updated_at or created, "updated_at")
        alignment = self.tc_alignment_snapshot
        if alignment is not None and not isinstance(alignment, Mapping):
            raise ProjectError("tc_alignment_snapshot must be an object or null")
        object.__setattr__(self, "id", timeline_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "bin_id", None if self.bin_id is None else str(self.bin_id).strip())
        object.__setattr__(self, "source_paths", sources)
        object.__setattr__(self, "config_snapshot", _snapshot(self.config_snapshot, field="config_snapshot"))
        object.__setattr__(
            self,
            "tc_alignment_snapshot",
            None if alignment is None else _snapshot(alignment, field="tc_alignment_snapshot"),
        )
        object.__setattr__(
            self,
            "playback_cache_path",
            None if self.playback_cache_path is None else _coerce_path(self.playback_cache_path),
        )
        object.__setattr__(self, "playback_cache_status", playback_status)
        object.__setattr__(self, "stitch_status", stitch_status)
        object.__setattr__(self, "created_at", created)
        object.__setattr__(self, "updated_at", updated)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        source_paths: Iterable[str | PurePath],
        config_snapshot: Mapping[str, Any],
        bin_id: str | None = None,
        tc_alignment_snapshot: Mapping[str, Any] | None = None,
        in_frame: int = 0,
        out_frame: int | None = None,
        playback_cache_path: str | PurePath | None = None,
        playback_cache_status: PlaybackCacheStatus | str = PlaybackCacheStatus.EMPTY,
        stitch_status: StitchStatus | str = StitchStatus.UNSTITCHED,
        order: int = 0,
        timeline_id: str | None = None,
    ) -> TimelineRecord:
        timestamp = _now()
        return cls(
            id=timeline_id or uuid.uuid4().hex,
            name=name,
            bin_id=bin_id,
            source_paths=tuple(source_paths),
            config_snapshot=dict(config_snapshot),
            tc_alignment_snapshot=None if tc_alignment_snapshot is None else dict(tc_alignment_snapshot),
            in_frame=in_frame,
            out_frame=out_frame,
            playback_cache_path=playback_cache_path,
            playback_cache_status=PlaybackCacheStatus(playback_cache_status),
            stitch_status=StitchStatus(stitch_status),
            order=order,
            created_at=timestamp,
            updated_at=timestamp,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "bin_id": self.bin_id,
            "source_paths": [_path_to_json(path) for path in self.source_paths],
            "config_snapshot": _json_value(self.config_snapshot, field="config_snapshot"),
            "tc_alignment_snapshot": _json_value(
                self.tc_alignment_snapshot, field="tc_alignment_snapshot"
            ),
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "playback_cache_path": (
                None
                if self.playback_cache_path is None
                else _path_to_json(self.playback_cache_path)
            ),
            "playback_cache_status": self.playback_cache_status.value,
            "stitch_status": self.stitch_status.value,
            "order": self.order,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> TimelineRecord:
        allowed = {
            "id", "name", "bin_id", "source_paths", "config_snapshot",
            "tc_alignment_snapshot", "in_frame", "out_frame", "playback_cache_path",
            "playback_cache_status", "stitch_status", "order", "created_at", "updated_at",
        }
        _require_keys(payload, allowed, field="timeline")
        missing = allowed - set(payload)
        if missing:
            raise ProjectError(f"timeline is missing fields: {', '.join(sorted(missing))}")
        sources = payload["source_paths"]
        config = payload["config_snapshot"]
        alignment = payload["tc_alignment_snapshot"]
        if not isinstance(sources, list):
            raise ProjectError("source_paths must be a list")
        if not isinstance(config, Mapping):
            raise ProjectError("config_snapshot must be an object")
        if alignment is not None and not isinstance(alignment, Mapping):
            raise ProjectError("tc_alignment_snapshot must be an object or null")
        values = dict(payload)
        values["source_paths"] = tuple(_path_from_json(path) for path in sources)
        values["config_snapshot"] = dict(config)
        values["tc_alignment_snapshot"] = None if alignment is None else dict(alignment)
        values["playback_cache_path"] = (
            None if payload["playback_cache_path"] is None else _path_from_json(payload["playback_cache_path"])
        )
        return cls(**values)


class ProjectStore:
    """Ordered, JSON-backed project model with immediate atomic persistence."""

    def __init__(
        self,
        path: str | PurePath,
        settings: ProjectSettings,
        bins: Iterable[Bin] = (),
        timelines: Iterable[TimelineRecord] = (),
        *,
        autosave: bool = True,
    ) -> None:
        self.path = Path(path)
        self.settings = settings
        self.autosave = autosave
        self._bins = list(bins)
        self._timelines = list(timelines)
        self._validate()

    @classmethod
    def create(
        cls,
        path: str | PurePath,
        *,
        name: str,
        settings_snapshot: Mapping[str, Any] | None = None,
        autosave: bool = True,
    ) -> ProjectStore:
        store = cls(
            path,
            ProjectSettings(name=name, settings_snapshot=dict(settings_snapshot or {})),
            autosave=autosave,
        )
        if autosave:
            store.save()
        return store

    @property
    def bins(self) -> tuple[Bin, ...]:
        return tuple(self._bins)

    @property
    def timelines(self) -> tuple[TimelineRecord, ...]:
        return tuple(self._timelines)

    def _validate(self) -> None:
        bin_ids = [item.id for item in self._bins]
        timeline_ids = [item.id for item in self._timelines]
        if len(bin_ids) != len(set(bin_ids)):
            raise ProjectError("project contains duplicate bin ids")
        if len(timeline_ids) != len(set(timeline_ids)):
            raise ProjectError("project contains duplicate timeline ids")
        known_bins = set(bin_ids)
        for item in self._bins:
            if item.parent_id is not None and item.parent_id not in known_bins:
                raise ProjectError(f"unknown parent bin: {item.parent_id}")
            self._assert_no_bin_cycle(item.id, items=self._bins)
        for timeline in self._timelines:
            if timeline.bin_id is not None and timeline.bin_id not in known_bins:
                raise ProjectError(f"unknown timeline bin: {timeline.bin_id}")
        self._validate_sibling_orders()

    def _validate_sibling_orders(self) -> None:
        for parent_id in {item.parent_id for item in self._bins}:
            orders = [item.order for item in self._bins if item.parent_id == parent_id]
            if len(orders) != len(set(orders)):
                raise ProjectError("sibling bins contain duplicate order values")
        for bin_id in {item.bin_id for item in self._timelines}:
            orders = [item.order for item in self._timelines if item.bin_id == bin_id]
            if len(orders) != len(set(orders)):
                raise ProjectError("sibling timelines contain duplicate order values")

    def _assert_no_bin_cycle(self, bin_id: str, *, items: list[Bin] | None = None) -> None:
        by_id = {item.id: item for item in (self._bins if items is None else items)}
        seen = {bin_id}
        current = by_id.get(bin_id)
        while current is not None and current.parent_id is not None:
            if current.parent_id in seen:
                raise ProjectError("bin hierarchy cannot contain a cycle")
            seen.add(current.parent_id)
            current = by_id.get(current.parent_id)

    def _mutate(self, operation: Callable[[], _T]) -> _T:
        old_settings = self.settings
        old_bins = list(self._bins)
        old_timelines = list(self._timelines)
        try:
            result = operation()
            self._validate()
            if self.autosave:
                self.save()
            return result
        except Exception:
            self.settings = old_settings
            self._bins = old_bins
            self._timelines = old_timelines
            raise

    def update_settings(self, *, name: str | None = None, settings_snapshot: Mapping[str, Any] | None = None) -> ProjectSettings:
        def change() -> ProjectSettings:
            self.settings = ProjectSettings(
                name=self.settings.name if name is None else name,
                settings_snapshot=self.settings.settings_snapshot if settings_snapshot is None else dict(settings_snapshot),
            )
            return self.settings
        return self._mutate(change)

    def list_bins(self, parent_id: str | None = None) -> tuple[Bin, ...]:
        return tuple(sorted((item for item in self._bins if item.parent_id == parent_id), key=lambda item: item.order))

    def add_bin(self, item: Bin) -> Bin:
        def change() -> Bin:
            if any(existing.id == item.id for existing in self._bins):
                raise ProjectError(f"duplicate bin id: {item.id}")
            if item.parent_id is not None and not any(existing.id == item.parent_id for existing in self._bins):
                raise ProjectError(f"unknown parent bin: {item.parent_id}")
            siblings = self.list_bins(item.parent_id)
            index = min(item.order, len(siblings))
            self._bins = [
                replace(existing, order=existing.order + 1)
                if existing.parent_id == item.parent_id and existing.order >= index else existing
                for existing in self._bins
            ]
            added = replace(item, order=index)
            self._bins.append(added)
            return added
        return self._mutate(change)

    def update_bin(self, bin_id: str, *, name: str) -> Bin:
        def change() -> Bin:
            index = self._bin_index(bin_id)
            updated = replace(self._bins[index], name=name)
            self._bins[index] = updated
            return updated
        return self._mutate(change)

    def remove_bin(self, bin_id: str, *, recursive: bool = False) -> Bin:
        def change() -> Bin:
            index = self._bin_index(bin_id)
            removed = self._bins[index]
            descendants = self._descendant_ids(bin_id)
            occupied = any(item.bin_id in descendants for item in self._timelines)
            if (len(descendants) > 1 or occupied) and not recursive:
                raise ProjectError("bin is not empty; use recursive=True")
            self._bins = [item for item in self._bins if item.id not in descendants]
            if recursive:
                self._timelines = [item for item in self._timelines if item.bin_id not in descendants]
            self._normalize_bin_orders(removed.parent_id)
            return removed
        return self._mutate(change)

    def move_bin(self, bin_id: str, parent_id: str | None, index: int | None = None) -> Bin:
        def change() -> Bin:
            old_index = self._bin_index(bin_id)
            current = self._bins[old_index]
            if parent_id is not None and not any(item.id == parent_id for item in self._bins):
                raise ProjectError(f"unknown parent bin: {parent_id}")
            if parent_id == bin_id or parent_id in self._descendant_ids(bin_id):
                raise ProjectError("cannot move a bin into itself or its descendant")
            self._bins.pop(old_index)
            self._normalize_bin_orders(current.parent_id)
            siblings = self.list_bins(parent_id)
            target = len(siblings) if index is None else self._checked_index(index, len(siblings))
            self._bins = [
                replace(item, order=item.order + 1)
                if item.parent_id == parent_id and item.order >= target else item
                for item in self._bins
            ]
            moved = replace(current, parent_id=parent_id, order=target)
            self._bins.append(moved)
            return moved
        return self._mutate(change)

    def reorder_bin(self, bin_id: str, index: int) -> Bin:
        item = self._bins[self._bin_index(bin_id)]
        return self.move_bin(bin_id, item.parent_id, index)

    def list_timelines(self, bin_id: str | None = None) -> tuple[TimelineRecord, ...]:
        return tuple(sorted((item for item in self._timelines if item.bin_id == bin_id), key=lambda item: item.order))

    def add_timeline(self, item: TimelineRecord) -> TimelineRecord:
        def change() -> TimelineRecord:
            if any(existing.id == item.id for existing in self._timelines):
                raise ProjectError(f"duplicate timeline id: {item.id}")
            if item.bin_id is not None and not any(existing.id == item.bin_id for existing in self._bins):
                raise ProjectError(f"unknown timeline bin: {item.bin_id}")
            siblings = self.list_timelines(item.bin_id)
            index = min(item.order, len(siblings))
            self._timelines = [
                replace(existing, order=existing.order + 1)
                if existing.bin_id == item.bin_id and existing.order >= index else existing
                for existing in self._timelines
            ]
            added = replace(item, order=index)
            self._timelines.append(added)
            return added
        return self._mutate(change)

    def update_timeline(self, timeline_id: str, **changes: Any) -> TimelineRecord:
        forbidden = {"id", "created_at", "order", "bin_id"}
        unknown = set(changes) - {field for field in TimelineRecord.__dataclass_fields__}
        if unknown or forbidden.intersection(changes):
            names = unknown | forbidden.intersection(changes)
            raise ProjectError(f"unsupported timeline updates: {', '.join(sorted(names))}")

        def change() -> TimelineRecord:
            index = self._timeline_index(timeline_id)
            updated = replace(self._timelines[index], **changes, updated_at=_now())
            self._timelines[index] = updated
            return updated
        return self._mutate(change)

    def remove_timeline(self, timeline_id: str) -> TimelineRecord:
        def change() -> TimelineRecord:
            index = self._timeline_index(timeline_id)
            removed = self._timelines.pop(index)
            self._normalize_timeline_orders(removed.bin_id)
            return removed
        return self._mutate(change)

    def move_timeline(self, timeline_id: str, bin_id: str | None, index: int | None = None) -> TimelineRecord:
        def change() -> TimelineRecord:
            old_index = self._timeline_index(timeline_id)
            current = self._timelines.pop(old_index)
            if bin_id is not None and not any(item.id == bin_id for item in self._bins):
                raise ProjectError(f"unknown timeline bin: {bin_id}")
            self._normalize_timeline_orders(current.bin_id)
            siblings = self.list_timelines(bin_id)
            target = len(siblings) if index is None else self._checked_index(index, len(siblings))
            self._timelines = [
                replace(item, order=item.order + 1)
                if item.bin_id == bin_id and item.order >= target else item
                for item in self._timelines
            ]
            moved = replace(current, bin_id=bin_id, order=target, updated_at=_now())
            self._timelines.append(moved)
            return moved
        return self._mutate(change)

    def reorder_timeline(self, timeline_id: str, index: int) -> TimelineRecord:
        item = self._timelines[self._timeline_index(timeline_id)]
        return self.move_timeline(timeline_id, item.bin_id, index)

    def _bin_index(self, bin_id: str) -> int:
        for index, item in enumerate(self._bins):
            if item.id == bin_id:
                return index
        raise ProjectError(f"unknown bin: {bin_id}")

    def _timeline_index(self, timeline_id: str) -> int:
        for index, item in enumerate(self._timelines):
            if item.id == timeline_id:
                return index
        raise ProjectError(f"unknown timeline: {timeline_id}")

    def _descendant_ids(self, bin_id: str) -> set[str]:
        result = {bin_id}
        changed = True
        while changed:
            before = len(result)
            result.update(item.id for item in self._bins if item.parent_id in result)
            changed = len(result) != before
        return result

    @staticmethod
    def _checked_index(index: int, sibling_count: int) -> int:
        if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= sibling_count:
            raise ProjectError(f"index must be between 0 and {sibling_count}")
        return index

    def _normalize_bin_orders(self, parent_id: str | None) -> None:
        ordered = sorted((item for item in self._bins if item.parent_id == parent_id), key=lambda item: item.order)
        replacements = {item.id: replace(item, order=index) for index, item in enumerate(ordered)}
        self._bins = [replacements.get(item.id, item) for item in self._bins]

    def _normalize_timeline_orders(self, bin_id: str | None) -> None:
        ordered = sorted((item for item in self._timelines if item.bin_id == bin_id), key=lambda item: item.order)
        replacements = {item.id: replace(item, order=index) for index, item in enumerate(ordered)}
        self._timelines = [replacements.get(item.id, item) for item in self._timelines]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "settings": self.settings.to_dict(),
            "bins": [item.to_dict() for item in self._bins],
            "timelines": [item.to_dict() for item in self._timelines],
        }

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
            temporary = None
            try:
                directory_fd = os.open(self.path.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            raise ProjectError(f"cannot save project {self.path}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    @classmethod
    def load(cls, path: str | PurePath, *, autosave: bool = True) -> ProjectStore:
        store_path = Path(path)
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot load project {store_path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise ProjectError("project root must be an object")
        _require_keys(payload, {"version", "settings", "bins", "timelines"}, field="project")
        if payload.get("version") != STORE_VERSION:
            raise ProjectError(f"unsupported project version: {payload.get('version')!r}")
        if set(payload) != {"version", "settings", "bins", "timelines"}:
            raise ProjectError("project is missing required fields")
        settings = payload["settings"]
        bins = payload["bins"]
        timelines = payload["timelines"]
        if not isinstance(settings, Mapping) or not isinstance(bins, list) or not isinstance(timelines, list):
            raise ProjectError("project settings must be an object; bins and timelines must be lists")
        if not all(isinstance(item, Mapping) for item in bins + timelines):
            raise ProjectError("project bins and timelines must contain objects")
        return cls(
            store_path,
            ProjectSettings.from_dict(settings),
            [Bin.from_dict(item) for item in bins],
            [TimelineRecord.from_dict(item) for item in timelines],
            autosave=autosave,
        )
