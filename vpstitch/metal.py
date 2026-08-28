from __future__ import annotations

import ctypes
from dataclasses import dataclass
import os
from pathlib import Path
import sys
import weakref

import numpy as np

from .color import load_ocio_config
from .config import Color


MAX_METAL_CAMERAS = 5
METAL_KERNEL_NAME = "vpstitch_render"
METAL_INPUT_KERNEL_NAME = "vpstitch_input"
METAL_MIN_OUTPUT_PIXELS = 512 * 512


class MetalBackendError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class MetalTexture:
    binding: int
    width: int
    height: int
    dimensions: int
    channels: int
    linear: bool
    values: np.ndarray


@dataclass(frozen=True, slots=True)
class MetalProgram:
    source: str
    textures: tuple[MetalTexture, ...]
    filter_name: str


@dataclass(frozen=True, slots=True)
class MetalInputProgram:
    source: str
    textures: tuple[MetalTexture, ...]


def _metal_library_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []
    override = os.environ.get("VPSTITCH_METAL_LIBRARY")
    if override:
        candidates.append(Path(override).expanduser())
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(str(getattr(sys, "_MEIPASS"))) / "libvpstitch_metal.dylib")
    root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            root / ".build" / "macos" / "libvpstitch_metal.dylib",
            root / "libvpstitch_metal.dylib",
        ]
    )
    return tuple(candidates)


def _load_metal_library() -> ctypes.CDLL:
    if sys.platform != "darwin":
        raise MetalBackendError("Metal is available only on macOS")
    path = next((candidate for candidate in _metal_library_candidates() if candidate.is_file()), None)
    if path is None:
        raise MetalBackendError("bundled Metal library is missing")
    library = ctypes.CDLL(str(path))
    library.vpstitch_metal_available.argtypes = []
    library.vpstitch_metal_available.restype = ctypes.c_int
    library.vpstitch_metal_create.argtypes = [ctypes.c_char_p, ctypes.c_char_p]
    library.vpstitch_metal_create.restype = ctypes.c_void_p
    library.vpstitch_metal_destroy.argtypes = [ctypes.c_void_p]
    library.vpstitch_metal_destroy.restype = None
    library.vpstitch_metal_last_error.argtypes = [ctypes.c_void_p]
    library.vpstitch_metal_last_error.restype = ctypes.c_char_p
    library.vpstitch_metal_ready.argtypes = [ctypes.c_void_p]
    library.vpstitch_metal_ready.restype = ctypes.c_int
    library.vpstitch_metal_set_texture.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.vpstitch_metal_set_texture.restype = ctypes.c_int
    library.vpstitch_metal_upload_source.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.vpstitch_metal_upload_source.restype = ctypes.c_int
    library.vpstitch_metal_set_input_pipeline.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_char_p,
        ctypes.c_char_p,
    ]
    library.vpstitch_metal_set_input_pipeline.restype = ctypes.c_int
    library.vpstitch_metal_set_input_texture.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.vpstitch_metal_set_input_texture.restype = ctypes.c_int
    library.vpstitch_metal_transform_source.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_float,
        ctypes.c_float,
        ctypes.c_float,
    ]
    library.vpstitch_metal_transform_source.restype = ctypes.c_int
    pointer_array = ctypes.POINTER(ctypes.POINTER(ctypes.c_float))
    library.vpstitch_metal_render_tile.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        pointer_array,
        pointer_array,
        pointer_array,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    library.vpstitch_metal_render_tile.restype = ctypes.c_int
    library.vpstitch_metal_prepare_tile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint32),
        pointer_array,
        pointer_array,
        pointer_array,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
    ]
    library.vpstitch_metal_prepare_tile.restype = ctypes.c_int
    library.vpstitch_metal_render_prepared_tile.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    library.vpstitch_metal_render_prepared_tile.restype = ctypes.c_int
    library.vpstitch_metal_render_prepared_frame.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.POINTER(ctypes.c_uint32),
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.c_uint32,
        ctypes.POINTER(ctypes.c_uint16),
    ]
    library.vpstitch_metal_render_prepared_frame.restype = ctypes.c_int
    if not library.vpstitch_metal_available():
        raise MetalBackendError("Metal device is unavailable")
    return library


