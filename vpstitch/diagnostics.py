from __future__ import annotations

from dataclasses import asdict, dataclass, replace

from .config import ConfigError, RigConfig, Video
from .ffmpegio import VideoProbe


@dataclass(frozen=True)
class DiagnosticIssue:
    severity: str
    code: str
    message: str


@dataclass(frozen=True)
class DiagnosticReport:
    inputs: tuple[VideoProbe, ...]
    issues: tuple[DiagnosticIssue, ...]

    @property
    def passed(self) -> bool:
        return not any(issue.severity == "error" for issue in self.issues)

    def to_dict(self) -> dict[str, object]:
        return {
            "passed": self.passed,
            "inputs": [probe.to_dict() for probe in self.inputs],
            "issues": [asdict(issue) for issue in self.issues],
        }


def assess_inputs(
    probes: list[VideoProbe],
    config: RigConfig | None = None,
    *,
    allow_low_bit_depth: bool = False,
) -> DiagnosticReport:
    issues: list[DiagnosticIssue] = []
    if not probes:
        issues.append(DiagnosticIssue("error", "no-input", "No video inputs were provided."))
        return DiagnosticReport((), tuple(issues))

    for probe in probes:
        if probe.bit_depth < 10:
            issues.append(
                DiagnosticIssue(
                    "warning" if allow_low_bit_depth else "error",
                    "input-below-10bit-allowed" if allow_low_bit_depth else "input-below-10bit",
                    f"{probe.path}: {probe.pixel_format} is only {probe.bit_depth}-bit; "
                    + (
                        "continuing because --allow-low-bit-depth was supplied."
                        if allow_low_bit_depth
                        else "use --allow-low-bit-depth only when this quality trade-off is intentional."
                    ),
                )
            )
        if not probe.color_primaries or not probe.color_trc:
            issues.append(
                DiagnosticIssue(
                    "warning",
                    "missing-color-metadata",
                    f"{probe.path}: color primaries/transfer metadata is incomplete; "
                    "set it explicitly in the rig config or use an OCIO colorspace.",
                )
            )

    reference = probes[0]
    for probe in probes[1:]:
        if (probe.width, probe.height) != (reference.width, reference.height):
            issues.append(
                DiagnosticIssue(
                    "error",
                    "resolution-mismatch",
                    f"{probe.path}: {probe.width}x{probe.height} does not match "
                    f"{reference.width}x{reference.height}.",
                )
            )
        if abs(probe.fps - reference.fps) > 0.001:
            issues.append(
                DiagnosticIssue(
                    "error",
                    "fps-mismatch",
                    f"{probe.path}: {probe.fps} fps does not match {reference.fps} fps.",
                )
            )
        if probe.bit_depth != reference.bit_depth:
            issues.append(
                DiagnosticIssue(
                    "warning",
                    "bit-depth-mismatch",
                    f"{probe.path}: {probe.bit_depth}-bit differs from the first input's "
                    f"{reference.bit_depth}-bit depth.",
                )
            )
        if (
            probe.color_primaries,
            probe.color_trc,
            probe.colorspace,
            probe.color_range,
        ) != (
            reference.color_primaries,
            reference.color_trc,
            reference.colorspace,
            reference.color_range,
        ):
            issues.append(
                DiagnosticIssue(
                    "warning",
                    "color-metadata-mismatch",
                    f"{probe.path}: color metadata differs from the first camera.",
                )
            )

    if config is not None:
        if len(probes) != len(config.cameras):
            issues.append(
                DiagnosticIssue(
                    "error",
                    "camera-count-mismatch",
                    f"Config expects {len(config.cameras)} cameras, got {len(probes)}.",
                )
            )
        for camera, probe in zip(config.cameras, probes):
            if (probe.width, probe.height) != (camera.width, camera.height):
                issues.append(
                    DiagnosticIssue(
                        "error",
                        "config-resolution-mismatch",
                        f"{camera.name} expects {camera.width}x{camera.height}, but "
                        f"{probe.path} is {probe.width}x{probe.height}.",
                    )
                )

    return DiagnosticReport(tuple(probes), tuple(issues))


def resolve_passthrough_video(video: Video, probes: list[VideoProbe]) -> Video:
    """Propagate matching input color tags without silently relabelling pixels."""

    if not probes:
        raise ConfigError("cannot resolve passthrough metadata without input probes")
    reference = probes[0]
    replacements: dict[str, str] = {}
    fields = ("color_primaries", "color_trc", "colorspace", "color_range")
    for field in fields:
        configured = getattr(video, field)
        detected = getattr(reference, field)
        if configured and detected and configured != detected:
            raise ConfigError(
                f"passthrough cannot relabel {field} from {detected} to {configured}; "
                "use matching tags or an OCIO transform"
            )
        if configured is None and detected is not None:
            replacements[field] = detected
    return replace(video, **replacements)
