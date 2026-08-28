from __future__ import annotations

import json
from pathlib import Path, PureWindowsPath

import pytest

from vpstitch.project import (
    Bin,
    MediaCacheStatus,
    MediaRecord,
    PlaybackCacheStatus,
    ProjectError,
    ProjectStore,
    StitchStatus,
    TimelineRecord,
)


def plate_paths(numbers: tuple[int, ...], *, windows: bool = False):
    if windows:
        return [PureWindowsPath(fr"D:\Shoot\Take_01_P{number:02d}.mov") for number in numbers]
    return [Path("plates") / f"Take_01_P{number:02d}.mov" for number in numbers]


def timeline(name: str, bin_id: str | None, numbers=(1, 2, 3, 4, 5), order=0):
    return TimelineRecord.create(
        name=name,
        timeline_id=f"timeline-{name}",
        bin_id=bin_id,
        source_paths=plate_paths(numbers),
        config_snapshot={"output": {"width": 8192}, "ocio": Path("color/studio.ocio")},
        tc_alignment_snapshot={"common_frames": 240},
        in_frame=12,
        out_frame=120,
        playback_cache_path=Path("cache") / f"{name}.mp4",
        playback_cache_status=PlaybackCacheStatus.READY,
        stitch_status=StitchStatus.READY,
        order=order,
    )


def test_round_trip_preserves_project_snapshots_statuses_and_cross_platform_paths(tmp_path: Path):
    project_path = tmp_path / "project" / "project.json"
    store = ProjectStore.create(
        project_path,
        name="서울 촬영",
        settings_snapshot={
            "canvas": [8192, 2252],
            "ocio": Path("config/studio.ocio"),
            "portable_relative": PureWindowsPath(r"config\studio.ocio"),
        },
    )
    shoot = store.add_bin(Bin.create("Location A", bin_id="bin-a"))
    media = store.add_media(
        MediaRecord.create(
            plate_paths((6,), windows=True)[0],
            bin_id=shoot.id,
            media_id="media-p06",
        )
    )
    original = TimelineRecord.create(
        name="Front 3 Cam",
        timeline_id="front-01",
        bin_id=shoot.id,
        source_paths=plate_paths((6, 7, 8), windows=True),
        config_snapshot={"rig": "front_3cam"},
        tc_alignment_snapshot=None,
        playback_cache_path=PureWindowsPath(r"E:\Cache\front.mp4"),
        playback_cache_status="ready",
        stitch_status="ready",
    )
    store.add_timeline(original)

    loaded = ProjectStore.load(project_path)

    assert loaded.settings.name == "서울 촬영"
    assert loaded.settings.settings_snapshot["ocio"] == "config/studio.ocio"
    assert loaded.settings.settings_snapshot["portable_relative"] == "config/studio.ocio"
    assert loaded.list_bins() == (shoot,)
    assert loaded.list_media(shoot.id) == (media,)
    assert loaded.list_timelines(shoot.id) == (original,)
    assert loaded.timelines[0].source_paths == tuple(plate_paths((6, 7, 8), windows=True))
    assert loaded.timelines[0].playback_cache_path == PureWindowsPath(r"E:\Cache\front.mp4")
    raw = json.loads(project_path.read_text(encoding="utf-8"))
    assert raw["version"] == 2
    assert raw["media"][0]["id"] == "media-p06"
    assert "root" not in raw["settings"]


def test_add_media_many_persists_import_batch_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore.create(tmp_path / "project.json", name="Batch")
    folder = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    saves = 0
    real_save = store.save

    def tracked_save() -> None:
        nonlocal saves
        saves += 1
        real_save()

    monkeypatch.setattr(store, "save", tracked_save)
    records = tuple(
        MediaRecord.create(
            tmp_path / f"P{number:02d}.mov",
            bin_id=folder.id,
            order=index,
        )
        for index, number in enumerate((1, 2, 3, 4, 5))
    )

    added = store.add_media_many(records)

    assert saves == 1
    assert added == store.list_media(folder.id)


