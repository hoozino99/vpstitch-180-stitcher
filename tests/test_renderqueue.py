from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pytest

from vpstitch.renderqueue import (
    RenderJob,
    RenderQueueError,
    RenderQueueStore,
    RenderStatus,
)


def make_job(name: str, *, status: RenderStatus = RenderStatus.QUEUED) -> RenderJob:
    return RenderJob.create(
        name=name,
        source_paths=[Path("plates") / f"{name}-P01.mov", Path("plates") / f"{name}-P02.mov"],
        config_snapshot={
            "output": {"width": 20_000, "height": 5_504},
            "ocio": {"config": Path("color") / "studio.ocio"},
            "portable_relative": PureWindowsPath(r"color\studio.ocio"),
        },
        tc_alignment_snapshot={"fps": 24.0, "common_frames": 240},
        tc_alignment_path=Path("alignments") / f"{name}.json",
        in_frame=12,
        out_frame=120,
        output_path=Path("renders") / f"{name}.mov",
        status=status,
        job_id=f"job-{name}",
    )


def test_job_round_trip_keeps_snapshots_ranges_and_paths(tmp_path: Path) -> None:
    queue_path = tmp_path / "state" / "render-queue.json"
    store = RenderQueueStore(queue_path)
    original = make_job("야간 테이크 01")

    store.add(original)
    loaded = RenderQueueStore.load(queue_path)

    assert loaded.jobs == (original,)
    job = loaded.jobs[0]
    assert job.in_frame == 12
    assert job.out_frame == 120
    assert job.tc_alignment_snapshot == {"fps": 24.0, "common_frames": 240}
    assert job.tc_alignment_path == Path("alignments") / "야간 테이크 01.json"
    assert job.config_snapshot["ocio"]["config"] == "color/studio.ocio"
    assert job.config_snapshot["portable_relative"] == "color/studio.ocio"
    assert json.loads(queue_path.read_text(encoding="utf-8"))["version"] == 1


def test_completed_render_duration_persists_and_validates(tmp_path: Path) -> None:
    queue_path = tmp_path / "render-queue.json"
    store = RenderQueueStore(queue_path)
    job = store.add(make_job("duration"))
    store.update(
        job.id,
        status=RenderStatus.DONE,
        elapsed_seconds=3_661.4,
    )

    loaded = RenderQueueStore.load(queue_path).jobs[0]
    assert loaded.status is RenderStatus.DONE
    assert loaded.elapsed_seconds == pytest.approx(3_661.4)
    assert json.loads(queue_path.read_text(encoding="utf-8"))["jobs"][0][
        "elapsed_seconds"
    ] == pytest.approx(3_661.4)

    with pytest.raises(RenderQueueError, match="elapsed_seconds"):
        RenderJob.create(
            name="invalid duration",
            source_paths=["P01.mov"],
            config_snapshot={},
            output_path="invalid.mov",
            elapsed_seconds=float("inf"),
        )


def test_job_snapshot_is_detached_from_later_nested_config_changes() -> None:
    config = {
        "cameras": [
            {
                "name": "cam0",
                "yaw_deg": -42.5,
                "crop_left": 0.03,
                "color_gain": [1.02, 1.0, 0.98],
            }
        ],
        "output": {"width": 20_000, "height": 5_504},
        "color": {"match_strength": 0.8},
    }
    job = RenderJob.create(
        name="locked",
        source_paths=["P01.mov"],
        config_snapshot=config,
        output_path="locked.mov",
    )

    config["cameras"][0]["yaw_deg"] = 75.0
    config["cameras"][0]["color_gain"][0] = 0.5
    config["output"]["width"] = 1_920
    config["color"]["match_strength"] = 0.1

    assert job.config_snapshot["cameras"][0]["yaw_deg"] == -42.5
    assert job.config_snapshot["cameras"][0]["color_gain"] == [1.02, 1.0, 0.98]
    assert job.config_snapshot["output"]["width"] == 20_000
    assert job.config_snapshot["color"]["match_strength"] == 0.8


