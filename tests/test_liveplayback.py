from __future__ import annotations

import subprocess
import unicodedata
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from vpstitch.config import Camera, Lens
from vpstitch.liveplayback import (
    AlignedFramePlan,
    LivePlaybackSession,
    SynchronizedProxyDecoder,
)
from vpstitch.config import Color, Output, RigConfig
from vpstitch.ffmpegio import ffmpeg_executable
from vpstitch.sourcecache import (
    finalize_source_proxy,
    plan_source_proxy,
    source_proxy_command,
)


def _cameras(count: int) -> tuple[Camera, ...]:
    return tuple(
        Camera(
            name=f"cam-{index}",
            width=8,
            height=4,
            yaw_deg=0.0,
            pitch_deg=0.0,
            roll_deg=0.0,
            lens=Lens(model="pinhole", fx=4.0, fy=4.0, cx=4.0, cy=2.0),
        )
        for index in range(count)
    )


def _payload(sources: list[str], *, count: int = 20) -> dict[str, object]:
    return {
        "fps": 24.0,
        "inputs": [
            {"path": source, "skip_frames": index, "frame_count": count}
            for index, source in enumerate(sources)
        ],
    }


def test_aligned_frame_plan_combines_tc_and_manual_offsets(tmp_path: Path) -> None:
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    cameras = _cameras(3)
    cameras = (
        replace(cameras[0], frame_offset=-2),
        cameras[1],
        replace(cameras[2], frame_offset=1),
    )

    plan = AlignedFramePlan.from_payload(_payload(sources), sources, cameras, 24.0)

    assert plan.starts == (0, 3, 5)
    assert plan.common_frames == 15


class _FakeDecoder:
    instances: list[_FakeDecoder] = []
    fail_enabled = True

    def __init__(self, source, _camera, _fps, *, start_frame, **_kwargs):  # type: ignore[no-untyped-def]
        self.source = str(source)
        self.frame = start_frame
        self.closed = False
        self.fail_at = (
            4
            if self.fail_enabled and self.source.endswith("P02.mp4")
            else None
        )
        self.__class__.instances.append(self)

    def read(self, *, copy=True):  # type: ignore[no-untyped-def]
        if self.fail_at is not None and self.frame >= self.fail_at:
            return None
        value = self.frame
        self.frame += 1
        return np.full((4, 8, 3), value, dtype=np.uint16)

    def close(self) -> None:
        self.closed = True


def test_synchronized_decoder_never_returns_partial_camera_bundle(tmp_path: Path) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = True
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    cameras = _cameras(3)
    plan = AlignedFramePlan(24.0, (0, 0, 0), (10, 10, 10), 10)
    decoder = SynchronizedProxyDecoder(
        sources, cameras, plan, decoder_factory=_FakeDecoder
    )

    bundles = [decoder.read_bundle() for _ in range(5)]

    assert [bundle.timeline_frame for bundle in bundles[:4] if bundle] == [0, 1, 2, 3]
    assert bundles[4] is None
    assert all(len(bundle.frames) == 3 for bundle in bundles[:4] if bundle)
    decoder.close()
    assert all(item.closed for item in _FakeDecoder.instances)


def test_synchronized_decoder_seek_reopens_all_cameras_at_same_timeline_frame(
    tmp_path: Path,
) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = False
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    cameras = _cameras(3)
    plan = AlignedFramePlan(24.0, (2, 3, 4), (20, 20, 20), 16)
    decoder = SynchronizedProxyDecoder(
        sources, cameras, plan, decoder_factory=_FakeDecoder
    )

    decoder.seek(5)
    bundle = decoder.read_bundle()

    assert bundle is not None and bundle.timeline_frame == 5
    assert [int(frame[0, 0, 0]) for frame in bundle.frames] == [7, 8, 9]


def test_alignment_rejects_source_path_drift(tmp_path: Path) -> None:
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    payload = _payload(sources)
    payload["inputs"][1]["path"] = str(tmp_path / "wrong.mp4")  # type: ignore[index]

    with pytest.raises(ValueError, match="source mismatch"):
        AlignedFramePlan.from_payload(payload, sources, _cameras(3), 24.0)