def test_bin_hierarchy_move_reorder_and_recursive_remove(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project.json", name="Hierarchy")
    a = store.add_bin(Bin.create("A", bin_id="a"))
    b = store.add_bin(Bin.create("B", bin_id="b", order=1))
    child = store.add_bin(Bin.create("Child", parent_id=a.id, bin_id="child"))
    store.add_timeline(timeline("nested", child.id))

    store.reorder_bin(b.id, 0)
    assert [item.id for item in store.list_bins()] == ["b", "a"]
    with pytest.raises(ProjectError, match="descendant"):
        store.move_bin(a.id, child.id)
    store.move_bin(child.id, b.id)
    assert store.list_bins(b.id)[0].id == child.id
    with pytest.raises(ProjectError, match="not empty"):
        store.remove_bin(b.id)
    store.remove_bin(b.id, recursive=True)
    assert [item.id for item in store.list_bins()] == ["a"]
    assert store.timelines == ()


def test_remove_media_keeps_source_file_and_normalizes_sibling_order(
    tmp_path: Path,
) -> None:
    store = ProjectStore.create(tmp_path / "project.json", name="Media")
    folder = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    paths = [tmp_path / f"P{number:02d}.mov" for number in (6, 7, 8)]
    for path in paths:
        path.touch()
    records = [
        store.add_media(MediaRecord.create(path, bin_id=folder.id, order=index))
        for index, path in enumerate(paths)
    ]

    removed = store.remove_media(records[1].id)

    assert removed.path == paths[1]
    assert paths[1].is_file()
    assert [item.order for item in store.list_media(folder.id)] == [0, 1]
    assert [item.path for item in store.list_media(folder.id)] == [paths[0], paths[2]]


def test_move_media_many_changes_folder_and_order_in_one_save(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = ProjectStore.create(tmp_path / "project.json", name="Move Media")
    source = store.add_bin(Bin.create("Source", bin_id="source"))
    target = store.add_bin(Bin.create("Target", bin_id="target", order=1))
    source_records = [
        store.add_media(
            MediaRecord.create(
                tmp_path / f"P{number:02d}.mov",
                bin_id=source.id,
                media_id=f"p{number:02d}",
                order=index,
            )
        )
        for index, number in enumerate((1, 2, 3))
    ]
    existing = store.add_media(
        MediaRecord.create(
            tmp_path / "existing.mov",
            bin_id=target.id,
            media_id="existing",
        )
    )
    saves = 0
    real_save = store.save

    def tracked_save() -> None:
        nonlocal saves
        saves += 1
        real_save()

    monkeypatch.setattr(store, "save", tracked_save)

    moved = store.move_media_many(
        (source_records[2].id, source_records[0].id),
        target.id,
        0,
    )

    assert saves == 1
    assert [item.id for item in moved] == ["p03", "p01"]
    assert [item.id for item in store.list_media(source.id)] == ["p02"]
    assert [item.id for item in store.list_media(target.id)] == [
        "p03",
        "p01",
        existing.id,
    ]
    assert [item.order for item in store.list_media(target.id)] == [0, 1, 2]


def test_reorder_media_uses_final_sibling_index(tmp_path: Path) -> None:
    store = ProjectStore.create(tmp_path / "project.json", name="Reorder Media")
    folder = store.add_bin(Bin.create("Shoot", bin_id="shoot"))
    for index, number in enumerate((1, 2, 3)):
        store.add_media(
            MediaRecord.create(
                tmp_path / f"P{number:02d}.mov",
                bin_id=folder.id,
                media_id=f"p{number:02d}",
                order=index,
            )
        )

    store.reorder_media("p01", 2)

    assert [item.id for item in store.list_media(folder.id)] == [
        "p02",
        "p03",
        "p01",
    ]


def test_media_source_cache_fields_round_trip_and_update(tmp_path: Path) -> None:
    project_path = tmp_path / "project.json"
    store = ProjectStore.create(project_path, name="Source Cache")
    source = tmp_path / "P01.mov"
    source.touch()
    media = store.add_media(MediaRecord.create(source, media_id="p01"))
    cache = tmp_path / "cache" / "p01.mp4"

    updated = store.update_media(
        media.id,
        source_cache_path=cache,
        source_cache_status=MediaCacheStatus.READY,
        source_cache_error=None,
    )
    loaded = ProjectStore.load(project_path).media[0]

    assert updated.source_cache_status is MediaCacheStatus.READY
    assert loaded.source_cache_path == cache
    assert loaded.source_cache_status is MediaCacheStatus.READY
    assert loaded.source_cache_error is None


def test_legacy_media_payload_defaults_to_empty_source_cache(tmp_path: Path) -> None:
    project_path = tmp_path / "project.json"
    store = ProjectStore.create(project_path, name="Legacy Cache")
    source = tmp_path / "P01.mov"
    source.touch()
    store.add_media(MediaRecord.create(source, media_id="p01"))
    payload = json.loads(project_path.read_text(encoding="utf-8"))
    for key in (
        "source_cache_path",
        "source_cache_status",
        "source_cache_error",
    ):
        payload["media"][0].pop(key)
    project_path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = ProjectStore.load(project_path).media[0]

    assert loaded.source_cache_path is None
    assert loaded.source_cache_status is MediaCacheStatus.EMPTY
    assert loaded.source_cache_error is None


def test_timeline_move_reorder_update_and_remove(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project.json", name="Timelines")
    first_bin = store.add_bin(Bin.create("First", bin_id="first"))
    second_bin = store.add_bin(Bin.create("Second", bin_id="second", order=1))
    one = store.add_timeline(timeline("one", first_bin.id))
    two = store.add_timeline(timeline("two", first_bin.id, numbers=(6, 7, 8), order=1))
    three = store.add_timeline(timeline("three", first_bin.id, order=2))

    store.reorder_timeline(three.id, 0)
    assert [item.id for item in store.list_timelines(first_bin.id)] == [three.id, one.id, two.id]
    moved = store.move_timeline(one.id, second_bin.id)
    assert moved.bin_id == second_bin.id
    updated = store.update_timeline(one.id, name="one renamed", in_frame=20, out_frame=80)
    assert updated.name == "one renamed"
    assert updated.updated_at >= updated.created_at
    assert store.remove_timeline(two.id).id == two.id
    assert [item.order for item in store.list_timelines(first_bin.id)] == [0]
    assert ProjectStore.load(store.path).list_timelines(second_bin.id)[0].name == "one renamed"


def test_atomic_write_failure_preserves_disk_and_rolls_back_memory(tmp_path: Path, monkeypatch):
    project_path = tmp_path / "project.json"
    store = ProjectStore.create(project_path, name="Safe")
    previous = project_path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("vpstitch.project.os.replace", fail_replace)
    with pytest.raises(ProjectError, match="simulated replace failure"):
        store.add_bin(Bin.create("Must rollback", bin_id="rollback"))

    assert project_path.read_bytes() == previous
    assert store.bins == ()
    assert list(tmp_path.glob(".project.json.*.tmp")) == []


@pytest.mark.parametrize(
    "payload, message",
    [
        ({"version": 99, "settings": {}, "bins": [], "media": [], "timelines": []}, "unsupported"),
        ({"version": 1, "settings": {"name": "x", "settings_snapshot": {}}, "bins": [], "timelines": [], "future": True}, "unknown"),
        ({"version": 1, "settings": {"name": "x", "settings_snapshot": {}}, "bins": "bad", "timelines": []}, "must be lists"),
    ],
)
def test_invalid_or_unknown_payload_is_rejected(tmp_path: Path, payload, message):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ProjectError, match=message):
        ProjectStore.load(path)


def test_rejects_corrupt_json_unknown_relations_and_invalid_camera_slots(tmp_path: Path):
    corrupt = tmp_path / "corrupt.json"
    corrupt.write_text("{broken", encoding="utf-8")
    with pytest.raises(ProjectError, match="cannot load"):
        ProjectStore.load(corrupt)

    store = ProjectStore.create(tmp_path / "valid.json", name="Validation")
    with pytest.raises(ProjectError, match="unknown timeline bin"):
        store.add_timeline(timeline("orphan", "missing"))
    with pytest.raises(ProjectError, match="contain 3 or 5 camera slots"):
        TimelineRecord.create(
            name="wrong",
            source_paths=plate_paths((1, 2)),
            config_snapshot={},
        )
    duplicate = tmp_path / "clip.mov"
    with pytest.raises(ProjectError, match="multiple camera slots"):
        TimelineRecord.create(
            name="duplicate assignment",
            source_paths=(duplicate, duplicate, tmp_path / "other.mov"),
            config_snapshot={},
        )


def test_timeline_preserves_manual_camera_slot_order_for_arbitrary_names(
    tmp_path: Path,
) -> None:
    paths = tuple(tmp_path / name for name in ("left.mov", "center.mov", "right.mov"))

    record = TimelineRecord.create(
        name="Manual Order",
        source_paths=paths,
        config_snapshot={},
    )

    assert record.source_paths == paths


def test_v1_project_migrates_timeline_sources_into_media_pool(tmp_path: Path):
    path = tmp_path / "legacy.json"
    legacy = timeline("legacy", None, numbers=(6, 7, 8)).to_dict()
    legacy.pop("inherits_project_settings")
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "settings": {"name": "Legacy", "settings_snapshot": {}},
                "bins": [],
                "timelines": [legacy],
            }
        ),
        encoding="utf-8",
    )

    loaded = ProjectStore.load(path, autosave=False)

    assert tuple(item.path for item in loaded.media) == loaded.timelines[0].source_paths
    assert loaded.timelines[0].inherits_project_settings is False


