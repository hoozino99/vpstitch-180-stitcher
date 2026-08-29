from __future__ import annotations

import unicodedata
from collections import OrderedDict
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable, Protocol

import numpy as np

from .config import Camera, RigConfig
from .ffmpegio import VideoDecoder
from .interactive import InteractivePreviewRenderer


@dataclass(frozen=True, slots=True)
class AlignedFramePlan:
    """One immutable mapping from timeline frames to every camera source."""

    fps: float
    starts: tuple[int, ...]
    frame_counts: tuple[int, ...]
    common_frames: int

    @classmethod
    def from_payload(
        cls,
        payload: dict[str, object],
        sources: list[str] | tuple[str, ...],
        cameras: tuple[Camera, ...],
        fps: float,
    ) -> AlignedFramePlan:
        inputs = payload.get("inputs")
        if not isinstance(inputs, list) or len(inputs) != len(sources):
            raise ValueError("alignment input count does not match playback sources")
        if len(cameras) != len(sources):
            raise ValueError("camera count does not match playback sources")
        plan_fps = float(payload.get("fps", 0.0))
        if abs(plan_fps - fps) > 0.001:
            raise ValueError("alignment fps does not match playback fps")
        skips: list[int] = []
        counts: list[int] = []
        for source, item in zip(sources, inputs, strict=True):
            if not isinstance(item, dict):
                raise ValueError("alignment input entry is invalid")
            planned_path = Path(str(item.get("path", ""))).resolve()
            source_path = Path(source).resolve()
            try:
                paths_match = planned_path.samefile(source_path)
            except OSError:
                # Qt/macOS can pass an APFS path in decomposed Unicode (NFD)
                # while the JSON plan contains the composed spelling (NFC).
                # They name the same path even though direct string comparison
                # reports a mismatch.
                paths_match = unicodedata.normalize(
                    "NFC", str(planned_path)
                ) == unicodedata.normalize("NFC", str(source_path))
            if not paths_match:
                raise ValueError(
                    f"alignment source mismatch: expected {planned_path}, got {source}"
                )
            skip = int(item.get("skip_frames", -1))
            count = int(item.get("frame_count", 0))
            if skip < 0 or count < 1 or skip >= count:
                raise ValueError(f"invalid alignment range for {source}")
            skips.append(skip)
            counts.append(count)
        starts = [
            skip + camera.frame_offset
            for skip, camera in zip(skips, cameras, strict=True)
        ]
        normalization = -min(0, min(starts))
        starts = [start + normalization for start in starts]
        common_frames = min(
            count - start for count, start in zip(counts, starts, strict=True)
        )
        if common_frames < 1:
            raise ValueError("manual camera offsets leave no common aligned range")
        return cls(
            fps=fps,
            starts=tuple(starts),
            frame_counts=tuple(counts),
            common_frames=common_frames,
        )


@dataclass(frozen=True, slots=True)
class FrameBundle:
    timeline_frame: int
    frames: tuple[np.ndarray, ...]


class _Decoder(Protocol):
    def read(self, *, copy: bool = True) -> np.ndarray | None: ...

    def close(self) -> None: ...


DecoderFactory = Callable[..., _Decoder]


class SynchronizedProxyDecoder:
    """Persistent 3/5-camera decoder that only returns complete frame bundles."""

    def __init__(
        self,
        sources: list[str] | tuple[str, ...],
        cameras: tuple[Camera, ...],
        plan: AlignedFramePlan,
        *,
        decoder_factory: DecoderFactory = VideoDecoder,
        decoder_threads: int = 1,
        timeline_frame: int = 0,
    ) -> None:
        if len(sources) not in (3, 5):
            raise ValueError("live playback requires a 3-camera or 5-camera set")
        if len(sources) != len(cameras) or len(sources) != len(plan.starts):
            raise ValueError("source, camera, and alignment counts must match")
        self.sources = tuple(str(Path(source)) for source in sources)
        self.cameras = cameras
        self.plan = plan
        self.decoder_factory = decoder_factory
        self.decoder_threads = decoder_threads
        if timeline_frame < 0 or timeline_frame >= plan.common_frames:
            raise ValueError("timeline frame is outside the aligned range")
        self.position = timeline_frame
        self._decoders: list[_Decoder] = []
        self._eof = False
        self._open(timeline_frame)

    def _open(self, timeline_frame: int) -> None:
        opened: list[_Decoder] = []
        try:
            for source, camera, start in zip(
                self.sources, self.cameras, self.plan.starts, strict=True
            ):
                opened.append(
                    self.decoder_factory(
                        source,
                        replace(camera, frame_offset=0),
                        self.plan.fps,
                        start_frame=start + timeline_frame,
                        source_fps=self.plan.fps,
                        exact_frame_seek=True,
                        output_size=(camera.width, camera.height),
                        decoder_threads=self.decoder_threads,
                        source_bit_depth=8,
                    )
                )
        except Exception:
            for decoder in opened:
                decoder.close()
            raise
        self._decoders = opened
        self.position = timeline_frame
        self._eof = False

    def read_bundle(self) -> FrameBundle | None:
        if self._eof or self.position >= self.plan.common_frames:
            return None
        frames: list[np.ndarray] = []
        for decoder in self._decoders:
            frame = decoder.read(copy=True)
            if frame is None:
                self._eof = True
                return None
            frames.append(frame)
        bundle = FrameBundle(self.position, tuple(frames))
        self.position += 1
        return bundle

    def seek(self, timeline_frame: int) -> None:
        if timeline_frame < 0 or timeline_frame >= self.plan.common_frames:
            raise ValueError("timeline frame is outside the aligned range")
        self.close()
        self._open(timeline_frame)

    def close(self) -> None:
        decoders, self._decoders = self._decoders, []
        for decoder in decoders:
            decoder.close()
        self._eof = True

    def __enter__(self) -> SynchronizedProxyDecoder:
        return self

    def __exit__(self, *_args: object) -> None:
        self.close()


