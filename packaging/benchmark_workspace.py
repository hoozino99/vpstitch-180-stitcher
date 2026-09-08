"""Compare workspace hot paths against a git ref with identical synthetic pixels.

Run from the repository root: python packaging/benchmark_workspace.py [ref]
"""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import subprocess
import sys
import time
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import numpy as np
import vpstitch.pipeline as pipeline
from vpstitch.blend import weighted_blend
from vpstitch.config import Camera, Color, Lens, Output, RigConfig
from vpstitch.interactive import InteractivePreviewRenderer


def old_module(name, ref):
    source = subprocess.check_output(["git", "show", f"{ref}:vpstitch/{name}.py"], text=True)
    module = types.ModuleType(f"vpstitch._baseline_{name}")
    module.__package__ = "vpstitch"
    exec(compile(source, f"baseline/{name}.py", "exec"), module.__dict__)
    return module


def median_ms(callback, runs=9):
    callback()
    samples = []
    for _ in range(runs):
        start = time.perf_counter()
        callback()
        samples.append((time.perf_counter() - start) * 1000)
    return statistics.median(samples)


def main():
    ref = sys.argv[1] if len(sys.argv) > 1 else "v0.1.1"
    old_blend = old_module("blend", ref).weighted_blend
    old_preview = old_module("interactive", ref)
    old_preview.weighted_blend = old_blend
    rng = np.random.default_rng(91)
    frames = [rng.random((512, 2048, 3), dtype=np.float32) for _ in range(5)]
    weights = [rng.random((512, 2048), dtype=np.float32) for _ in frames]
    results = {}

    def record(name, before, after):
        baseline = median_ms(before)
        current = median_ms(after)
        results[name] = {"before_ms": round(baseline, 3), "after_ms": round(current, 3),
                         "speedup": round(baseline / current, 2), "pixels_equal": True}

    np.testing.assert_array_equal(old_blend(frames, weights), weighted_blend(frames, weights))
    record("five_camera_blend_2048x512", lambda: old_blend(frames, weights), lambda: weighted_blend(frames, weights))
    cameras = tuple(Camera(f"cam{i}", 320, 240, yaw, 0, 0,
                           Lens("pinhole", 220, 220, 160, 120)) for i, yaw in enumerate((-80, -40, 0, 40, 80)))
    rig = RigConfig(cameras=cameras, output=Output(width=2048, height=512, horizontal_fov_deg=180,
                    vertical_fov_deg=50, tile_width=512, tile_height=256), color=Color(integer_dither=False))
    plates = [(rng.random((240, 320, 3)) * 65535).astype(np.uint16) for _ in cameras]
    before = old_preview.InteractivePreviewRenderer(max_width=1280, max_height=720)
    after = InteractivePreviewRenderer(max_width=1280, max_height=720)
    np.testing.assert_array_equal(before.render_frames(rig, plates), after.render_frames(rig, plates))
    record("five_camera_live_preview_1280x320", lambda: before.render_frames(rig, plates), lambda: after.render_frames(rig, plates))
    stitcher = pipeline.Stitcher(rig)
    stitcher._backend_rejected = True
    stitcher._metal_backend = None
    output = np.empty((512, 2048, 3), dtype=np.uint16)

    def render(blender):
        pipeline.weighted_blend = blender
        stitcher.stitch_arrays(plates, output)

    render(old_blend)
    reference = output.copy()
    render(weighted_blend)
    np.testing.assert_array_equal(reference, output)
    record("five_camera_cpu_stitch_2048x512", lambda: render(old_blend), lambda: render(weighted_blend))
    print(json.dumps({"baseline": ref, "kind": "synthetic CPU benchmark; excludes decode and encode", "results": results}, indent=2))


if __name__ == "__main__":
    main()
