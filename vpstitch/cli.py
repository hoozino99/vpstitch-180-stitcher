from __future__ import annotations

import argparse
import sys
import tempfile
import json
import re
from dataclasses import replace
from pathlib import Path

import numpy as np
import tifffile

from .config import (
    ConfigError,
    MAX_CANVAS_HEIGHT,
    MAX_CANVAS_WIDTH,
    RigConfig,
    load_config,
)
from .ffmpegio import VideoDecoder, VideoEncoder, probe_video
from .imageio import ExrSequenceEncoder, TiffSequenceEncoder
from .pipeline import Stitcher
from .mapcache import MapCache
from .canvas import analyze_canvas, write_coverage_mask
from .diagnostics import assess_inputs, resolve_passthrough_video
from .calibration import CalibrationError, calibrate_checkerboard
from .rigcalibration import calibrate_rig_rotation, write_calibrated_config
from .resources import estimate_resources


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

    cache = MapCache(config, args.map_cache).open(progress=_progress)
    stitcher = Stitcher(config, map_cache=cache)
    decoders = [
        VideoDecoder(path, camera, config.video.fps)
        for path, camera in zip(args.inputs, config.cameras, strict=True)
    ]
    if config.video.output_codec == "tiff16-sequence":
        encoder = TiffSequenceEncoder(
            args.output,
            config.output.width,
            config.output.height,
            config.video,
            config.color,
        )
    elif config.video.output_codec == "exr-half-sequence":
        encoder = ExrSequenceEncoder(
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
                sources = [decoder.read() for decoder in decoders]
                if any(source is None for source in sources):
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
    decoders = [
        VideoDecoder(path, camera, config.video.fps, start_time=args.time)
        for path, camera in zip(args.inputs, config.cameras, strict=True)
    ]
    written: list[str] = []
    try:
        for decoder, camera in zip(decoders, config.cameras, strict=True):
            frame = decoder.read()
            if frame is None:
                raise OSError(f"{camera.name}: no frame available at {args.time:.3f} seconds")
            destination = output / f"{camera.name}.tif"
            if destination.exists():
                raise OSError(f"refusing to overwrite existing reference: {destination}")
            tifffile.imwrite(
                destination,
                frame,
                photometric="rgb",
                bigtiff=True,
                metadata=None,
            )
            written.append(str(destination))
    finally:
        for decoder in decoders:
            decoder.close()
    manifest = {
        "time_seconds": args.time,
        "fps": config.video.fps,
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

        config = ocio.Config.CreateFromFile(args.ocio_config)
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

    frame = subparsers.add_parser("stitch-frame", help="stitch TIFF/EXR still frames")
    frame.add_argument("--config", required=True)
    frame.add_argument("--output", required=True, help="16-bit RGB BigTIFF")
    frame.add_argument("inputs", nargs="+")
    _add_canvas_arguments(frame)
    frame.set_defaults(function=_stitch_frame)

    video = subparsers.add_parser("stitch-video", help="stitch five video files")
    video.add_argument("--config", required=True)
    video.add_argument("--output", required=True)
    video.add_argument("--map-cache", default=".vpstitch-cache")
    video.add_argument(
        "--frames",
        type=int,
        help="limit the render to this many frames without changing the config file",
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
