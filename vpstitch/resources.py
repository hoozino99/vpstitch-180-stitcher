from __future__ import annotations

from dataclasses import asdict, dataclass
from math import ceil

from .config import RigConfig
from .renderflow import DEFAULT_PREFETCH_MAX_BYTES


@dataclass(frozen=True)
class ResourceEstimate:
    input_frame_bytes: int
    output_frame_bytes: int
    projection_cache_bytes: int
    uncompressed_sequence_bytes_per_minute: int | None
    estimated_peak_working_bytes: int
    recommended_free_memory_bytes: int
    decode_prefetch_extra_bytes: int
    decode_prefetch_recommended: bool
    notes: tuple[str, ...]

    def to_dict(self) -> dict[str, object]:
        result = asdict(self)
        result["input_frame_gib"] = self.input_frame_bytes / 2**30
        result["output_frame_gib"] = self.output_frame_bytes / 2**30
        result["projection_cache_gib"] = self.projection_cache_bytes / 2**30
        result["estimated_peak_working_gib"] = self.estimated_peak_working_bytes / 2**30
        result["recommended_free_memory_gib"] = self.recommended_free_memory_bytes / 2**30
        if self.uncompressed_sequence_bytes_per_minute is not None:
            result["uncompressed_sequence_gib_per_minute"] = (
                self.uncompressed_sequence_bytes_per_minute / 2**30
            )
        return result


def estimate_resources(config: RigConfig) -> ResourceEstimate:
    camera_pixels = sum(camera.width * camera.height for camera in config.cameras)
    output_pixels = config.output.width * config.output.height
    input_frame = camera_pixels * 3 * 2
    output_frame = output_pixels * 3 * 2
    # Two float32 maps (x/y) for every camera.
    projection_cache = output_pixels * len(config.cameras) * 2 * 4

    margin = (
        int(ceil(config.flow.max_displacement_px)) + 16
        if config.flow.enabled
        else 0
    )
    tile_width = min(config.output.width, config.output.tile_width + 2 * margin)
    tile_height = min(config.output.height, config.output.tile_height + 2 * margin)
    tile_pixels = tile_width * tile_height
    cameras = len(config.cameras)
    # Warps, weights, masks, flow fields, blend accumulator, and OCIO output.
    tile_scratch = tile_pixels * (cameras * (3 * 4 + 4 + 1) + 40)

    # Five concurrent high-resolution FFmpeg decoders and the encoder retain
    # native reference/filter frames beyond the Python-owned source and output
    # buffers. These conservative factors come from 5x6K -> 20K process-tree
    # measurements and intentionally overestimate low-bit-depth inputs.
    runtime_overhead = 2 * 2**30
    peak = input_frame * 7 + output_frame * 3 + tile_scratch + runtime_overhead
    prefetch_recommended = input_frame <= DEFAULT_PREFETCH_MAX_BYTES
    if prefetch_recommended:
        peak += input_frame
    if config.color.mode == "ocio":
        peak += camera_pixels * 3 * 4
    # Keep four GiB beyond the measured working set, then round up in 2 GiB
    # increments so borderline machines receive a clear warning.
    memory_step = 2 * 2**30
    recommended = int(ceil((peak * 1.25 + 4 * 2**30) / memory_step) * memory_step)

    sequence_per_minute = None
    if config.video is not None:
        sequence_per_minute = int(output_frame * config.video.fps * 60.0)
    notes = [
        "Projection cache size is independent of clip duration.",
        "The sequence figure is an uncompressed RGB-equivalent worst-case size.",
        "FFV1/ProRes sizes depend on image content and cannot be predicted exactly.",
        "Peak memory includes concurrent FFmpeg decoder and encoder working sets.",
    ]
    notes.append(
        "Decode prefetch is enabled because one copied camera bundle fits the safe budget."
        if prefetch_recommended
        else "Decode prefetch is disabled to avoid duplicating a high-resolution camera bundle."
    )
    if config.video and config.video.output_codec == "hevc-444-10":
        notes.append("HEVC is intended for delivery, not the preservation master.")
    if config.color.mode == "ocio":
        notes.append("OCIO keeps one float32 working copy of all camera frames.")
    return ResourceEstimate(
        input_frame_bytes=input_frame,
        output_frame_bytes=output_frame,
        projection_cache_bytes=projection_cache,
        uncompressed_sequence_bytes_per_minute=sequence_per_minute,
        estimated_peak_working_bytes=peak,
        recommended_free_memory_bytes=recommended,
        decode_prefetch_extra_bytes=input_frame if prefetch_recommended else 0,
        decode_prefetch_recommended=prefetch_recommended,
        notes=tuple(notes),
    )
