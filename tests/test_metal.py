from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pytest

from vpstitch.color import (
    BUNDLED_ACES_STUDIO_ID,
    ColorPipeline,
    quantize_u16,
)
from vpstitch.config import Color
from vpstitch.metal import (
    METAL_INPUT_KERNEL_NAME,
    MetalStitchBackend,
    build_metal_input_program,
)


def _p3_pq_color(*, color_match: bool = False) -> Color:
    return Color(
        mode="ocio",
        ocio_config=BUNDLED_ACES_STUDIO_ID,
        working_space="ACEScg",
        output_mode="display_view",
        display="ST2084-P3-D65 - Display",
        view="ACES 2.0 - HDR 1000 nits (P3 D65)",
        match_enabled=color_match,
        match_strength=0.7,
        integer_dither=False,
    )


def test_metal_input_program_supports_textureless_camera_transform() -> None:
    pytest.importorskip("PyOpenColorIO")
    program = build_metal_input_program(_p3_pq_color(), "Camera Rec.709")

    assert f"kernel void {METAL_INPUT_KERNEL_NAME}" in program.source
    assert "vp_input_ocio_ocio_input_transform ocio;" in program.source
    assert "ocio();" not in program.source


@pytest.mark.skipif(sys.platform != "darwin", reason="Metal requires macOS")
def test_metal_input_color_match_matches_cpu_pipeline() -> None:
    pytest.importorskip("PyOpenColorIO")
    library = (
        Path(__file__).resolve().parents[1]
        / ".build"
        / "macos"
        / "libvpstitch_metal.dylib"
    )
    if not library.is_file():
        pytest.skip("Metal test library is not built")

    settings = _p3_pq_color(color_match=True)
    gain = (1.02, 0.98, 1.01)
    source = np.random.default_rng(7).integers(
        0, 65536, (32, 48, 3), dtype=np.uint16
    )
    backend = MetalStitchBackend(settings, ["Camera Rec.709"], [gain])
    backend.transform_sources([source])
    map_y, map_x = np.mgrid[:32, :48].astype(np.float32)
    backend.prepare_tile(
        1,
        [0],
        [(map_x, map_y)],
        [np.ones((32, 48), dtype=np.float32)],
    )
    actual = backend.render_prepared_tile(
        1,
        48,
        32,
        tile_x=0,
        tile_y=0,
        frame_index=0,
        dither=False,
        seed=1,
    )
    batched = np.full((40, 64, 3), 65535, dtype=np.uint16)
    backend.render_prepared_frame(
        [(1, 8, 4)],
        batched,
        frame_index=0,
        dither=False,
        seed=1,
    )
    assert np.array_equal(batched[4:36, 8:56], actual)
    assert not np.any(batched[:4])
    assert not np.any(batched[:, :8])
    dithered_tile = backend.render_prepared_tile(
        1,
        48,
        32,
        tile_x=8,
        tile_y=4,
        frame_index=3,
        dither=True,
        seed=7349,
    )
    dithered_batch = np.empty((40, 64, 3), dtype=np.uint16)
    backend.render_prepared_frame(
        [(1, 8, 4)],
        dithered_batch,
        frame_index=3,
        dither=True,
        seed=7349,
    )
    assert np.array_equal(dithered_batch[4:36, 8:56], dithered_tile)

    color = ColorPipeline(settings, ["Camera Rec.709"], [gain])
    expected = quantize_u16(
        color.working_to_output(color.input_to_working(0, source)),
        False,
        1,
        0,
        0,
        0,
    )
    difference = np.abs(actual.astype(np.int32) - expected.astype(np.int32))
    assert int(difference.max()) <= 1
