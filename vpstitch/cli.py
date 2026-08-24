from __future__ import annotations

import argparse
import sys
import tempfile
import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np

from .config import (
    ConfigError,
    MAX_CANVAS_HEIGHT,
    MAX_CANVAS_WIDTH,
    RigConfig,
    load_config,
)
from .ffmpegio import DpxSequenceEncoder, VideoDecoder, VideoEncoder, probe_video
from .imageio import ExrSequenceEncoder, write_png
from .pipeline import Stitcher
from .mapcache import MapCache
from .canvas import analyze_canvas, write_coverage_mask
from .diagnostics import assess_inputs, resolve_passthrough_video
from .calibration import CalibrationError, calibrate_checkerboard
from .rigcalibration import calibrate_rig_rotation, write_calibrated_config
from .resources import estimate_resources
from .color import load_ocio_config
from .timecode import align_by_timecode


def _progress(done: int, total: int) -> None:
    print(f"\rtiles {done}/{total}", end="", flush=True)
    if done == total:
        print()


def _canvas_dimensions(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("canvas must look like 15360x3968")
    width, height = int(match.group(1)), int(match.group(2))
    if not (1 <= width <= MAX_CANVAS_WIDTH and 1 <= height <= MAX_CANVAS_HEIGHT):
        raise argparse.ArgumentTypeError(
            f"canvas must be within {MAX_CANVAS_WIDTH}x{MAX_CANVAS_HEIGHT}"
        )
    return width, height


def _apply_canvas_overrides(config: RigConfig, args: argparse.Namespace) -> RigConfig:
    output = config.output
    if getattr(args, "canvas", None):
        output = replace(output, width=args.canvas[0], height=args.canvas[1])
    replacements = {
        "horizontal_fov_deg": getattr(args, "h_fov", None),
        "vertical_fov_deg": getattr(args, "v_fov", None),
        "center_yaw_deg": getattr(args, "center_yaw", None),
        "center_pitch_deg": getattr(args, "center_pitch", None),
    }
    output = replace(output, **{key: value for key, value in replacements.items() if value is not None})
    if getattr(args, "rugby_strength", None) is not None:
        output = replace(
            output,
            projection="cylindrical_rugby",
            rugby_strength=args.rugby_strength,
        )
    return replace(config, output=output)


def _add_canvas_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--canvas",
        type=_canvas_dimensions,
        help=f"output canvas WIDTHxHEIGHT, max {MAX_CANVAS_WIDTH}x{MAX_CANVAS_HEIGHT}",
    )
    parser.add_argument("--h-fov", type=float, help="horizontal field of view in degrees")
    parser.add_argument("--v-fov", type=float, help="vertical field of view in degrees")
    parser.add_argument("--center-yaw", type=float, help="canvas center yaw in degrees")
    parser.add_argument("--center-pitch", type=float, help="canvas center pitch in degrees")
    parser.add_argument(
        "--rugby-strength",
        type=float,
        help="vertical edge compression for a cylindrical rugby-ball projection (0-<1)",
    )


def _load_alignment_plan(
    path: str | None,
    inputs: list[str],
    fps: float,
) -> tuple[list[int], list[int], int] | None:
    if not path:
        return None
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        plan_inputs = payload["inputs"]
        common_frames = int(payload["common_frames"])
        plan_fps = float(payload["fps"])
    except (OSError, json.JSONDecodeError, KeyError, TypeError, ValueError) as error:
        raise ConfigError(f"invalid alignment plan {path}: {error}") from error
    if not isinstance(plan_inputs, list) or len(plan_inputs) != len(inputs):
        raise ConfigError("alignment plan input count does not match render inputs")
    if abs(plan_fps - fps) > 0.001:
        raise ConfigError("alignment plan fps does not match the render config")
    skips: list[int] = []
    counts: list[int] = []
    for source, item in zip(inputs, plan_inputs, strict=True):
        if not isinstance(item, dict):
            raise ConfigError("alignment plan input entry is invalid")
        try:
            planned_path = Path(str(item["path"])).resolve()
            skip = int(item["skip_frames"])
            count = int(item["frame_count"])
        except (KeyError, TypeError, ValueError) as error:
            raise ConfigError(f"invalid alignment plan input: {error}") from error
        if planned_path != Path(source).resolve():
            raise ConfigError(
                f"alignment plan source mismatch: expected {planned_path}, got {source}"
            )
        if skip < 0 or count < 1 or skip >= count:
            raise ConfigError(f"invalid alignment range for {source}")
        skips.append(skip)
        counts.append(count)
    if common_frames < 1:
        raise ConfigError("alignment plan has no common frames")
    return skips, counts, common_frames