def test_alignment_accepts_equivalent_macos_unicode_paths(tmp_path: Path) -> None:
    composed_dir = tmp_path / "260828_테스트"
    composed_dir.mkdir()
    composed_sources = [composed_dir / f"P0{index}.mp4" for index in range(1, 4)]
    for source in composed_sources:
        source.touch()
    payload = _payload([str(source) for source in composed_sources])
    decomposed_sources = [
        unicodedata.normalize("NFD", str(source)) for source in composed_sources
    ]

    plan = AlignedFramePlan.from_payload(
        payload,
        decomposed_sources,
        _cameras(3),
        24.0,
    )

    assert plan.common_frames == 18


def test_live_session_keeps_decoder_between_sequential_frames(
    tmp_path: Path, monkeypatch
) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = False
    cameras = _cameras(3)
    config = RigConfig(
        cameras=cameras,
        output=Output(width=32, height=12, tile_width=32, tile_height=12),
        color=Color(integer_dither=False),
    )
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    plan = AlignedFramePlan(24.0, (0, 0, 0), (20, 20, 20), 20)
    session = LivePlaybackSession(
        sources,
        config,
        plan,
        decoder_factory=_FakeDecoder,
        max_width=32,
        max_height=12,
    )
    rendered_tokens: list[int] = []

    def render_frames(_config, frames, *, frame_token):  # type: ignore[no-untyped-def]
        rendered_tokens.append(frame_token)
        return frames[0]

    monkeypatch.setattr(session.renderer, "render_frames", render_frames)

    first, _ = session.render_frame(2)
    second, _ = session.render_frame(3)

    assert first.timeline_frame == 2
    assert second.timeline_frame == 3
    assert rendered_tokens == [2, 3]
    assert len(_FakeDecoder.instances) == 3


def test_live_session_reuses_rendered_frame_until_settings_change(
    tmp_path: Path, monkeypatch
) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = False
    config = RigConfig(
        cameras=_cameras(3),
        output=Output(width=32, height=12, tile_width=32, tile_height=12),
        color=Color(integer_dither=False),
    )
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    plan = AlignedFramePlan(24.0, (0, 0, 0), (20, 20, 20), 20)
    session = LivePlaybackSession(
        sources,
        config,
        plan,
        decoder_factory=_FakeDecoder,
        max_width=32,
        max_height=12,
    )
    renders: list[int] = []

    def render_frames(_config, frames, *, frame_token):  # type: ignore[no-untyped-def]
        renders.append(frame_token)
        return frames[0]

    monkeypatch.setattr(session.renderer, "render_frames", render_frames)

    first = session.render_frame(4)
    second = session.render_frame(4)

    assert second is first
    assert session.has_rendered_frame(4)
    assert renders == [4]
    assert len(_FakeDecoder.instances) == 3


def test_live_session_reuses_decoded_frame_across_setting_changes(
    tmp_path: Path, monkeypatch
) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = False
    config = RigConfig(
        cameras=_cameras(3),
        output=Output(width=32, height=12, tile_width=32, tile_height=12),
        color=Color(integer_dither=False),
    )
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    plan = AlignedFramePlan(24.0, (0, 0, 0), (20, 20, 20), 20)
    session = LivePlaybackSession(
        sources,
        config,
        plan,
        decoder_factory=_FakeDecoder,
        max_width=32,
        max_height=12,
    )
    monkeypatch.setattr(
        session.renderer,
        "render_frames",
        lambda _config, frames, *, frame_token: frames[0],
    )

    session.render_frame(7)
    decoder_positions = tuple(decoder.frame for decoder in _FakeDecoder.instances)
    changed = replace(
        config,
        output=replace(config.output, seam_feather_deg=7.0),
    )
    assert session.can_reconfigure(sources, changed, plan)
    session.reconfigure(changed)
    session.render_frame(7)

    assert tuple(decoder.frame for decoder in _FakeDecoder.instances) == decoder_positions
    assert len(_FakeDecoder.instances) == 3


