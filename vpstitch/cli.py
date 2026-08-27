from __future__ import annotations

import argparse
import sys
import tempfile
import json
import re
from dataclasses import asdict
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
from .imageio import ExrSequenceEncoder, read_image, write_png
from .pipeline import Stitcher
from .mapcache import MapCache
from .canvas import analyze_canvas, write_coverage_mask
from .diagnostics import assess_inputs, interpret_input_probes, resolve_passthrough_video
from .calibration import CalibrationError, calibrate_checkerboard
from .rigcalibration import calibrate_rig_rotation, write_calibrated_config
from .resources import estimate_resources
from .renderflow import FrameBundleReader, should_prefetch_decode
from .color import load_ocio_config
from .color import ColorPipeline
from .colormatch import ACESCG_LUMA_WEIGHTS, solve_color_match
from .geometry import Tile, camera_map, remap_camera
from .timecode import align_by_timecode
from .liveplayback import AlignedFramePlan


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


def _planned_decoder_starts(
    config: RigConfig,
    inputs: list[str],
    alignment_path: str | None,
) -> tuple[list[int] | None, int | None]:
    assert config.video is not None
    if not alignment_path:
        return None, None
    try:
        payload = json.loads(Path(alignment_path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("alignment payload must be an object")
        plan = AlignedFramePlan.from_payload(
            payload,
            inputs,
            config.cameras,
            config.video.fps,
        )
    except (OSError, json.JSONDecodeError, TypeError, ValueError) as error:
        raise ConfigError(
            f"invalid alignment plan {alignment_path}: {error}"
        ) from error
    return list(plan.starts), plan.common_frames


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
    interpreted_probes = interpret_input_probes(probes, config)
    diagnostic = assess_inputs(
        interpreted_probes,
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
        config = replace(
            config,
            video=resolve_passthrough_video(config.video, interpreted_probes),
        )

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

    decode_scale = float(args.decode_scale)
    if not 0.0 < decode_scale <= 1.0:
        raise ConfigError("--decode-scale must be greater than 0 and at most 1")
    if decode_scale < 1.0:
        scaled_cameras = []
        for camera in config.cameras:
            lens = camera.lens
            scaled_cameras.append(
                replace(
                    camera,
                    width=max(1, int(round(camera.width * decode_scale))),
                    height=max(1, int(round(camera.height * decode_scale))),
                    lens=replace(
                        lens,
                        fx=lens.fx * decode_scale,
                        fy=lens.fy * decode_scale,
                        cx=lens.cx * decode_scale,
                        cy=lens.cy * decode_scale,
                        circle_radius=(
                            None
                            if lens.circle_radius is None
                            else lens.circle_radius * decode_scale
                        ),
                    ),
                )
            )
        config = replace(config, cameras=tuple(scaled_cameras))

    cache = MapCache(config, args.map_cache).open(progress=_progress)
    stitcher = Stitcher(config, map_cache=cache)
    decoders: list[VideoDecoder] = []
    encoder: ExrSequenceEncoder | DpxSequenceEncoder | VideoEncoder | None = None
    destination: np.memmap | None = None
    temporary_context: tempfile.TemporaryDirectory[str] | None = None
    frame_reader: FrameBundleReader | None = None
    frame_index = 0
    cleanup_error: Exception | None = None
    try:
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
                    source_bit_depth=probe.bit_depth,
                    output_size=(camera.width, camera.height)
                    if decode_scale < 1.0
                    else None,
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
        temporary_context = tempfile.TemporaryDirectory(prefix="vpstitch-")
        frame_path = Path(temporary_context.name) / "frame.rgb48"
        destination_dtype = (
            np.float16
            if config.video.output_codec == "exr-half-sequence"
            else np.uint16
        )
        destination = np.memmap(
            frame_path, dtype=destination_dtype, mode="w+", shape=frame_shape
        )
        prefetch = should_prefetch_decode(config)
        frame_reader = FrameBundleReader(decoders, prefetch=prefetch)
        print(
            "decode scheduling: "
            + ("one-frame bounded prefetch" if prefetch else "zero-copy sequential")
        )
        while config.video.frames is None or frame_index < config.video.frames:
            prefetch_next = (
                config.video.frames is None
                or frame_index + 1 < config.video.frames
            )
            sources = frame_reader.read(prefetch_next=prefetch_next)
            if any(source is None for source in sources):
                if config.video.frames is not None and frame_index < config.video.frames:
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
            if frame_index == 0:
                print(
                    f"remap backend: {stitcher.backend_decision.backend} · "
                    f"{stitcher.backend_decision.reason}"
                )
            encoder.write(destination)
            frame_index += 1
    finally:
        preserving_error = sys.exc_info()[0] is not None
        # Close decoder pipes before joining a possible prefetch read. This
        # guarantees an FFmpeg stall cannot leave the background reader waiting
        # forever during cancellation or exception cleanup.
        for decoder in decoders:
            try:
                decoder.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        if frame_reader is not None:
            try:
                frame_reader.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        if encoder is not None:
            try:
                encoder.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        try:
            cache.close()
        except Exception as error:
            if cleanup_error is None:
                cleanup_error = error
        if destination is not None:
            try:
                destination.flush()
                mapping = getattr(destination, "_mmap", None)
                if mapping is not None:
                    mapping.close()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        if temporary_context is not None:
            try:
                temporary_context.cleanup()
            except Exception as error:
                if cleanup_error is None:
                    cleanup_error = error
        if cleanup_error is not None and not preserving_error:
            raise cleanup_error
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
        interpret_input_probes(probes, config),
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
                source_bit_depth=probe.bit_depth,
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


def _match_color(args: argparse.Namespace) -> None:
    config = load_config(args.config)
    if config.color.mode != "ocio":
        raise ConfigError("camera color matching requires OCIO mode")
    if len(args.images) != len(config.cameras):
        raise ConfigError(f"expected {len(config.cameras)} reference images")
    names = [camera.name for camera in config.cameras]
    if args.reference_camera not in names:
        raise ConfigError(
            "reference camera must be one of: " + ", ".join(names)
        )
    reference_index = names.index(args.reference_camera)
    pipeline = ColorPipeline(
        replace(config.color, match_enabled=False),
        [camera.colorspace for camera in config.cameras],
        [camera.color_gain for camera in config.cameras],
    )
    tile = Tile(0, 0, config.output.width, config.output.height)
    warped: list[np.ndarray] = []
    masks: list[np.ndarray] = []
    for index, (camera, image_path) in enumerate(
        zip(config.cameras, args.images, strict=True)
    ):
        image = read_image(image_path)
        if image.shape[:2] != (camera.height, camera.width):
            raise ConfigError(
                f"{camera.name} reference expects {camera.width}x{camera.height}, "
                f"got {image.shape[1]}x{image.shape[0]}"
            )
        working = pipeline.input_to_working(index, image, apply_match=False)
        map_x, map_y, valid, _ = camera_map(camera, tile, config.output)
        warped.append(remap_camera(working, map_x, map_y))
        masks.append(valid.astype(np.float32))
    result = solve_color_match(
        warped,
        masks,
        reference_index,
        strength=1.0,
        gain_limits=(args.minimum_gain, args.maximum_gain),
        luma_weights=(
            ACESCG_LUMA_WEIGHTS
            if config.color.working_space.casefold() == "acescg"
            else (0.2126, 0.7152, 0.0722)
        ),
        min_overlap_pixels=args.minimum_overlap,
    )
    payload = {
        "reference_camera": args.reference_camera,
        "working_space": config.color.working_space,
        "cameras": [
            {
                "name": camera.name,
                "gain": [float(value) for value in result.gains[index]],
                "confidence": float(result.confidence[index]),
                "connected": bool(result.diagnostics.connected[index]),
            }
            for index, camera in enumerate(config.cameras)
        ],
        "overlaps": [asdict(item) for item in result.diagnostics.overlaps],
        "rejected_overlaps": [list(item) for item in result.diagnostics.rejected_overlaps],
    }
    destination = Path(args.output)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=destination.parent, delete=False
    ) as handle:
        json.dump(payload, handle, indent=2)
        handle.flush()
        temporary = Path(handle.name)
    temporary.replace(destination)
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
    video.add_argument(
        "--decode-scale",
        type=float,
        default=1.0,
        help="downscale source decoding for playback proxies (0-1]",
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

    color_match = subparsers.add_parser(
        "match-color",
        help="match camera white point from aligned scene-linear overlaps",
    )
    color_match.add_argument("--config", required=True)
    color_match.add_argument("--reference-camera", required=True)
    color_match.add_argument("--output", required=True)
    color_match.add_argument("--minimum-gain", type=float, default=0.85)
    color_match.add_argument("--maximum-gain", type=float, default=1.18)
    color_match.add_argument("--minimum-overlap", type=int, default=256)
    color_match.add_argument("images", nargs="+")
    color_match.set_defaults(function=_match_color)
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