def test_store_does_not_expose_mutable_internal_job_snapshots(tmp_path: Path) -> None:
    store = RenderQueueStore(tmp_path / "queue.json")
    original = make_job("isolated")
    returned = store.add(original)
    digest = returned.snapshot_digest

    original.config_snapshot["output"]["width"] = 1
    returned.config_snapshot["output"]["width"] = 2
    exposed = store.jobs[0]
    exposed.config_snapshot["output"]["width"] = 3

    locked = store.jobs[0]
    assert locked.config_snapshot["output"]["width"] == 20_000
    assert locked.snapshot_digest == digest
    assert RenderQueueStore.load(store.path).jobs[0].snapshot_digest == digest


def test_store_add_update_remove_reorder_and_next_queued(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    store = RenderQueueStore(queue_path)
    first = make_job("first", status=RenderStatus.DONE)
    second = make_job("second")
    third = make_job("third")

    store.add(first)
    store.add(second)
    store.add(third)
    assert store.next_queued() == second

    store.reorder(third.id, 0)
    assert [job.id for job in store.jobs] == [third.id, first.id, second.id]
    assert store.next_queued() == third

    updated = store.update(third.id, name="third renamed", status="failed", error="disk full")
    assert updated.status is RenderStatus.FAILED
    assert updated.error == "disk full"
    assert store.next_queued() == second

    assert store.remove(first.id) == first
    reloaded = RenderQueueStore.load(queue_path)
    assert [job.id for job in reloaded.jobs] == [third.id, second.id]
    assert reloaded.jobs[0].name == "third renamed"


def test_load_recovers_rendering_jobs_and_persists_recovery(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    RenderQueueStore(queue_path, [make_job("interrupted", status=RenderStatus.RENDERING)]).save()

    recovered = RenderQueueStore.load(queue_path)

    assert recovered.jobs[0].status is RenderStatus.QUEUED
    saved_status = json.loads(queue_path.read_text(encoding="utf-8"))["jobs"][0]["status"]
    assert saved_status == "queued"


def test_paths_are_serialized_without_resolving_or_platform_rewriting(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    windows_source = PureWindowsPath(r"D:\Shoot 01\P01.mov")
    windows_output = PureWindowsPath(r"E:\Renders\take.mov")
    job = RenderJob.create(
        name="portable",
        source_paths=[windows_source],
        config_snapshot={},
        output_path=windows_output,
        job_id="portable-job",
    )

    RenderQueueStore(queue_path, [job]).save()
    raw = json.loads(queue_path.read_text(encoding="utf-8"))["jobs"][0]

    assert raw["source_paths"] == [str(windows_source)]
    assert raw["output_path"] == str(windows_output)
    loaded = RenderQueueStore.load(queue_path).jobs[0]
    assert loaded.source_paths[0] == windows_source
    assert loaded.output_path == windows_output


def test_atomic_save_keeps_previous_file_when_replace_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    queue_path = tmp_path / "queue.json"
    original = RenderQueueStore(queue_path, [make_job("safe")], autosave=False)
    original.save()
    previous = queue_path.read_bytes()
    changed = RenderQueueStore(queue_path, [make_job("changed")], autosave=False)

    def fail_replace(source: object, destination: object) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr("vpstitch.renderqueue.os.replace", fail_replace)
    with pytest.raises(RenderQueueError, match="simulated replace failure"):
        changed.save()

    assert queue_path.read_bytes() == previous
    assert list(tmp_path.glob(".queue.json.*.tmp")) == []


def test_validation_rejects_invalid_ranges_duplicates_and_unknown_status(tmp_path: Path) -> None:
    with pytest.raises(RenderQueueError, match="out_frame"):
        RenderJob.create(
            name="bad range",
            source_paths=["P01.mov"],
            config_snapshot={},
            output_path="out.mov",
            in_frame=10,
            out_frame=10,
        )
    with pytest.raises(ValueError):
        RenderJob.create(
            name="bad status",
            source_paths=["P01.mov"],
            config_snapshot={},
            output_path="out.mov",
            status="paused",
        )

    job = make_job("duplicate")
    store = RenderQueueStore(tmp_path / "queue.json", [job], autosave=False)
    with pytest.raises(RenderQueueError, match="duplicate"):
        store.add(job)


def test_missing_store_loads_as_empty_and_manual_save_mode_is_supported(tmp_path: Path) -> None:
    queue_path = tmp_path / "queue.json"
    store = RenderQueueStore.load(queue_path, autosave=False)

    assert store.jobs == ()
    assert store.next_queued() is None
    store.add(make_job("manual"))
    assert not queue_path.exists()
    store.save()
    assert queue_path.exists()