def _output_gpu_processor(settings: Color):
    import PyOpenColorIO as ocio

    config = load_ocio_config(str(settings.ocio_config))
    if settings.output_mode == "display_view":
        transform = ocio.DisplayViewTransform(
            src=str(settings.working_space),
            display=str(settings.display),
            view=str(settings.view),
        )
        return config.getProcessor(transform).getDefaultGPUProcessor()
    return config.getProcessor(
        str(settings.working_space), str(settings.output_space)
    ).getDefaultGPUProcessor()


def _input_gpu_processor(settings: Color, camera_space: str):
    config = load_ocio_config(str(settings.ocio_config))
    return config.getProcessor(
        camera_space, str(settings.working_space)
    ).getDefaultGPUProcessor()


def _pack_texture(texture) -> MetalTexture:  # type: ignore[no-untyped-def]
    channel_name = str(texture.channel)
    channels = 1 if channel_name.endswith("TEXTURE_RED_CHANNEL") else 4
    raw = np.asarray(texture.getValues(), dtype=np.float32)
    if channels == 4:
        rgb = raw.reshape(-1, 3)
        packed = np.ones((rgb.shape[0], 4), dtype=np.float32)
        packed[:, :3] = rgb
        values = np.ascontiguousarray(packed.reshape(-1))
    else:
        values = np.ascontiguousarray(raw.reshape(-1))
    dimensions = 1 if str(texture.dimensions).endswith("TEXTURE_1D") else 2
    return MetalTexture(
        binding=int(texture.textureShaderBindingIndex),
        width=int(texture.width),
        height=int(texture.height),
        dimensions=dimensions,
        channels=channels,
        linear=str(texture.interpolation).endswith("INTERP_LINEAR"),
        values=values,
    )


def build_metal_input_program(
    settings: Color,
    camera_space: str | None,
) -> MetalInputProgram:
    if settings.mode != "ocio":
        raise MetalBackendError("Metal input rendering requires OCIO mode")
    if not camera_space:
        raise MetalBackendError("Metal camera input color space is missing")
    import PyOpenColorIO as ocio

    processor = _input_gpu_processor(settings, camera_space)
    descriptor = ocio.GpuShaderDesc.CreateShaderDesc()
    descriptor.setLanguage(ocio.GpuLanguage.GPU_LANGUAGE_MSL_2_0)
    descriptor.setFunctionName("ocio_input_transform")
    descriptor.setResourcePrefix("vp_input_ocio_")
    processor.extractGpuShaderInfo(descriptor)
    if tuple(descriptor.get3DTextures()):
        raise MetalBackendError(
            f"camera color space {camera_space!r} requires an unsupported 3D LUT"
        )
    ocio_textures = tuple(descriptor.getTextures())
    textures = tuple(_pack_texture(texture) for texture in ocio_textures)
    declarations: list[str] = []
    constructor: list[str] = []
    for texture in ocio_textures:
        dimension = (
            "texture1d"
            if str(texture.dimensions).endswith("TEXTURE_1D")
            else "texture2d"
        )
        binding = int(texture.textureShaderBindingIndex)
        declarations.extend(
            [
                f"{dimension}<float> {texture.textureName} [[texture({binding})]]",
                f"sampler {texture.samplerName} [[sampler({binding})]]",
            ]
        )
        constructor.extend([str(texture.textureName), str(texture.samplerName)])
    resource_arguments = ""
    if declarations:
        resource_arguments = ",\n    " + ",\n    ".join(declarations)
    constructor_arguments = ", ".join(constructor)
    constructor_suffix = f"({constructor_arguments})" if constructor_arguments else ""
    source = f"""
#include <metal_stdlib>
using namespace metal;

{descriptor.getShaderText()}

struct VPInputParams {{
    uint width;
    uint height;
    uint padding0;
    uint padding1;
    float gainR;
    float gainG;
    float gainB;
    float padding2;
}};

kernel void {METAL_INPUT_KERNEL_NAME}(
    device const ushort *input [[buffer(0)]],
    device float *output [[buffer(1)]],
    constant VPInputParams &params [[buffer(2)]]{resource_arguments},
    uint gid [[thread_position_in_grid]]
) {{
    uint pixels = params.width * params.height;
    if (gid >= pixels) return;
    uint offset = gid * 3u;
    float3 encoded = float3(
        float(input[offset]),
        float(input[offset + 1u]),
        float(input[offset + 2u])
    ) * (1.0f / 65535.0f);
    vp_input_ocio_ocio_input_transform ocio{constructor_suffix};
    float3 working = ocio.ocio_input_transform(float4(encoded, 1.0f)).rgb;
    working *= float3(params.gainR, params.gainG, params.gainB);
    output[offset] = working.x;
    output[offset + 1u] = working.y;
    output[offset + 2u] = working.z;
}}
"""
    return MetalInputProgram(source=source, textures=textures)