def test_live_session_move_draft_reuses_decoded_frame_and_separate_renderer(
    tmp_path: Path, monkeypatch
) -> None:
    _FakeDecoder.instances.clear()
    _FakeDecoder.fail_enabled = False
    config = RigConfig(
        cameras=_cameras(3),
        output=Output(width=64, height=24, tile_width=64, tile_height=24),
        color=Color(integer_dither=False),
    )
    sources = [str(tmp_path / f"P0{index}.mp4") for index in range(1, 4)]
    plan = AlignedFramePlan(24.0, (0, 0, 0), (20, 20, 20), 20)
    session = LivePlaybackSession(
        sources,
        config,
        plan,
        decoder_factory=_FakeDecoder,
        max_width=64,
        max_height=24,
    )
    calls: list[tuple[str, object]] = []
    monkeypatch.setattr(
        session.renderer,
        "render_frames",
        lambda _config, frames, *, frame_token: (
            calls.append(("full", frame_token)) or frames[0]
        ),
    )
    monkeypatch.setattr(
        session.draft_renderer,
        "render_frames",
        lambda _config, frames, *, frame_token: (
            calls.append(("draft", frame_token)) or frames[0]
        ),
    )

    session.render_frame(6)
    decoder_positions = tuple(decoder.frame for decoder in _FakeDecoder.instances)
    changed = replace(
        config,
        cameras=(replace(config.cameras[0], yaw_deg=-0.05), *config.cameras[1:]),
    )
    session.reconfigure(changed)
    session.render_frame(6, draft=True)

    assert calls == [("full", 6), ("draft", 6)]
    assert tuple(decoder.frame for decoder in _FakeDecoder.instances) == decoder_positions
    assert session.has_rendered_frame(6, draft=True)
    assert len(_FakeDecoder.instances) == 3


@pytest.mark.parametrize("camera_count", [3, 5])
def test_real_source_proxies_decode_and_render_synchronized_bundles(
    tmp_path: Path,
    camera_count: int,
) -> None:
    ffmpeg = ffmpeg_executable()
    colors = ("red", "green", "blue", "yellow", "magenta")
    proxies: list[str] = []
    for index in range(camera_count):
        source = tmp_path / f"P{index + 1:02d}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-y",
                "-f",
                "lavfi",
                "-i",
                f"color=c={colors[index]}:s=160x90:r=12:d=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(source),
            ],
            check=True,
            capture_output=True,
        )
        proxy_plan = plan_source_proxy(source, tmp_path / "cache")
        program, arguments = source_proxy_command(proxy_plan)
        subprocess.run(
            [program, *arguments],
            check=True,
            capture_output=True,
        )
        proxies.append(str(finalize_source_proxy(proxy_plan)))

    cameras = tuple(
        Camera(
            name=f"cam-{index}",
            width=160,
            height=90,
            yaw_deg=(index - (camera_count - 1) / 2) * 25.0,
            pitch_deg=0.0,
            roll_deg=0.0,
            lens=Lens(
                model="pinhole",
                fx=100.0,
                fy=100.0,
                cx=80.0,
                cy=45.0,
            ),
        )
        for index in range(camera_count)
    )
    config = RigConfig(
        cameras=cameras,
        output=Output(
            width=320,
            height=120,
            horizontal_fov_deg=120.0,
            vertical_fov_deg=50.0,
            tile_width=320,
            tile_height=120,
        ),
        color=Color(integer_dither=False),
    )
    plan = AlignedFramePlan(
        fps=12.0,
        starts=(0,) * camera_count,
        frame_counts=(6,) * camera_count,
        common_frames=6,
    )
    session = LivePlaybackSession(
        proxies,
        config,
        plan,
        max_width=320,
        max_height=120,
    )

    first, first_image = session.render_frame(0)
    second, second_image = session.render_frame(1)
    session.close()

    assert first.timeline_frame == 0
    assert second.timeline_frame == 1
    assert len(first.frames) == camera_count
    assert first_image.shape == (120, 320, 3)
    assert second_image.shape == first_image.shape