def test_empty_timeline_can_receive_a_complete_media_set(tmp_path: Path):
    store = ProjectStore.create(tmp_path / "project.json", name="Assignment")
    empty = store.add_timeline(
        TimelineRecord.create(name="Take 01", source_paths=(), config_snapshot={})
    )
    assert empty.source_paths == ()

    updated = store.update_timeline(
        empty.id, source_paths=tuple(plate_paths((1, 2, 3, 4, 5)))
    )

    assert len(updated.source_paths) == 5
    assert ProjectStore.load(store.path).timelines[0].source_paths == updated.source_paths


def test_timeline_accepts_camera_names_and_numbered_parent_folders() -> None:
    camera_names = [Path("plates") / f"camera-{number}.mov" for number in range(1, 6)]
    parent_names = [Path(f"P{number:02d}") / "A001.mov" for number in (6, 7, 8)]

    assert TimelineRecord.create(
        name="Rear", source_paths=camera_names, config_snapshot={}
    ).source_paths == tuple(camera_names)
    assert TimelineRecord.create(
        name="Front", source_paths=parent_names, config_snapshot={}
    ).source_paths == tuple(parent_names)


def test_v2_timeline_requires_explicit_project_settings_inheritance(tmp_path: Path) -> None:
    path = tmp_path / "missing-inheritance.json"
    payload = timeline("broken", None).to_dict()
    payload.pop("inherits_project_settings")
    path.write_text(
        json.dumps(
            {
                "version": 2,
                "settings": {"name": "Broken", "settings_snapshot": {}},
                "bins": [],
                "media": [],
                "timelines": [payload],
            }
        ),
        encoding="utf-8",
    )

    with pytest.raises(ProjectError, match="inherits_project_settings"):
        ProjectStore.load(path)


def test_save_copy_writes_recovery_snapshot_without_switching_project(tmp_path: Path) -> None:
    project_path = tmp_path / "project.json"
    recovery_path = tmp_path / "project.autosave.json"
    store = ProjectStore.create(project_path, name="Recovery")
    store.add_bin(Bin.create("Master"))

    assert store.save_copy(recovery_path) == recovery_path
    assert store.path == project_path
    recovered = ProjectStore.load(recovery_path, autosave=False)
    assert recovered.settings.name == "Recovery"
    assert recovered.bins[0].name == "Master"
