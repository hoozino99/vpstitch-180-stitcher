from __future__ import annotations

from dataclasses import replace

from vpstitch.config import load_config
from vpstitch.resources import estimate_resources


def test_20k_resource_estimate_counts_exact_frame_and_map_sizes() -> None:
    config = load_config("configs/five_cam_180.sample.json")
    config = replace(config, output=replace(config.output, width=20000, height=6000))
    report = estimate_resources(config)
    assert report.output_frame_bytes == 20000 * 6000 * 3 * 2
    assert report.projection_cache_bytes == 20000 * 6000 * 5 * 2 * 4
    assert report.recommended_free_memory_bytes >= report.estimated_peak_working_bytes
    assert report.uncompressed_sequence_bytes_per_minute is not None
