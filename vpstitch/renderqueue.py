from __future__ import annotations

import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass, replace
from enum import Enum
from pathlib import Path, PurePath, PurePosixPath, PureWindowsPath
from typing import Any, Iterable, Mapping


STORE_VERSION = 1
_WINDOWS_PATH = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


class RenderQueueError(ValueError):
    """Raised when a render queue or job contains invalid data."""


class RenderStatus(str, Enum):
    QUEUED = "queued"
    RENDERING = "rendering"
    DONE = "done"
    FAILED = "failed"


def _json_value(value: Any, *, field: str) -> Any:
    """Return a detached JSON-safe value, serializing pathlib objects as strings."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, PurePath):
        return str(value)
    if isinstance(value, Enum):
        return _json_value(value.value, field=field)
    if isinstance(value, Mapping):
        converted: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise RenderQueueError(f"{field} keys must be strings")
            converted[key] = _json_value(item, field=field)
        return converted
    if isinstance(value, (list, tuple)):
        return [_json_value(item, field=field) for item in value]
    raise RenderQueueError(f"{field} contains a non-JSON value: {type(value).__name__}")


def _snapshot(value: Mapping[str, Any] | None, *, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    converted = _json_value(value, field=field)
    assert isinstance(converted, dict)
    return converted


def _coerce_path(value: str | PurePath) -> PurePath:
    return value if isinstance(value, PurePath) else Path(value)


def _path_from_json(value: Any) -> PurePath:
    text = str(value)
    if _WINDOWS_PATH.match(text) or "\\" in text:
        return PureWindowsPath(text)
    if text.startswith("/"):
        return PurePosixPath(text)
    return Path(text)


@dataclass(frozen=True, slots=True)
class RenderJob:
    """A self-contained snapshot of one timeline render.

    ``in_frame`` is inclusive and ``out_frame`` is exclusive. An ``out_frame`` of
    ``None`` means render to the end of the aligned common range.
    """

    id: str
    name: str
    source_paths: tuple[PurePath, ...]
    config_snapshot: dict[str, Any]
    output_path: PurePath
    tc_alignment_snapshot: dict[str, Any] | None = None
    tc_alignment_path: PurePath | None = None
    in_frame: int = 0
    out_frame: int | None = None
    status: RenderStatus = RenderStatus.QUEUED
    error: str | None = None

    def __post_init__(self) -> None:
        job_id = str(self.id).strip()
        name = str(self.name).strip()
        if not job_id:
            raise RenderQueueError("job id cannot be empty")
        if not name:
            raise RenderQueueError("job name cannot be empty")

        sources = tuple(_coerce_path(path) for path in self.source_paths)
        if not sources:
            raise RenderQueueError("a render job requires at least one source path")
        output = _coerce_path(self.output_path)
        if not str(output):
            raise RenderQueueError("output path cannot be empty")
        if isinstance(self.in_frame, bool) or not isinstance(self.in_frame, int):
            raise RenderQueueError("in_frame must be an integer")
        if self.in_frame < 0:
            raise RenderQueueError("in_frame cannot be negative")
        if self.out_frame is not None:
            if isinstance(self.out_frame, bool) or not isinstance(self.out_frame, int):
                raise RenderQueueError("out_frame must be an integer or None")
            if self.out_frame <= self.in_frame:
                raise RenderQueueError("out_frame must be greater than in_frame")
        try:
            status = RenderStatus(self.status)
        except ValueError as exc:
            raise RenderQueueError(f"invalid render status: {self.status!r}") from exc

        error = None if self.error is None else str(self.error)
        alignment_path = (
            None if self.tc_alignment_path is None else _coerce_path(self.tc_alignment_path)
        )
        object.__setattr__(self, "id", job_id)
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "source_paths", sources)
        object.__setattr__(
            self,
            "config_snapshot",
            _snapshot(self.config_snapshot, field="config_snapshot") or {},
        )
        object.__setattr__(
            self,
            "tc_alignment_snapshot",
            _snapshot(self.tc_alignment_snapshot, field="tc_alignment_snapshot"),
        )
        object.__setattr__(self, "tc_alignment_path", alignment_path)
        object.__setattr__(self, "output_path", output)
        object.__setattr__(self, "status", status)
        object.__setattr__(self, "error", error)

    @classmethod
    def create(
        cls,
        *,
        name: str,
        source_paths: Iterable[str | PurePath],
        config_snapshot: Mapping[str, Any],
        output_path: str | PurePath,
        tc_alignment_snapshot: Mapping[str, Any] | None = None,
        tc_alignment_path: str | PurePath | None = None,
        in_frame: int = 0,
        out_frame: int | None = None,
        status: RenderStatus | str = RenderStatus.QUEUED,
        error: str | None = None,
        job_id: str | None = None,
    ) -> RenderJob:
        return cls(
            id=job_id or uuid.uuid4().hex,
            name=name,
            source_paths=tuple(_coerce_path(path) for path in source_paths),
            config_snapshot=dict(config_snapshot),
            output_path=_coerce_path(output_path),
            tc_alignment_snapshot=(
                None if tc_alignment_snapshot is None else dict(tc_alignment_snapshot)
            ),
            tc_alignment_path=(
                None if tc_alignment_path is None else _coerce_path(tc_alignment_path)
            ),
            in_frame=in_frame,
            out_frame=out_frame,
            status=RenderStatus(status),
            error=error,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source_paths": [str(path) for path in self.source_paths],
            "config_snapshot": _json_value(
                self.config_snapshot, field="config_snapshot"
            ),
            "tc_alignment_snapshot": _json_value(
                self.tc_alignment_snapshot, field="tc_alignment_snapshot"
            ),
            "tc_alignment_path": (
                None if self.tc_alignment_path is None else str(self.tc_alignment_path)
            ),
            "in_frame": self.in_frame,
            "out_frame": self.out_frame,
            "output_path": str(self.output_path),
            "status": self.status.value,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, payload: Mapping[str, Any]) -> RenderJob:
        try:
            sources = payload["source_paths"]
            if not isinstance(sources, list):
                raise RenderQueueError("source_paths must be a list")
            config = payload["config_snapshot"]
            if not isinstance(config, Mapping):
                raise RenderQueueError("config_snapshot must be an object")
            alignment = payload.get("tc_alignment_snapshot")
            if alignment is not None and not isinstance(alignment, Mapping):
                raise RenderQueueError("tc_alignment_snapshot must be an object or null")
            return cls(
                id=str(payload["id"]),
                name=str(payload["name"]),
                source_paths=tuple(_path_from_json(path) for path in sources),
                config_snapshot=dict(config),
                output_path=_path_from_json(payload["output_path"]),
                tc_alignment_snapshot=None if alignment is None else dict(alignment),
                tc_alignment_path=(
                    None
                    if payload.get("tc_alignment_path") is None
                    else _path_from_json(payload["tc_alignment_path"])
                ),
                in_frame=payload.get("in_frame", 0),
                out_frame=payload.get("out_frame"),
                status=RenderStatus(payload.get("status", RenderStatus.QUEUED.value)),
                error=payload.get("error"),
            )
        except KeyError as exc:
            raise RenderQueueError(f"render job is missing field: {exc.args[0]}") from exc
        except (TypeError, ValueError) as exc:
            if isinstance(exc, RenderQueueError):
                raise
            raise RenderQueueError(f"invalid render job: {exc}") from exc


class RenderQueueStore:
    """Ordered JSON-backed render queue with atomic, immediate persistence."""

    def __init__(
        self,
        path: str | PurePath,
        jobs: Iterable[RenderJob] = (),
        *,
        autosave: bool = True,
    ) -> None:
        self.path = Path(path)
        self.autosave = autosave
        self._jobs = list(jobs)
        self._validate_unique_ids()

    @property
    def jobs(self) -> tuple[RenderJob, ...]:
        return tuple(self._jobs)

    def _validate_unique_ids(self) -> None:
        ids = [job.id for job in self._jobs]
        if len(ids) != len(set(ids)):
            raise RenderQueueError("render queue contains duplicate job ids")

    @classmethod
    def load(
        cls, path: str | PurePath, *, autosave: bool = True
    ) -> RenderQueueStore:
        store_path = Path(path)
        if not store_path.exists():
            return cls(store_path, autosave=autosave)
        try:
            payload = json.loads(store_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RenderQueueError(f"cannot load render queue {store_path}: {exc}") from exc
        if not isinstance(payload, Mapping):
            raise RenderQueueError("render queue root must be an object")
        if payload.get("version") != STORE_VERSION:
            raise RenderQueueError(
                f"unsupported render queue version: {payload.get('version')!r}"
            )
        raw_jobs = payload.get("jobs")
        if not isinstance(raw_jobs, list):
            raise RenderQueueError("render queue jobs must be a list")

        jobs: list[RenderJob] = []
        recovered = False
        for raw_job in raw_jobs:
            if not isinstance(raw_job, Mapping):
                raise RenderQueueError("each render queue job must be an object")
            job = RenderJob.from_dict(raw_job)
            if job.status is RenderStatus.RENDERING:
                job = replace(job, status=RenderStatus.QUEUED)
                recovered = True
            jobs.append(job)
        store = cls(store_path, jobs, autosave=autosave)
        if recovered and autosave:
            store.save()
        return store

    def save(self) -> None:
        payload = {
            "version": STORE_VERSION,
            "jobs": [job.to_dict() for job in self._jobs],
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as temporary:
                temporary_path = Path(temporary.name)
                json.dump(payload, temporary, ensure_ascii=False, indent=2)
                temporary.write("\n")
                temporary.flush()
                os.fsync(temporary.fileno())
            os.replace(temporary_path, self.path)
            temporary_path = None
        except OSError as exc:
            raise RenderQueueError(f"cannot save render queue {self.path}: {exc}") from exc
        finally:
            if temporary_path is not None:
                try:
                    temporary_path.unlink()
                except FileNotFoundError:
                    pass

    def _persist(self) -> None:
        if self.autosave:
            self.save()

    def _index(self, job_id: str) -> int:
        for index, job in enumerate(self._jobs):
            if job.id == job_id:
                return index
        raise KeyError(job_id)

    def add(self, job: RenderJob) -> RenderJob:
        if any(existing.id == job.id for existing in self._jobs):
            raise RenderQueueError(f"duplicate render job id: {job.id}")
        self._jobs.append(job)
        self._persist()
        return job

    def remove(self, job_id: str) -> RenderJob:
        removed = self._jobs.pop(self._index(job_id))
        self._persist()
        return removed

    def update(self, job_id: str, **changes: Any) -> RenderJob:
        if "id" in changes and changes["id"] != job_id:
            raise RenderQueueError("a render job id cannot be changed")
        index = self._index(job_id)
        if "status" in changes:
            changes["status"] = RenderStatus(changes["status"])
        updated = replace(self._jobs[index], **changes)
        self._jobs[index] = updated
        self._persist()
        return updated

    def reorder(self, job_id: str, new_index: int) -> RenderJob:
        if isinstance(new_index, bool) or not isinstance(new_index, int):
            raise TypeError("new_index must be an integer")
        if not self._jobs:
            raise KeyError(job_id)
        old_index = self._index(job_id)
        bounded_index = max(0, min(new_index, len(self._jobs) - 1))
        job = self._jobs.pop(old_index)
        self._jobs.insert(bounded_index, job)
        self._persist()
        return job

    def next_queued(self) -> RenderJob | None:
        return next(
            (job for job in self._jobs if job.status is RenderStatus.QUEUED),
            None,
        )