def build_metal_program(settings: Color) -> MetalProgram:
    if settings.mode != "ocio":
        raise MetalBackendError("Metal final rendering currently requires OCIO mode")
    import PyOpenColorIO as ocio

    processor = _output_gpu_processor(settings)
    descriptor = ocio.GpuShaderDesc.CreateShaderDesc()
    descriptor.setLanguage(ocio.GpuLanguage.GPU_LANGUAGE_MSL_2_0)
    descriptor.setFunctionName("ocio_transform")
    descriptor.setResourcePrefix("vp_ocio_")
    processor.extractGpuShaderInfo(descriptor)
    if tuple(descriptor.get3DTextures()):
        raise MetalBackendError("this OCIO output transform requires an unsupported 3D LUT")
    ocio_textures = tuple(descriptor.getTextures())
    textures = tuple(_pack_texture(texture) for texture in ocio_textures)
    declarations: list[str] = []
    constructor: list[str] = []
    for texture in ocio_textures:
        dimension = "texture1d" if str(texture.dimensions).endswith("TEXTURE_1D") else "texture2d"
        binding = int(texture.textureShaderBindingIndex)
        declarations.extend(
            [
                f"{dimension}<float> {texture.textureName} [[texture({binding})]]",
                f"sampler {texture.samplerName} [[sampler({binding})]]",
            ]
        )
        constructor.extend([str(texture.textureName), str(texture.samplerName)])
    resource_arguments = ""
    if declarations:
        resource_arguments = ",\n    " + ",\n    ".join(declarations)
    constructor_arguments = ", ".join(constructor)
    filter_name = (
        "lanczos4"
        if os.environ.get("VPSTITCH_METAL_FILTER", "cubic").strip().lower()
        == "lanczos4"
        else "cubic"
    )
    remap_function = (
        "vp_remap_lanczos" if filter_name == "lanczos4" else "vp_remap_cubic"
    )
    source = f"""
#include <metal_stdlib>
using namespace metal;

{descriptor.getShaderText()}

constant uint VP_MAX_CAMERAS = 5;

struct VPParams {{
    uint width;
    uint height;
    uint activeCount;
    uint frameIndex;
    uint tileX;
    uint tileY;
    uint dither;
    uint seed;
    uint outputWidth;
    uint outputOriginX;
    uint outputOriginY;
    uint outputPadding;
    uint sourceWidths[VP_MAX_CAMERAS];
    uint sourceHeights[VP_MAX_CAMERAS];
}};

inline float vp_sinc(float x) {{
    float ax = abs(x);
    if (ax < 1.0e-6f) return 1.0f;
    float p = M_PI_F * x;
    return sin(p) / p;
}}

inline float vp_lanczos(float x) {{
    float ax = abs(x);
    return ax < 4.0f ? vp_sinc(x) * vp_sinc(x * 0.25f) : 0.0f;
}}

inline float3 vp_read_rgb(
    device const float *source,
    int x,
    int y,
    uint width,
    uint height
) {{
    if (x < 0 || y < 0 || x >= int(width) || y >= int(height)) return float3(0.0f);
    uint offset = (uint(y) * width + uint(x)) * 3u;
    return float3(source[offset], source[offset + 1u], source[offset + 2u]);
}}

inline float3 vp_remap_lanczos(
    device const float *source,
    device const float *mapX,
    device const float *mapY,
    uint pixel,
    uint sourceWidth,
    uint sourceHeight
) {{
    float x = mapX[pixel];
    float y = mapY[pixel];
    if (!isfinite(x) || !isfinite(y)) return float3(0.0f);
    int baseX = int(floor(x));
    int baseY = int(floor(y));
    float wx[8];
    float wy[8];
    float sumX = 0.0f;
    float sumY = 0.0f;
    for (int index = 0; index < 8; ++index) {{
        wx[index] = vp_lanczos(x - float(baseX + index - 3));
        wy[index] = vp_lanczos(y - float(baseY + index - 3));
        sumX += wx[index];
        sumY += wy[index];
    }}
    float inverse = 1.0f / max(abs(sumX * sumY), 1.0e-12f);
    float3 result = float3(0.0f);
    for (int row = 0; row < 8; ++row) {{
        for (int column = 0; column < 8; ++column) {{
            result += vp_read_rgb(
                source,
                baseX + column - 3,
                baseY + row - 3,
                sourceWidth,
                sourceHeight
            ) * (wx[column] * wy[row]);
        }}
    }}
    return result * inverse;
}}

inline float vp_cubic(float x) {{
    float ax = abs(x);
    if (ax <= 1.0f) return ((1.5f * ax - 2.5f) * ax) * ax + 1.0f;
    if (ax < 2.0f) return ((-0.5f * ax + 2.5f) * ax - 4.0f) * ax + 2.0f;
    return 0.0f;
}}

inline float3 vp_remap_cubic(
    device const float *source,
    device const float *mapX,
    device const float *mapY,
    uint pixel,
    uint sourceWidth,
    uint sourceHeight
) {{
    float x = mapX[pixel];
    float y = mapY[pixel];
    if (!isfinite(x) || !isfinite(y)) return float3(0.0f);
    int baseX = int(floor(x));
    int baseY = int(floor(y));
    float3 result = float3(0.0f);
    float sum = 0.0f;
    for (int row = -1; row <= 2; ++row) {{
        float wy = vp_cubic(y - float(baseY + row));
        for (int column = -1; column <= 2; ++column) {{
            float weight = vp_cubic(x - float(baseX + column)) * wy;
            result += vp_read_rgb(
                source,
                baseX + column,
                baseY + row,
                sourceWidth,
                sourceHeight
            ) * weight;
            sum += weight;
        }}
    }}
    return result / max(abs(sum), 1.0e-12f);
}}

inline uint vp_hash(uint value) {{
    value ^= value >> 16;
    value *= 0x7feb352du;
    value ^= value >> 15;
    value *= 0x846ca68bu;
    return value ^ (value >> 16);
}}

inline float vp_random(uint value) {{
    return float(vp_hash(value) & 0x00ffffffu) * (1.0f / 16777216.0f);
}}

kernel void {METAL_KERNEL_NAME}(
    device ushort *output [[buffer(0)]],
    constant VPParams &params [[buffer(1)]],
    device const float *source0 [[buffer(2)]],
    device const float *source1 [[buffer(3)]],
    device const float *source2 [[buffer(4)]],
    device const float *source3 [[buffer(5)]],
    device const float *source4 [[buffer(6)]],
    device const float *mapX0 [[buffer(7)]],
    device const float *mapX1 [[buffer(8)]],
    device const float *mapX2 [[buffer(9)]],
    device const float *mapX3 [[buffer(10)]],
    device const float *mapX4 [[buffer(11)]],
    device const float *mapY0 [[buffer(12)]],
    device const float *mapY1 [[buffer(13)]],
    device const float *mapY2 [[buffer(14)]],
    device const float *mapY3 [[buffer(15)]],
    device const float *mapY4 [[buffer(16)]],
    device const float *weight0 [[buffer(17)]],
    device const float *weight1 [[buffer(18)]],
    device const float *weight2 [[buffer(19)]],
    device const float *weight3 [[buffer(20)]],
    device const float *weight4 [[buffer(21)]]{resource_arguments},
    uint2 gid [[thread_position_in_grid]]
) {{
    if (gid.x >= params.width || gid.y >= params.height) return;
    uint pixel = gid.y * params.width + gid.x;
    float3 accumulated = float3(0.0f);
    float weightSum = 0.0f;
#define VP_SAMPLE_SLOT(SLOT) \
    if (params.activeCount > SLOT) {{ \
        float weight = weight##SLOT[pixel]; \
        if (weight > 0.0f) {{ \
            accumulated += {remap_function}(source##SLOT, mapX##SLOT, mapY##SLOT, pixel, params.sourceWidths[SLOT], params.sourceHeights[SLOT]) * weight; \
            weightSum += weight; \
        }} \
    }}
    VP_SAMPLE_SLOT(0)
    VP_SAMPLE_SLOT(1)
    VP_SAMPLE_SLOT(2)
    VP_SAMPLE_SLOT(3)
    VP_SAMPLE_SLOT(4)
#undef VP_SAMPLE_SLOT
    float3 working = weightSum > 1.0e-8f ? accumulated / weightSum : float3(0.0f);
    vp_ocio_ocio_transform ocio({constructor_arguments});
    float3 encoded = ocio.ocio_transform(float4(working, 1.0f)).rgb;
    if (params.dither != 0u) {{
        uint absoluteX = params.tileX + gid.x;
        uint absoluteY = params.tileY + gid.y;
        uint base = params.seed ^ ((params.frameIndex + 1u) * 0x9e3779b1u)
            ^ ((absoluteX + 1u) * 0x85ebca77u)
            ^ ((absoluteY + 1u) * 0xc2b2ae3du);
        float3 noise = float3(
            vp_random(base) - vp_random(base ^ 0x68bc21ebu),
            vp_random(base ^ 0x02e5be93u) - vp_random(base ^ 0x967a889bu),
            vp_random(base ^ 0xb5297a4du) - vp_random(base ^ 0x1b56c4e9u)
        ) * (1.0f / 65535.0f);
        encoded += noise;
    }}
    encoded = clamp(encoded, float3(0.0f), float3(1.0f));
    uint outputPixel = (params.outputOriginY + gid.y) * params.outputWidth
        + params.outputOriginX + gid.x;
    uint outputOffset = outputPixel * 3u;
    output[outputOffset] = ushort(round(encoded.r * 65535.0f));
    output[outputOffset + 1u] = ushort(round(encoded.g * 65535.0f));
    output[outputOffset + 2u] = ushort(round(encoded.b * 65535.0f));
}}
"""
    return MetalProgram(source=source, textures=textures, filter_name=filter_name)


