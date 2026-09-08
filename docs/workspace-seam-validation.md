# Workspace and seam validation

This source revision improves the desktop workflow, repeated preview rendering, CPU blending, and optional seam placement. It is a development-branch change; the existing v0.1.1 release assets are separate builds.

## Workspace behavior

- The project manager and editor share a flat gray Qt theme with readable system fonts and visible focus and disabled states.
- Media search matches clips and folders, preserves matching ancestors, clears hidden selections, and restores expansion when cleared.
- Timeline actions refresh after loading. Empty timelines cannot play or clear nonexistent plates.
- Queue actions distinguish pending, selected, and completed jobs. Open output and Remove are visible; removing a job preserves its output file.
- Preview updates reuse the graphics item and retain the view transform when dimensions stay unchanged. Cached seam weights invalidate when geometry, output, feather, or custom paths change.
- Subprocess progress parsing preserves incomplete lines and split UTF-8 characters across reads.

## Fixed seam paths

`output.seam_paths_deg` optionally supplies one horizontal-angle path per adjacent camera pair. Each path contains at least two finite values sampled uniformly from the top to the bottom of the output canvas. Every angle must lie strictly between the associated cameras' yaw values. For a three-camera rig with yaws of -40, 0, and 40 degrees, a valid example is:

```json
{
  "output": {
    "seam_paths_deg": [[-20, -18, -20], [20, 22, 20]]
  }
}
```

This is an output-field fragment to add to an existing rig profile, not a complete configuration. Preview and tiled rendering sample paths in full-canvas coordinates, including tile margins. The same path is reused across the clip. Omitting the field retains the original midpoint boundaries. **Canvas → Reset seam path** removes custom paths while preserving camera poses and color settings; per-camera feather overrides remain in effect.

`calibrate-rig --refine-seams` enables optional rotation refinement near the midpoint seam when enough distributed feature matches support a better fit. It rejects sparse support and excessive corrections. The existing Auto Stitch default remains unchanged. This option does not generate fixed seam paths or guarantee a better fit across every depth or frame. Review representative frames before applying a calibration to a complete plate.

`vpstitch-gui --project /path/to/project.json` opens a saved project directly. Without that option, the project manager opens as before.

## Measured performance

The synthetic CPU benchmark compares the v0.1.1 implementation with this revision on identical input pixels, after a warm-up and using the median of nine runs. Run it from the repository root with the project dependencies installed:

```bash
python packaging/benchmark_workspace.py v0.1.1
```

| Operation | Baseline | Improved | Ratio |
| --- | ---: | ---: | ---: |
| Five-camera blend, 2048 × 512 | 57.297 ms | 33.315 ms | 1.72× |
| Five-camera live preview, 1280 × 320 | 37.525 ms | 16.483 ms | 2.28× |
| Five-camera CPU stitch, 2048 × 512 | 418.736 ms | 389.722 ms | 1.07× |

All three comparisons produced identical pixels. These measurements exclude decoding, encoding, and file I/O; they do not establish equivalent end-to-end or Metal speedups. Timing varies by host.

## Validation evidence and limits

- Local macOS regression suite on 2026-09-08: **309 passed in 46.86 seconds** with `QT_QPA_PLATFORM=offscreen`. Coverage includes blending equivalence, cache invalidation, tile-consistent seam weights, configuration validation, calibration fallback, queue states, media filtering, and progress parsing.
- Actual packaged macOS GUI: project open, timeline readiness, search, three-camera preview/playback, queue render, output reveal, and custom-path preview were exercised. The bundled CLI and ad-hoc code signature were verified separately.
- Packaged output: 20,000 × 5,504 ProRes HQ and ProRes 4444 two-frame checks completed and decoded successfully during workspace QA. Final seam QA also produced a two-frame 20K ProRes HQ file through Metal → IOSurface, plus a full 520-frame, 24 fps SDR review from synchronized source proxies. These are distinct checks, not a full-length 20K delivery.
- One three-camera plate: the accepted left-camera correction reduced the median of per-sample feature-alignment medians from **45.5 px to 5.2 px** in 20K coordinates over 15 sampled times. A fixed right seam path and narrower feather reduced doubled crane edges. A right-camera rotation candidate and optical-flow candidate were rejected after they worsened nearby objects or road lines.
- Existing color-match gains and OCIO delivery settings were preserved. This work does not certify physical LED-wall color accuracy, remove every parallax artifact, or manually inspect all 520 frames at full resolution.
- Windows and Intel Mac package execution are not established by the macOS checks. CI results should be inspected for the pushed revision separately.

Source footage, machine-specific project paths, screenshots, experimental scripts, and rendered QA media are excluded from the repository. Durable implementation, regression tests, and the portable synthetic benchmark are included. Local final comparisons, the review movie, saved projects, and usable app bundles are retained; temporary frames, map caches, trial exports, and duplicate build staging are removed after validation.