def _planned_decoder_starts(
    config: RigConfig,
    inputs: list[str],
    alignment_path: str | None,
) -> tuple[list[int] | None, int | None]:
    assert config.video is not None
    plan = _load_alignment_plan(alignment_path, inputs, config.video.fps)
    if plan is None:
        return None, None
    plan_skips, frame_counts, _ = plan
    starts = [
        skip + camera.frame_offset
        for skip, camera in zip(plan_skips, config.cameras, strict=True)
    ]
    normalization = -min(0, min(starts))
    starts = [start + normalization for start in starts]
    available = min(
        count - start for count, start in zip(frame_counts, starts, strict=True)
    )
    if available < 1:
        raise ConfigError("manual camera offsets leave no common aligned range")
    return starts, available


def _stitch_frame(args: argparse.Namespace) -> None:
    config = _apply_canvas_overrides(load_config(args.config), args)
    if len(args.inputs) != len(config.cameras):
        raise ConfigError(f"expected {len(config.cameras)} input images")
    Stitcher(config).stitch_images(args.inputs, args.output, progress=_progress)


def _stitch_video(args: argparse.Namespace) -> None:
    config = _apply_canvas_overrides(load_config(args.config), args)
    if config.video is None:
        raise ConfigError("video section is required for stitch-video")
    if args.frames is not None:
        if args.frames < 1:
            raise ConfigError("--frames must be at least 1")
        config = replace(config, video=replace(config.video, frames=args.frames))
    if len(args.inputs) != len(config.cameras):
        raise ConfigError(f"expected {len(config.cameras)} input videos")

    probes = [probe_video(path) for path in args.inputs]
    diagnostic = assess_inputs(
        probes,
        config,
        allow_low_bit_depth=args.allow_low_bit_depth,
    )
    errors = [issue.message for issue in diagnostic.issues if issue.severity == "error"]
    if errors:
        raise ConfigError("input preflight failed: " + " | ".join(errors))
    for issue in diagnostic.issues:
        if issue.severity == "warning":
            print(f"warning: {issue.message}", file=sys.stderr)
    if config.color.mode == "passthrough":
        assert config.video is not None
        config = replace(config, video=resolve_passthrough_video(config.video, probes))

    planned_starts, aligned_frames = _planned_decoder_starts(
        config, args.inputs, args.alignment_plan
    )
    if aligned_frames is not None:
        available = aligned_frames - args.start_frame
        if available < 1:
            raise ConfigError("--start-frame is outside the aligned common range")
        if config.video.frames is None:
            config = replace(config, video=replace(config.video, frames=available))
        elif config.video.frames > available:
            raise ConfigError(
                f"requested {config.video.frames} frames, but only {available} aligned frames remain"
            )

    cache = MapCache(config, args.map_cache).open(progress=_progress)
    stitcher = Stitcher(config, map_cache=cache)
    decoders = []
    for index, (path, camera, probe) in enumerate(
        zip(args.inputs, config.cameras, probes, strict=True)
    ):
        if planned_starts is None:
            decoder_camera = camera
            decoder_start = args.start_frame
        else:
            decoder_camera = replace(camera, frame_offset=0)
            decoder_start = planned_starts[index] + args.start_frame
        decoders.append(
            VideoDecoder(
                path,
                decoder_camera,
                config.video.fps,
                start_frame=decoder_start,
                source_fps=probe.fps,
                exact_frame_seek=True,
            )
        )
    if config.video.output_codec == "exr-half-sequence":
        encoder = ExrSequenceEncoder(
            args.output,
            config.output.width,
            config.output.height,
            config.video,
            config.color,
        )
    elif config.video.output_codec == "dpx12-sequence":
        encoder = DpxSequenceEncoder(
            args.output,
            config.output.width,
            config.output.height,
            config.video,
            config.color,
        )
    else:
        encoder = VideoEncoder(
            args.output,
            config.output.width,
            config.output.height,
            config.video,
        )
    frame_shape = (config.output.height, config.output.width, 3)
    with tempfile.TemporaryDirectory(prefix="vpstitch-") as temporary:
        frame_path = Path(temporary) / "frame.rgb48"
        destination_dtype = (
            np.float16
            if config.video.output_codec == "exr-half-sequence"
            else np.uint16
        )
        destination = np.memmap(
            frame_path, dtype=destination_dtype, mode="w+", shape=frame_shape
        )
        frame_index = 0
        try:
            while config.video.frames is None or frame_index < config.video.frames:
                # Each decoder owns a persistent frame buffer. The stitcher
                # consumes all five before the next read, so avoid allocating
                # and copying another 16-bit full frame per camera here.
                sources = [decoder.read(copy=False) for decoder in decoders]
                if any(source is None for source in sources):
                    if (
                        config.video.frames is not None
                        and frame_index < config.video.frames
                    ):
                        ended = [
                            config.cameras[index].name
                            for index, source in enumerate(sources)
                            if source is None
                        ]
                        raise OSError(
                            "input ended before the requested frame count: "
                            + ", ".join(ended)
                        )
                    break
                print(f"frame {frame_index}")
                stitcher.stitch_arrays(
                    sources,  # type: ignore[arg-type]
                    destination,
                    frame_index=frame_index,
                    progress=_progress,
                )
                encoder.write(destination)
                frame_index += 1
        finally:
            for decoder in decoders:
                decoder.close()
            try:
                encoder.close()
            finally:
                destination.flush()
                mapping = getattr(destination, "_mmap", None)
                if mapping is not None:
                    mapping.close()
    print(f"encoded {frame_index} frames -> {args.output}")