class LivePlaybackSession:
    """Thread-confined decoder + renderer session for editor-style playback."""

    def __init__(
        self,
        sources: list[str] | tuple[str, ...],
        config: RigConfig,
        plan: AlignedFramePlan,
        *,
        decoder_factory: DecoderFactory = VideoDecoder,
        max_width: int = 1280,
        max_height: int = 720,
    ) -> None:
        self.sources = tuple(str(Path(source)) for source in sources)
        self.config = config
        self.plan = plan
        self.decoder_factory = decoder_factory
        self.renderer = InteractivePreviewRenderer(
            max_width=max_width,
            max_height=max_height,
        )
        # Inspector movement gets its own renderer. It shares decoded frame
        # bundles with the normal viewer but keeps lower-resolution maps and
        # warped layers hot, so continuous nudges never force another decode
        # or disturb the full-quality preview renderer.
        self.draft_renderer = InteractivePreviewRenderer(
            max_width=min(max_width, 640),
            max_height=min(max_height, 360),
        )
        self.decoder: SynchronizedProxyDecoder | None = None
        self._frame_cache: OrderedDict[int, FrameBundle] = OrderedDict()
        self._frame_cache_limit = 8
        self._render_cache: OrderedDict[
            int, tuple[FrameBundle, np.ndarray]
        ] = OrderedDict()
        self._render_cache_limit = 12
        self._draft_render_cache: OrderedDict[
            int, tuple[FrameBundle, np.ndarray]
        ] = OrderedDict()
        self._draft_render_cache_limit = 2

    def can_reconfigure(
        self,
        sources: list[str] | tuple[str, ...],
        config: RigConfig,
        plan: AlignedFramePlan,
    ) -> bool:
        if tuple(str(Path(source)) for source in sources) != self.sources:
            return False
        if plan != self.plan or len(config.cameras) != len(self.config.cameras):
            return False
        return all(
            (new.width, new.height) == (old.width, old.height)
            for new, old in zip(config.cameras, self.config.cameras, strict=True)
        )

    def reconfigure(self, config: RigConfig) -> None:
        if not self.can_reconfigure(self.sources, config, self.plan):
            raise ValueError("live playback decode geometry changed")
        self.config = config
        # Decoded camera frames remain valid across color/geometry changes,
        # while stitched images must be regenerated with the new settings.
        self._render_cache.clear()
        self._draft_render_cache.clear()

    def _remember(self, bundle: FrameBundle) -> None:
        self._frame_cache[bundle.timeline_frame] = bundle
        self._frame_cache.move_to_end(bundle.timeline_frame)
        while len(self._frame_cache) > self._frame_cache_limit:
            self._frame_cache.popitem(last=False)

    def has_cached_frame(self, timeline_frame: int) -> bool:
        return timeline_frame in self._frame_cache

    def has_rendered_frame(self, timeline_frame: int, *, draft: bool = False) -> bool:
        cache = self._draft_render_cache if draft else self._render_cache
        return timeline_frame in cache

    def render_frame(
        self,
        timeline_frame: int,
        *,
        draft: bool = False,
    ) -> tuple[FrameBundle, np.ndarray]:
        render_cache = self._draft_render_cache if draft else self._render_cache
        rendered = render_cache.get(timeline_frame)
        if rendered is not None:
            render_cache.move_to_end(timeline_frame)
            return rendered
        bundle = self._frame_cache.get(timeline_frame)
        if bundle is not None:
            self._frame_cache.move_to_end(timeline_frame)
        else:
            if self.decoder is None:
                self.decoder = SynchronizedProxyDecoder(
                    self.sources,
                    self.config.cameras,
                    self.plan,
                    decoder_factory=self.decoder_factory,
                    timeline_frame=timeline_frame,
                )
            elif self.decoder.position != timeline_frame:
                self.decoder.seek(timeline_frame)
            bundle = self.decoder.read_bundle()
            if bundle is None:
                raise EOFError("synchronized source proxy reached end of stream")
            self._remember(bundle)
        renderer = self.draft_renderer if draft else self.renderer
        image = renderer.render_frames(
            self.config,
            bundle.frames,
            frame_token=bundle.timeline_frame,
        )
        rendered = (bundle, image)
        render_cache[timeline_frame] = rendered
        render_cache.move_to_end(timeline_frame)
        cache_limit = (
            self._draft_render_cache_limit if draft else self._render_cache_limit
        )
        while len(render_cache) > cache_limit:
            render_cache.popitem(last=False)
        return rendered

    def close(self) -> None:
        if self.decoder is not None:
            self.decoder.close()
            self.decoder = None
        self._frame_cache.clear()
        self._render_cache.clear()
        self._draft_render_cache.clear()