class MetalStitchBackend:
    def __init__(
        self,
        settings: Color,
        camera_spaces: list[str | None],
        camera_gains: list[tuple[float, float, float]],
    ):
        if len(camera_spaces) != len(camera_gains):
            raise MetalBackendError("Metal camera spaces and gains must match")
        self._library = _load_metal_library()
        program = build_metal_program(settings)
        self.filter_name = program.filter_name
        self._context = self._library.vpstitch_metal_create(
            program.source.encode("utf-8"), METAL_KERNEL_NAME.encode("ascii")
        )
        if not self._context:
            raise MetalBackendError("Metal context allocation failed")
        self._finalizer = weakref.finalize(
            self, self._library.vpstitch_metal_destroy, self._context
        )
        if not self._library.vpstitch_metal_ready(self._context):
            raise MetalBackendError(self.last_error())
        for texture in program.textures:
            pointer = texture.values.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            if not self._library.vpstitch_metal_set_texture(
                self._context,
                texture.binding,
                pointer,
                texture.width,
                texture.height,
                texture.channels,
                texture.dimensions,
                int(texture.linear),
            ):
                raise MetalBackendError(self.last_error())
        strength = float(settings.match_strength) if settings.match_enabled else 0.0
        self._camera_gains: list[tuple[float, float, float]] = []
        for camera, (space, gain) in enumerate(
            zip(camera_spaces, camera_gains, strict=True)
        ):
            input_program = build_metal_input_program(settings, space)
            if not self._library.vpstitch_metal_set_input_pipeline(
                self._context,
                camera,
                input_program.source.encode("utf-8"),
                METAL_INPUT_KERNEL_NAME.encode("ascii"),
            ):
                raise MetalBackendError(self.last_error())
            for texture in input_program.textures:
                pointer = texture.values.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
                if not self._library.vpstitch_metal_set_input_texture(
                    self._context,
                    camera,
                    texture.binding,
                    pointer,
                    texture.width,
                    texture.height,
                    texture.channels,
                    texture.dimensions,
                    int(texture.linear),
                ):
                    raise MetalBackendError(self.last_error())
            values = np.asarray(gain, dtype=np.float32)
            effective = np.exp(np.log(np.clip(values, 1e-6, None)) * strength)
            self._camera_gains.append(
                (float(effective[0]), float(effective[1]), float(effective[2]))
            )
        self._source_dimensions: list[tuple[int, int]] = []

    def last_error(self) -> str:
        message = self._library.vpstitch_metal_last_error(self._context)
        return message.decode("utf-8", errors="replace") if message else "unknown Metal error"

    def upload_sources(self, sources: list[np.ndarray]) -> None:
        if not 1 <= len(sources) <= MAX_METAL_CAMERAS:
            raise MetalBackendError("Metal supports one to five cameras")
        dimensions: list[tuple[int, int]] = []
        for index, source in enumerate(sources):
            contiguous = np.ascontiguousarray(source, dtype=np.float32)
            height, width, channels = contiguous.shape
            if channels != 3:
                raise MetalBackendError("Metal sources must be RGB")
            if not self._library.vpstitch_metal_upload_source(
                self._context,
                index,
                contiguous.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                width,
                height,
            ):
                raise MetalBackendError(self.last_error())
            dimensions.append((width, height))
        self._source_dimensions = dimensions

    def transform_sources(self, sources: list[np.ndarray]) -> None:
        """Upload encoded uint16 plates and transform them into GPU working buffers."""
        if len(sources) != len(self._camera_gains):
            raise MetalBackendError(
                f"Metal expected {len(self._camera_gains)} cameras, got {len(sources)}"
            )
        dimensions: list[tuple[int, int]] = []
        for index, source in enumerate(sources):
            values = np.asarray(source)
            if values.ndim != 3 or values.shape[2] != 3:
                raise MetalBackendError("Metal sources must be RGB")
            if values.dtype == np.uint8:
                contiguous = np.ascontiguousarray(values, dtype=np.uint16) * np.uint16(257)
            elif values.dtype == np.uint16:
                contiguous = np.ascontiguousarray(values)
            elif np.issubdtype(values.dtype, np.floating):
                contiguous = np.rint(
                    np.clip(values, 0.0, 1.0) * 65535.0
                ).astype(np.uint16)
            else:
                raise MetalBackendError(
                    f"unsupported Metal source dtype: {values.dtype}"
                )
            height, width, _ = contiguous.shape
            gain = self._camera_gains[index]
            if not self._library.vpstitch_metal_transform_source(
                self._context,
                index,
                contiguous.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
                width,
                height,
                gain[0],
                gain[1],
                gain[2],
            ):
                raise MetalBackendError(self.last_error())
            dimensions.append((width, height))
        self._source_dimensions = dimensions

    def render_tile(
        self,
        camera_indices: list[int],
        mappings: list[tuple[np.ndarray, np.ndarray]],
        weights: list[np.ndarray],
        *,
        tile_x: int,
        tile_y: int,
        frame_index: int,
        dither: bool,
        seed: int,
    ) -> np.ndarray:
        count = len(camera_indices)
        if not 1 <= count <= MAX_METAL_CAMERAS:
            raise MetalBackendError("Metal tile must contain one to five cameras")
        if len(mappings) != count or len(weights) != count:
            raise MetalBackendError("Metal map and weight counts must match")
        height, width = weights[0].shape
        map_x_values = [np.ascontiguousarray(item[0], dtype=np.float32) for item in mappings]
        map_y_values = [np.ascontiguousarray(item[1], dtype=np.float32) for item in mappings]
        weight_values = [np.ascontiguousarray(item, dtype=np.float32) for item in weights]
        expected = (height, width)
        if any(item.shape != expected for item in (*map_x_values, *map_y_values, *weight_values)):
            raise MetalBackendError("Metal maps and weights must share one shape")
        float_pointer = ctypes.POINTER(ctypes.c_float)
        map_x_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in map_x_values)
        )
        map_y_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in map_y_values)
        )
        weight_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in weight_values)
        )
        camera_values = (ctypes.c_uint32 * count)(*camera_indices)
        source_widths = (ctypes.c_uint32 * count)(
            *(self._source_dimensions[index][0] for index in camera_indices)
        )
        source_heights = (ctypes.c_uint32 * count)(
            *(self._source_dimensions[index][1] for index in camera_indices)
        )
        output = np.empty((height, width, 3), dtype=np.uint16)
        if not self._library.vpstitch_metal_render_tile(
            self._context,
            camera_values,
            map_x_pointers,
            map_y_pointers,
            weight_pointers,
            source_widths,
            source_heights,
            count,
            width,
            height,
            tile_x,
            tile_y,
            frame_index,
            int(dither),
            int(seed) & 0xFFFFFFFF,
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
        ):
            raise MetalBackendError(self.last_error())
        return output

    def prepare_tile(
        self,
        tile_id: int,
        camera_indices: list[int],
        mappings: list[tuple[np.ndarray, np.ndarray]],
        weights: list[np.ndarray],
    ) -> tuple[int, int]:
        count = len(camera_indices)
        if not 1 <= count <= MAX_METAL_CAMERAS:
            raise MetalBackendError("Metal tile must contain one to five cameras")
        if len(mappings) != count or len(weights) != count:
            raise MetalBackendError("Metal map and weight counts must match")
        height, width = weights[0].shape
        map_x_values = [np.ascontiguousarray(item[0], dtype=np.float32) for item in mappings]
        map_y_values = [np.ascontiguousarray(item[1], dtype=np.float32) for item in mappings]
        weight_values = [np.ascontiguousarray(item, dtype=np.float32) for item in weights]
        expected = (height, width)
        if any(item.shape != expected for item in (*map_x_values, *map_y_values, *weight_values)):
            raise MetalBackendError("Metal maps and weights must share one shape")
        float_pointer = ctypes.POINTER(ctypes.c_float)
        map_x_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in map_x_values)
        )
        map_y_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in map_y_values)
        )
        weight_pointers = (float_pointer * count)(
            *(item.ctypes.data_as(float_pointer) for item in weight_values)
        )
        camera_values = (ctypes.c_uint32 * count)(*camera_indices)
        source_widths = (ctypes.c_uint32 * count)(
            *(self._source_dimensions[index][0] for index in camera_indices)
        )
        source_heights = (ctypes.c_uint32 * count)(
            *(self._source_dimensions[index][1] for index in camera_indices)
        )
        if not self._library.vpstitch_metal_prepare_tile(
            self._context,
            int(tile_id),
            camera_values,
            map_x_pointers,
            map_y_pointers,
            weight_pointers,
            source_widths,
            source_heights,
            count,
            width,
            height,
        ):
            raise MetalBackendError(self.last_error())
        return width, height

    def render_prepared_tile(
        self,
        tile_id: int,
        width: int,
        height: int,
        *,
        tile_x: int,
        tile_y: int,
        frame_index: int,
        dither: bool,
        seed: int,
    ) -> np.ndarray:
        output = np.empty((height, width, 3), dtype=np.uint16)
        if not self._library.vpstitch_metal_render_prepared_tile(
            self._context,
            int(tile_id),
            tile_x,
            tile_y,
            frame_index,
            int(dither),
            int(seed) & 0xFFFFFFFF,
            output.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
        ):
            raise MetalBackendError(self.last_error())
        return output

    def render_prepared_frame(
        self,
        tiles: list[tuple[int, int, int]],
        destination: np.ndarray,
        *,
        frame_index: int,
        dither: bool,
        seed: int,
    ) -> None:
        if destination.dtype != np.uint16 or destination.ndim != 3:
            raise MetalBackendError("Metal frame destination must be uint16 RGB")
        height, width, channels = destination.shape
        if channels != 3 or not destination.flags.c_contiguous:
            raise MetalBackendError("Metal frame destination must be contiguous RGB")
        if not tiles:
            destination.fill(0)
            return
        tile_ids = (ctypes.c_uint32 * len(tiles))(*(item[0] for item in tiles))
        tile_xs = (ctypes.c_uint32 * len(tiles))(*(item[1] for item in tiles))
        tile_ys = (ctypes.c_uint32 * len(tiles))(*(item[2] for item in tiles))
        if not self._library.vpstitch_metal_render_prepared_frame(
            self._context,
            tile_ids,
            tile_xs,
            tile_ys,
            len(tiles),
            width,
            height,
            frame_index,
            int(dither),
            int(seed) & 0xFFFFFFFF,
            destination.ctypes.data_as(ctypes.POINTER(ctypes.c_uint16)),
        ):
            raise MetalBackendError(self.last_error())


def create_metal_backend(
    settings: Color,
    camera_spaces: list[str | None],
    camera_gains: list[tuple[float, float, float]],
) -> MetalStitchBackend | None:
    preference = os.environ.get("VPSTITCH_GPU_BACKEND", "auto").strip().lower()
    remap_preference = os.environ.get("VPSTITCH_REMAP_BACKEND", "auto").strip().lower()
    if preference == "cpu" or remap_preference in {"cpu", "opencl"}:
        return None
    camera_count = len(camera_spaces)
    if (
        sys.platform != "darwin"
        or not 1 <= camera_count <= MAX_METAL_CAMERAS
        or len(camera_gains) != camera_count
    ):
        return None
    try:
        return MetalStitchBackend(settings, camera_spaces, camera_gains)
    except (MetalBackendError, OSError, ValueError):
        if preference == "metal":
            raise
        return None
