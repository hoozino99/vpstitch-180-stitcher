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


STORE_VERSION = 2
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")
_EXPLICIT_PLATE_NUMBER = re.compile(
    r"(?:^|[^a-z0-9])(?:p(?:late)?|cam(?:era)?)[ ._-]*0?([1-8])(?=$|[^0-9])",
    re.IGNORECASE,
)
_BARE_PLATE_NUMBER = re.compile(r"(?:^|[^0-9])0([1-8])(?=$|[^0-9])")
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


class MediaCacheStatus(str, Enum):
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
    components = [path.stem, *(parent.name for parent in path.parents[:3])]
    for pattern in (_EXPLICIT_PLATE_NUMBER, _BARE_PLATE_NUMBER):
        for component in components:
            match = pattern.search(component)
            if match:
                return int(match.group(1))
    return None


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
class MediaRecord:
    """One source clip stored in the project Media Pool."""

    id: str
    path: PurePath
    bin_id: str | None = None
    order: int = 0
    created_at: str = ""
    source_cache_path: PurePath | None = None
    source_cache_status: MediaCacheStatus = MediaCacheStatus.EMPTY
    source_cache_error: str | None = None

    def __post_init__(self) -> None:
        media_id = str(self.id).strip()
        if not media_id:
            raise ProjectError("media id cannot be empty")
        if self.bin_id is not None and not str(self.bin_id).strip():
            raise ProjectError("bin_id cannot be empty")
        if isinstance(self.order, bool) or not isinstance(self.order, int) or self.order < 0:
            raise ProjectError("media order must be a non-negative integer")
        object.__setattr__(self, "id", media_id)
        object.__setattr__(self, "path", _coerce_path(self.path))
        object.__setattr__(
            self, "bin_id", None if self.bin_id is None else str(self.bin_id).strip()
        )
        object.__setattr__(
            self,
            "source_cache_path",
            None
            if self.source_cache_path is None
            else _coerce_path(self.source_cache_path),
        )
        object.__setattr__(
            self,
            "source_cache_status",
            MediaCacheStatus(self.source_cache_status),
        )
        object.__setattr__(
            self,
            "source_cache_error",
            None
            if self.source_cache_error is None
            else str(self.source_cache_error).strip() or None,
        )
        object.__setattr__(self, "created_at", _validate_timestamp(self.created_at or _now(), "created_at"))

    @classmethod
    def create(
        cls,
        path: str | PurePath,
        *,
        bin_id: str | None = None,
        order: int = 0,
        media_id: str | None = None,
    ) -> MediaRecord:
        return cls(
            id=media_id or uuid.uuid4().hex,
            path=_coerce_path(path),
            bin_id=bin_id,
            order=order,
            created_at=_now(),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "path": _path_to_json(self.path),
            "bin_id": self.bin_id,
            "order": self.order,
            "created_at": self.created_at,
            "source_cache_path": (
                None
                if self.source_cache_path is None
                else _path_to_json(self.source_cache_path)
            ),
            "source_cache_status": self.source_cache_status.value,
            "source_cache_error": self.source_cache_error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> MediaRecord:
        required = {"id", "path", "bin_id", "order", "created_at"}
        optional = {
            "source_cache_path",
            "source_cache_status",
            "source_cache_error",
        }
        allowed = required | optional
        _require_keys(payload, allowed, field="media")
        missing = required - set(payload)
        if missing:
            raise ProjectError(f"media is missing fields: {', '.join(sorted(missing))}")
        values = dict(payload)
        values["path"] = _path_from_json(payload["path"])
        cache_path = payload.get("source_cache_path")
        values["source_cache_path"] = (
            None if cache_path is None else _path_from_json(cache_path)
        )
        values["source_cache_status"] = MediaCacheStatus(
            payload.get("source_cache_status", MediaCacheStatus.EMPTY.value)
        )
        values["source_cache_error"] = payload.get("source_cache_error")
        return cls(**values)


@dataclass(frozen=True, slots=True)
class TimelineRecord:
    """One named stitch timeline and its independent state."""

    id: str
    name: str
    bin_id: str | None
    source_paths: tuple[PurePath, ...]
    config_snapshot: dict[str, Any]
    inherits_project_settings: bool = True
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
        if len(sources) not in (0, 3, 5):
            raise ProjectError("source_paths must be empty or contain 3 or 5 camera slots")
        if len(set(sources)) != len(sources):
            raise ProjectError("source_paths cannot assign one clip to multiple camera slots")
        if not isinstance(self.inherits_project_settings, bool):
            raise ProjectError("inherits_project_settings must be a boolean")
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
        inherits_project_settings: bool = True,
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
            inherits_project_settings=inherits_project_settings,
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
            "inherits_project_settings": self.inherits_project_settings,
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
    def from_dict(
        cls, payload: Mapping[str, Any], *, legacy: bool = False
    ) -> TimelineRecord:
        allowed = {
            "id", "name", "bin_id", "source_paths", "config_snapshot",
            "inherits_project_settings",
            "tc_alignment_snapshot", "in_frame", "out_frame", "playback_cache_path",
            "playback_cache_status", "stitch_status", "order", "created_at", "updated_at",
        }
        _require_keys(payload, allowed, field="timeline")
        missing_inheritance = "inherits_project_settings" not in payload
        missing = allowed - set(payload)
        if legacy and missing_inheritance:
            missing.remove("inherits_project_settings")
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
        values["inherits_project_settings"] = (
            False if legacy and missing_inheritance else payload["inherits_project_settings"]
        )
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
        media: Iterable[MediaRecord] = (),
        timelines: Iterable[TimelineRecord] = (),
        *,
        autosave: bool = True,
    ) -> None:
        self.path = Path(path)
        self.settings = settings
        self.autosave = autosave
        self._bins = list(bins)
        self._media = list(media)
        self._timelines = list(timelines)
        self.change_listener: (
            Callable[[dict[str, Any], dict[str, Any]], None] | None
        ) = None
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

    @property
    def media(self) -> tuple[MediaRecord, ...]:
        return tuple(self._media)

    def _validate(self) -> None:
        bin_ids = [item.id for item in self._bins]
        media_ids = [item.id for item in self._media]
        timeline_ids = [item.id for item in self._timelines]
        if len(bin_ids) != len(set(bin_ids)):
            raise ProjectError("project contains duplicate bin ids")
        if len(timeline_ids) != len(set(timeline_ids)):
            raise ProjectError("project contains duplicate timeline ids")
        if len(media_ids) != len(set(media_ids)):
            raise ProjectError("project contains duplicate media ids")
        media_paths = [str(item.path) for item in self._media]
        if len(media_paths) != len(set(media_paths)):
            raise ProjectError("project contains duplicate media paths")
        known_bins = set(bin_ids)
        for item in self._bins:
            if item.parent_id is not None and item.parent_id not in known_bins:
                raise ProjectError(f"unknown parent bin: {item.parent_id}")
            self._assert_no_bin_cycle(item.id, items=self._bins)
        for timeline in self._timelines:
            if timeline.bin_id is not None and timeline.bin_id not in known_bins:
                raise ProjectError(f"unknown timeline bin: {timeline.bin_id}")
        for item in self._media:
            if item.bin_id is not None and item.bin_id not in known_bins:
                raise ProjectError(f"unknown media bin: {item.bin_id}")
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
        for bin_id in {item.bin_id for item in self._media}:
            orders = [item.order for item in self._media if item.bin_id == bin_id]
            if len(orders) != len(set(orders)):
                raise ProjectError("sibling media contain duplicate order values")

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
        old_media = list(self._media)
        old_timelines = list(self._timelines)
        before = self.to_dict() if self.change_listener is not None else None
        try:
            result = operation()
            self._validate()
            if self.autosave:
                self.save()
            if self.change_listener is not None and before is not None:
                try:
                    self.change_listener(before, self.to_dict())
                except Exception:
                    # Project persistence must not fail because an optional UI
                    # observer (for example undo history) could not refresh.
                    pass
            return result
        except Exception:
            self.settings = old_settings
            self._bins = old_bins
            self._media = old_media
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
            occupied = any(item.bin_id in descendants for item in self._timelines) or any(
                item.bin_id in descendants for item in self._media
            )
            if (len(descendants) > 1 or occupied) and not recursive:
                raise ProjectError("bin is not empty; use recursive=True")
            self._bins = [item for item in self._bins if item.id not in descendants]
            if recursive:
                self._timelines = [item for item in self._timelines if item.bin_id not in descendants]
                self._media = [item for item in self._media if item.bin_id not in descendants]
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

    def list_media(self, bin_id: str | None = None) -> tuple[MediaRecord, ...]:
        return tuple(
            sorted(
                (item for item in self._media if item.bin_id == bin_id),
                key=lambda item: item.order,
            )
        )

    def add_media(self, item: MediaRecord) -> MediaRecord:
        return self.add_media_many((item,))[0]

    def add_media_many(self, items: Iterable[MediaRecord]) -> tuple[MediaRecord, ...]:
        requested = tuple(items)

        def change() -> tuple[MediaRecord, ...]:
            ids = {existing.id for existing in self._media}
            paths = {str(existing.path) for existing in self._media}
            bin_ids = {existing.id for existing in self._bins}
            added: list[MediaRecord] = []
            for item in requested:
                if item.id in ids:
                    raise ProjectError(f"duplicate media id: {item.id}")
                if str(item.path) in paths:
                    raise ProjectError(f"media already exists: {item.path}")
                if item.bin_id is not None and item.bin_id not in bin_ids:
                    raise ProjectError(f"unknown media bin: {item.bin_id}")
                siblings = self.list_media(item.bin_id)
                index = min(item.order, len(siblings))
                self._media = [
                    replace(existing, order=existing.order + 1)
                    if existing.bin_id == item.bin_id and existing.order >= index
                    else existing
                    for existing in self._media
                ]
                record = replace(item, order=index)
                self._media.append(record)
                added.append(record)
                ids.add(record.id)
                paths.add(str(record.path))
            return tuple(added)

        return self._mutate(change)

    def update_media(self, media_id: str, **changes: Any) -> MediaRecord:
        allowed = {
            "path",
            "bin_id",
            "order",
            "source_cache_path",
            "source_cache_status",
            "source_cache_error",
        }
        unknown = set(changes) - allowed
        if unknown:
            raise ProjectError(
                f"unsupported media fields: {', '.join(sorted(unknown))}"
            )

        def change() -> MediaRecord:
            index = self._media_index(media_id)
            current = self._media[index]
            updated = replace(current, **changes)
            if any(
                other.id != media_id and str(other.path) == str(updated.path)
                for other in self._media
            ):
                raise ProjectError(f"media already exists: {updated.path}")
            if updated.bin_id is not None and not any(
                item.id == updated.bin_id for item in self._bins
            ):
                raise ProjectError(f"unknown media bin: {updated.bin_id}")
            self._media[index] = updated
            return updated

        return self._mutate(change)

    def move_media_many(
        self,
        media_ids: Iterable[str],
        bin_id: str | None,
        index: int | None = None,
    ) -> tuple[MediaRecord, ...]:
        """Move media as one persisted operation while preserving drag order."""
        requested = tuple(dict.fromkeys(str(media_id) for media_id in media_ids))
        if not requested:
            return ()

        def change() -> tuple[MediaRecord, ...]:
            if bin_id is not None and not any(item.id == bin_id for item in self._bins):
                raise ProjectError(f"unknown media bin: {bin_id}")
            current_by_id = {item.id: item for item in self._media}
            missing = [media_id for media_id in requested if media_id not in current_by_id]
            if missing:
                raise ProjectError(f"unknown media: {missing[0]}")
            moving = [current_by_id[media_id] for media_id in requested]
            moving_ids = set(requested)
            old_bin_ids = {item.bin_id for item in moving}
            self._media = [item for item in self._media if item.id not in moving_ids]
            for old_bin_id in old_bin_ids:
                self._normalize_media_orders(old_bin_id)
            siblings = self.list_media(bin_id)
            target = (
                len(siblings)
                if index is None
                else self._checked_index(index, len(siblings))
            )
            offset = len(moving)
            self._media = [
                replace(item, order=item.order + offset)
                if item.bin_id == bin_id and item.order >= target
                else item
                for item in self._media
            ]
            moved = tuple(
                replace(item, bin_id=bin_id, order=target + item_index)
                for item_index, item in enumerate(moving)
            )
            self._media.extend(moved)
            return moved

        return self._mutate(change)

    def move_media(
        self, media_id: str, bin_id: str | None, index: int | None = None
    ) -> MediaRecord:
        return self.move_media_many((media_id,), bin_id, index)[0]

    def reorder_media(self, media_id: str, index: int) -> MediaRecord:
        item = self._media[self._media_index(media_id)]
        return self.move_media(media_id, item.bin_id, index)

    def media_for_paths(self, paths: Iterable[str | PurePath]) -> tuple[MediaRecord, ...]:
        wanted = {str(_coerce_path(path)) for path in paths}
        return tuple(item for item in self._media if str(item.path) in wanted)

    def remove_media(self, media_id: str) -> MediaRecord:
        def change() -> MediaRecord:
            index = self._media_index(media_id)
            removed = self._media.pop(index)
            self._normalize_media_orders(removed.bin_id)
            return removed
        return self._mutate(change)

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

    def _media_index(self, media_id: str) -> int:
        for index, item in enumerate(self._media):
            if item.id == media_id:
                return index
        raise ProjectError(f"unknown media: {media_id}")

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

    def _normalize_media_orders(self, bin_id: str | None) -> None:
        ordered = sorted(
            (item for item in self._media if item.bin_id == bin_id),
            key=lambda item: item.order,
        )
        replacements = {
            item.id: replace(item, order=index) for index, item in enumerate(ordered)
        }
        self._media = [replacements.get(item.id, item) for item in self._media]

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": STORE_VERSION,
            "settings": self.settings.to_dict(),
            "bins": [item.to_dict() for item in self._bins],
            "media": [item.to_dict() for item in self._media],
            "timelines": [item.to_dict() for item in self._timelines],
        }

    def _save_to(self, destination: Path) -> None:
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=destination.parent,
                prefix=f".{destination.name}.",
                suffix=".tmp",
                delete=False,
            ) as handle:
                temporary = Path(handle.name)
                json.dump(self.to_dict(), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
            temporary = None
            try:
                directory_fd = os.open(destination.parent, os.O_RDONLY)
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
            except OSError:
                pass
        except (OSError, TypeError, ValueError) as exc:
            raise ProjectError(f"cannot save project {destination}: {exc}") from exc
        finally:
            if temporary is not None:
                try:
                    temporary.unlink()
                except FileNotFoundError:
                    pass

    def save(self) -> None:
        self._save_to(self.path)

    def save_copy(self, destination: str | PurePath) -> Path:
        """Write an atomic recovery copy without changing the active project path."""
        target = Path(destination)
        self._save_to(target)
        return target

    @classmethod
    def from_dict(
        cls,
        path: str | PurePath,
        payload: Mapping[str, Any],
        *,
        autosave: bool = True,
    ) -> ProjectStore:
        """Build a validated store from an in-memory project snapshot."""
        store_path = Path(path)
        if not isinstance(payload, Mapping):
            raise ProjectError("project root must be an object")
        version = payload.get("version")
        if version not in {1, STORE_VERSION}:
            raise ProjectError(f"unsupported project version: {payload.get('version')!r}")
        allowed = {"version", "settings", "bins", "timelines"}
        if version == STORE_VERSION:
            allowed.add("media")
        _require_keys(payload, allowed, field="project")
        if set(payload) != allowed:
            raise ProjectError("project is missing required fields")
        settings = payload["settings"]
        bins = payload["bins"]
        media = payload.get("media", [])
        timelines = payload["timelines"]
        if not isinstance(settings, Mapping) or not isinstance(bins, list) or not isinstance(media, list) or not isinstance(timelines, list):
            raise ProjectError("project settings must be an object; bins, media and timelines must be lists")
        if not all(isinstance(item, Mapping) for item in bins + media + timelines):
            raise ProjectError("project bins, media and timelines must contain objects")
        loaded_timelines = [
            TimelineRecord.from_dict(item, legacy=version == 1) for item in timelines
        ]
        loaded_media = [MediaRecord.from_dict(item) for item in media]
        if version == 1:
            known_paths: set[str] = set()
            next_order: dict[str | None, int] = {}
            for timeline in loaded_timelines:
                for path in timeline.source_paths:
                    key = str(path)
                    if key in known_paths:
                        continue
                    bin_id = timeline.bin_id
                    order = next_order.get(bin_id, 0)
                    loaded_media.append(
                        MediaRecord.create(path, bin_id=bin_id, order=order)
                    )
                    next_order[bin_id] = order + 1
                    known_paths.add(key)
        return cls(
            store_path,
            ProjectSettings.from_dict(settings),
            [Bin.from_dict(item) for item in bins],
            loaded_media,
            loaded_timelines,
            autosave=autosave,
        )

    @classmethod
    def load(cls, path: str | PurePath, *, autosave: bool = True) -> ProjectStore:
        store_path = Path(path)
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ProjectError(f"cannot load project {store_path}: {exc}") from exc
        return cls.from_dict(store_path, payload, autosave=autosave)