def _extract_reference(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if config.video is None:
        raise ConfigError("video section is required for extract-reference")
    if len(args.inputs) != len(config.cameras):
        raise ConfigError(f"expected {len(config.cameras)} input videos")
    if not 0.0 < args.scale <= 1.0:
        raise ConfigError("--scale must be greater than 0 and at most 1")
    probes = [probe_video(path) for path in args.inputs]
    report = assess_inputs(
        probes,
        config,
        allow_low_bit_depth=args.allow_low_bit_depth,
    )
    errors = [issue.message for issue in report.issues if issue.severity == "error"]
    if errors:
        raise ConfigError("input preflight failed: " + " | ".join(errors))
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    planned_starts, aligned_frames = _planned_decoder_starts(
        config, args.inputs, args.alignment_plan
    )
    reference_frame = int(round(args.time * config.video.fps))
    requested_frame = args.start_frame + reference_frame
    if aligned_frames is not None and requested_frame >= aligned_frames:
        raise ConfigError("reference time is outside the aligned common range")
    decoders = []
    for index, (path, camera, probe) in enumerate(
        zip(args.inputs, config.cameras, probes, strict=True)
    ):
        if planned_starts is None:
            decoder_camera = camera
            decoder_start = requested_frame
        else:
            decoder_camera = replace(camera, frame_offset=0)
            decoder_start = planned_starts[index] + requested_frame
        decoders.append(
            VideoDecoder(
                path,
                decoder_camera,
                config.video.fps,
                start_frame=decoder_start,
                source_fps=probe.fps,
                exact_frame_seek=True,
                output_size=(
                    max(1, int(round(camera.width * args.scale))),
                    max(1, int(round(camera.height * args.scale))),
                ),
            )
        )
    written: list[str] = []
    try:
        for decoder, camera in zip(decoders, config.cameras, strict=True):
            frame = decoder.read()
            if frame is None:
                raise OSError(f"{camera.name}: no frame available at {args.time:.3f} seconds")
            destination = output / f"{camera.name}.png"
            if destination.exists():
                raise OSError(f"refusing to overwrite existing reference: {destination}")
            write_png(destination, frame)
            written.append(str(destination))
    finally:
        for decoder in decoders:
            decoder.close()
    manifest = {
        "time_seconds": args.time,
        "fps": config.video.fps,
        "reference_scale": args.scale,
        "frame_offsets": {
            camera.name: camera.frame_offset for camera in config.cameras
        },
        "references": written,
        "inputs": [probe.to_dict() for probe in probes],
    }
    (output / "reference_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps(manifest, indent=2))


def _build_maps(args: argparse.Namespace) -> None:
    config = _apply_canvas_overrides(load_config(args.config), args)
    cache = MapCache(config, args.map_cache)
    cache.open(build=True, progress=_progress)
    print(f"projection maps ready: {cache.directory}")


def _analyze_canvas(args: argparse.Namespace) -> None:
    config = _apply_canvas_overrides(load_config(args.config), args)
    report, mask = analyze_canvas(config)
    if args.mask:
        write_coverage_mask(args.mask, mask)
    print(json.dumps(report.to_dict(), indent=2))


def _probe_inputs(args: argparse.Namespace) -> None:
    config = load_config(args.config) if args.config else None
    report = assess_inputs(
        [probe_video(path) for path in args.inputs],
        config,
        allow_low_bit_depth=args.allow_low_bit_depth,
    )
    payload = json.dumps(report.to_dict(), indent=2)
    if args.output:
        Path(args.output).write_text(payload, encoding="utf-8")
    print(payload)


def _align_timecode(args: argparse.Namespace) -> None:
    probes = [probe_video(path, count_frames=True) for path in args.inputs]
    if args.config:
        config = load_config(args.config)
        if len(probes) != len(config.cameras):
            raise ConfigError(
                f"Config expects {len(config.cameras)} cameras, got {len(probes)}."
            )
        if config.video is not None and any(
            abs(probe.fps - config.video.fps) > 0.001 for probe in probes
        ):
            raise ConfigError(
                f"input fps does not match configured {config.video.fps:g} fps"
            )
    alignment = align_by_timecode(probes)
    payload = {
        **alignment.to_dict(),
        "probes": [probe.to_dict() for probe in probes],
    }
    encoded = json.dumps(payload, indent=2)
    if args.output:
        Path(args.output).write_text(encoded, encoding="utf-8")
    print(encoded)


def _pattern(value: str) -> tuple[int, int]:
    match = re.fullmatch(r"\s*(\d+)\s*[xX]\s*(\d+)\s*", value)
    if not match:
        raise argparse.ArgumentTypeError("pattern must look like 9x6")
    return int(match.group(1)), int(match.group(2))


def _calibrate_lens(args: argparse.Namespace) -> None:
    result = calibrate_checkerboard(
        args.images,
        args.pattern[0],
        args.pattern[1],
        args.square_size,
        args.model,
    )
    result.write(args.output)
    print(json.dumps(result.to_dict(), indent=2))


def _calibrate_rig(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    _, alignment = calibrate_rig_rotation(
        config,
        args.images,
        max_dimension=args.analysis_size,
        feature_count=args.features,
        match_ratio=args.match_ratio,
        angular_threshold_deg=args.angular_threshold,
        max_correction_deg=args.max_correction,
    )
    write_calibrated_config(args.config, args.output, alignment)
    if args.report:
        alignment.write_report(args.report)
    print(json.dumps(alignment.to_dict(), indent=2))


def _estimate_resources(args: argparse.Namespace) -> None:
    config = _apply_canvas_overrides(load_config(args.config), args)
    print(json.dumps(estimate_resources(config).to_dict(), indent=2))


def _list_ocio_spaces(args: argparse.Namespace) -> None:
    try:
        import PyOpenColorIO as ocio

        config = load_ocio_config(args.ocio_config)
        payload = {
            "config": args.ocio_config,
            "name": config.getName(),
            "colorspaces": list(config.getColorSpaceNames()),
        }
    except Exception as error:
        raise ValueError(f"unable to inspect OCIO config {args.ocio_config}: {error}") from error
    print(json.dumps(payload, indent=2))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vpstitch",
        description="High-bit-depth fixed-rig 180-degree stitcher",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    frame = subparsers.add_parser("stitch-frame", help="stitch still frames to 16-bit PNG")
    frame.add_argument("--config", required=True)
    frame.add_argument("--output", required=True, help="16-bit RGB PNG")
    frame.add_argument("inputs", nargs="+")
    _add_canvas_arguments(frame)
    frame.set_defaults(function=_stitch_frame)

    video = subparsers.add_parser("stitch-video", help="stitch five video files")
    video.add_argument("--config", required=True)
    video.add_argument("--output", required=True)
    video.add_argument("--map-cache", default=".vpstitch-cache")
    video.add_argument(
        "--alignment-plan",
        help="JSON generated by align-timecode; applies per-source TC skips",
    )
    video.add_argument(
        "--frames",
        type=int,
        help="limit the render to this many frames without changing the config file",
    )
    video.add_argument(
        "--start-frame",
        type=int,
        default=0,
        help="skip this many frames on the aligned common timeline",
    )
    video.add_argument(
        "--allow-low-bit-depth",
        action="store_true",
        help="continue with 8-bit or other sub-10-bit inputs, with an explicit warning",
    )
    video.add_argument("inputs", nargs="+")
    _add_canvas_arguments(video)
    video.set_defaults(function=_stitch_video)

    reference = subparsers.add_parser(
        "extract-reference",
        help="extract synchronized 16-bit reference stills from the camera videos",
    )
    reference.add_argument("--config", required=True)
    reference.add_argument("--time", type=float, required=True, help="timeline seconds")
    reference.add_argument("--output-dir", required=True)
    reference.add_argument(
        "--scale",
        type=float,
        default=1.0,
        help="uniformly downscale extracted reference frames for preview/calibration",
    )
    reference.add_argument(
        "--alignment-plan",
        help="JSON generated by align-timecode; applies per-source TC skips",
    )
    reference.add_argument(
        "--start-frame",
        type=int,
        default=0,
        help="additional frame offset on the aligned common timeline",
    )
    reference.add_argument(
        "--allow-low-bit-depth",
        action="store_true",
        help="continue with 8-bit or other sub-10-bit inputs, with an explicit warning",
    )
    reference.add_argument("inputs", nargs="+")
    reference.set_defaults(function=_extract_reference)

    maps = subparsers.add_parser(
        "build-maps", help="precompute fixed-rig projection maps for video"
    )
    maps.add_argument("--config", required=True)
    maps.add_argument("--map-cache", default=".vpstitch-cache")
    _add_canvas_arguments(maps)
    maps.set_defaults(function=_build_maps)

    canvas = subparsers.add_parser(
        "analyze-canvas", help="measure valid coverage and a conservative crop"
    )
    canvas.add_argument("--config", required=True)
    canvas.add_argument("--mask", help="optional low-resolution coverage PNG")
    _add_canvas_arguments(canvas)
    canvas.set_defaults(function=_analyze_canvas)

    probe = subparsers.add_parser(
        "probe-inputs", help="check bit depth, color metadata, resolution, and fps"
    )
    probe.add_argument("--config")
    probe.add_argument("--output", help="optional JSON report path")
    probe.add_argument(
        "--allow-low-bit-depth",
        action="store_true",
        help="report sub-10-bit inputs as warnings instead of errors",
    )
    probe.add_argument("inputs", nargs="+")
    probe.set_defaults(function=_probe_inputs)

    timecode = subparsers.add_parser(
        "align-timecode",
        help="read embedded SMPTE timecode and calculate the common clip range",
    )
    timecode.add_argument("--config", help="optional rig config for count/fps validation")
    timecode.add_argument("--output", help="optional alignment JSON path")
    timecode.add_argument("inputs", nargs="+")
    timecode.set_defaults(function=_align_timecode)

    calibration = subparsers.add_parser(
        "calibrate-lens", help="calibrate pinhole/fisheye intrinsics from checkerboards"
    )
    calibration.add_argument("--model", choices=["pinhole", "fisheye_equidistant"], required=True)
    calibration.add_argument("--pattern", type=_pattern, required=True, help="inner corners, e.g. 9x6")
    calibration.add_argument("--square-size", type=float, default=1.0)
    calibration.add_argument("--output", required=True)
    calibration.add_argument("images", nargs="+")
    calibration.set_defaults(function=_calibrate_lens)

    rig = subparsers.add_parser(
        "calibrate-rig",
        help="refine adjacent camera rotations from synchronized reference frames",
    )
    rig.add_argument("--config", required=True, help="config with calibrated lens intrinsics")
    rig.add_argument("--output", required=True, help="calibrated rig config JSON")
    rig.add_argument("--report", help="optional alignment quality report JSON")
    rig.add_argument("--analysis-size", type=int, default=2000)
    rig.add_argument("--features", type=int, default=12000)
    rig.add_argument("--match-ratio", type=float, default=0.72)
    rig.add_argument("--angular-threshold", type=float, default=1.25)
    rig.add_argument("--max-correction", type=float, default=12.0)
    rig.add_argument("images", nargs="+", help="one synchronized still per camera, config order")
    rig.set_defaults(function=_calibrate_rig)

    resources = subparsers.add_parser(
        "estimate-resources",
        help="estimate RAM, projection-cache, and uncompressed sequence storage",
    )
    resources.add_argument("--config", required=True)
    _add_canvas_arguments(resources)
    resources.set_defaults(function=_estimate_resources)

    ocio_spaces = subparsers.add_parser(
        "list-ocio-spaces",
        help="list exact colorspace names available in an OCIO config",
    )
    ocio_spaces.add_argument("--ocio-config", required=True)
    ocio_spaces.set_defaults(function=_list_ocio_spaces)
    return parser


def main() -> int:
    try:
        args = build_parser().parse_args()
        args.function(args)
        return 0
    except (CalibrationError, ConfigError, ValueError, OSError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
