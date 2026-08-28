#import <Foundation/Foundation.h>
#import <Metal/Metal.h>
#import <AVFoundation/AVFoundation.h>
#import <CoreVideo/CoreVideo.h>
#import <IOSurface/IOSurface.h>

#include <algorithm>
#include <cstdint>
#include <cstring>
#include <string>

constexpr int kMaxCameras = 5;
constexpr int kMaxTextures = 16;

struct RenderParams {
    uint32_t width;
    uint32_t height;
    uint32_t activeCount;
    uint32_t frameIndex;
    uint32_t tileX;
    uint32_t tileY;
    uint32_t dither;
    uint32_t seed;
    uint32_t outputWidth;
    uint32_t outputOriginX;
    uint32_t outputOriginY;
    uint32_t outputPadding;
    uint32_t sourceWidths[kMaxCameras];
    uint32_t sourceHeights[kMaxCameras];
};

struct InputParams {
    uint32_t width;
    uint32_t height;
    uint32_t padding0;
    uint32_t padding1;
    float gainR;
    float gainG;
    float gainB;
    float padding2;
};

struct EncodeParams {
    uint32_t width;
    uint32_t height;
    uint32_t frameIndex;
    uint32_t dither;
    uint32_t seed;
    uint32_t matrix2020;
    uint32_t lumaOffsetWords;
    uint32_t chromaOffsetWords;
    uint32_t lumaStrideWords;
    uint32_t chromaStrideWords;
};

@interface VPStitchMetalInputTransform : NSObject
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) NSMutableArray<id<MTLTexture>> *textures;
@property(nonatomic, strong) NSMutableArray<id<MTLSamplerState>> *samplers;
@end

@implementation VPStitchMetalInputTransform
@end

@interface VPStitchMetalTile : NSObject
@property(nonatomic) uint32_t width;
@property(nonatomic) uint32_t height;
@property(nonatomic, strong) NSArray<NSNumber *> *cameraIndices;
@property(nonatomic, strong) NSArray<NSNumber *> *sourceWidths;
@property(nonatomic, strong) NSArray<NSNumber *> *sourceHeights;
@property(nonatomic, strong) NSArray<id<MTLBuffer>> *mapXBuffers;
@property(nonatomic, strong) NSArray<id<MTLBuffer>> *mapYBuffers;
@property(nonatomic, strong) NSArray<id<MTLBuffer>> *weightBuffers;
@end

@implementation VPStitchMetalTile
@end

@interface VPStitchMetalContext : NSObject
@property(nonatomic, strong) id<MTLDevice> device;
@property(nonatomic, strong) id<MTLCommandQueue> queue;
@property(nonatomic, strong) id<MTLComputePipelineState> pipeline;
@property(nonatomic, strong) NSMutableArray<id<MTLBuffer>> *sources;
@property(nonatomic, strong) NSMutableArray<VPStitchMetalInputTransform *> *inputTransforms;
@property(nonatomic, strong) NSMutableArray<id<MTLTexture>> *textures;
@property(nonatomic, strong) NSMutableArray<id<MTLSamplerState>> *samplers;
@property(nonatomic, strong) NSMutableDictionary<NSNumber *, VPStitchMetalTile *> *preparedTiles;
@property(nonatomic, strong) id<MTLBuffer> frameOutput;
@property(nonatomic) NSUInteger frameOutputLength;
@property(nonatomic, copy) NSString *lastError;
@end

@implementation VPStitchMetalContext
@end

@interface VPStitchMetalVideoEncoder : NSObject
@property(nonatomic, strong) AVAssetWriter *writer;
@property(nonatomic, strong) AVAssetWriterInput *input;
@property(nonatomic, strong) AVAssetWriterInputPixelBufferAdaptor *adaptor;
@property(nonatomic) CMVideoFormatDescriptionRef formatDescription;
@property(nonatomic, strong) id<MTLComputePipelineState> conversionPipeline;
@property(nonatomic) uint32_t width;
@property(nonatomic) uint32_t height;
@property(nonatomic) uint32_t fpsNumerator;
@property(nonatomic) uint32_t fpsDenominator;
@property(nonatomic) uint64_t nextFrame;
@property(nonatomic) BOOL matrix2020;
@property(nonatomic) CFStringRef colorPrimariesAttachment;
@property(nonatomic) CFStringRef transferFunctionAttachment;
@property(nonatomic) CFStringRef yCbCrMatrixAttachment;
@property(nonatomic) BOOL ready;
@property(nonatomic) BOOL finished;
@property(atomic) BOOL cancelled;
@property(nonatomic, copy) NSString *lastError;
@end

@implementation VPStitchMetalVideoEncoder
- (void)dealloc {
    if (_formatDescription != nullptr) {
        CFRelease(_formatDescription);
        _formatDescription = nullptr;
    }
}
@end

static void setError(VPStitchMetalContext *context, NSString *message) {
    if (context != nil) {
        context.lastError = message ?: @"unknown Metal error";
    }
}

static void setEncoderError(VPStitchMetalVideoEncoder *encoder, NSString *message) {
    if (encoder != nil) {
        encoder.lastError = message ?: @"unknown native encoder error";
    }
}

static CFStringRef colorPrimariesAttachment(const char *value) {
    if (value != nullptr &&
        (std::strcmp(value, "smpte432") == 0 || std::strcmp(value, "p3-d65") == 0)) {
        return kCVImageBufferColorPrimaries_P3_D65;
    }
    if (value != nullptr && std::strcmp(value, "bt2020") == 0) {
        return kCVImageBufferColorPrimaries_ITU_R_2020;
    }
    return kCVImageBufferColorPrimaries_ITU_R_709_2;
}

static CFStringRef transferFunctionAttachment(const char *value) {
    if (value != nullptr && std::strcmp(value, "smpte2084") == 0) {
        return kCVImageBufferTransferFunction_SMPTE_ST_2084_PQ;
    }
    if (value != nullptr && std::strcmp(value, "arib-std-b67") == 0) {
        return kCVImageBufferTransferFunction_ITU_R_2100_HLG;
    }
    return kCVImageBufferTransferFunction_ITU_R_709_2;
}

static CFStringRef yCbCrMatrixAttachment(const char *value) {
    if (value != nullptr && std::strcmp(value, "bt2020nc") == 0) {
        return kCVImageBufferYCbCrMatrix_ITU_R_2020;
    }
    return kCVImageBufferYCbCrMatrix_ITU_R_709_2;
}

static bool waitUntilWriterReady(VPStitchMetalVideoEncoder *encoder) {
    const NSTimeInterval deadline = [NSDate timeIntervalSinceReferenceDate] + 60.0;
    while (!encoder.input.readyForMoreMediaData) {
        if (encoder.cancelled) {
            setEncoderError(encoder, @"native ProRes writer was cancelled");
            return false;
        }
        if (encoder.writer.status == AVAssetWriterStatusFailed ||
            encoder.writer.status == AVAssetWriterStatusCancelled) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes writer failed: %@",
                    encoder.writer.error.localizedDescription]
            );
            return false;
        }
        if ([NSDate timeIntervalSinceReferenceDate] >= deadline) {
            setEncoderError(encoder, @"native ProRes writer timed out waiting for input");
            return false;
        }
        [NSThread sleepForTimeInterval:0.001];
    }
    return true;
}

static bool surfacePlaneFits(
    size_t surfaceLength,
    size_t offset,
    size_t stride,
    size_t rowBytes,
    uint32_t height
) {
    if (height == 0 || offset > surfaceLength || rowBytes > surfaceLength - offset) {
        return false;
    }
    return height == 1 ||
        stride <= (surfaceLength - offset - rowBytes) / (height - 1);
}

static id<MTLBuffer> bufferWithBytes(
    VPStitchMetalContext *context,
    const void *bytes,
    NSUInteger length
) {
    if (bytes == nullptr || length == 0) {
        return nil;
    }
    return [context.device newBufferWithBytes:bytes
                                      length:length
                                     options:MTLResourceStorageModeShared];
}

static id arrayValue(NSArray *array, NSUInteger index) {
    if (index >= array.count) {
        return nil;
    }
    id value = array[index];
    return value == [NSNull null] ? nil : value;
}

static VPStitchMetalTile *prepareTile(
    VPStitchMetalContext *context,
    const uint32_t *cameraIndices,
    const float *const *mapX,
    const float *const *mapY,
    const float *const *weights,
    const uint32_t *sourceWidths,
    const uint32_t *sourceHeights,
    uint32_t activeCount,
    uint32_t width,
    uint32_t height
) {
    if (cameraIndices == nullptr || mapX == nullptr || mapY == nullptr ||
        weights == nullptr || sourceWidths == nullptr || sourceHeights == nullptr ||
        activeCount == 0 || activeCount > kMaxCameras || width == 0 || height == 0) {
        setError(context, @"invalid Metal tile description");
        return nil;
    }
    const NSUInteger pixels = static_cast<NSUInteger>(width) * height;
    NSMutableArray<NSNumber *> *indices = [NSMutableArray arrayWithCapacity:activeCount];
    NSMutableArray<NSNumber *> *widths = [NSMutableArray arrayWithCapacity:activeCount];
    NSMutableArray<NSNumber *> *heights = [NSMutableArray arrayWithCapacity:activeCount];
    NSMutableArray<id<MTLBuffer>> *mapXBuffers = [NSMutableArray arrayWithCapacity:activeCount];
    NSMutableArray<id<MTLBuffer>> *mapYBuffers = [NSMutableArray arrayWithCapacity:activeCount];
    NSMutableArray<id<MTLBuffer>> *weightBuffers = [NSMutableArray arrayWithCapacity:activeCount];
    for (uint32_t slot = 0; slot < activeCount; ++slot) {
        uint32_t camera = cameraIndices[slot];
        if (camera >= kMaxCameras || arrayValue(context.sources, camera) == nil) {
            setError(context, @"Metal source was not uploaded for prepared tile");
            return nil;
        }
        id<MTLBuffer> x = bufferWithBytes(context, mapX[slot], pixels * sizeof(float));
        id<MTLBuffer> y = bufferWithBytes(context, mapY[slot], pixels * sizeof(float));
        id<MTLBuffer> weight = bufferWithBytes(context, weights[slot], pixels * sizeof(float));
        if (x == nil || y == nil || weight == nil) {
            setError(context, @"Metal prepared map upload failed");
            return nil;
        }
        [indices addObject:@(camera)];
        [widths addObject:@(sourceWidths[slot])];
        [heights addObject:@(sourceHeights[slot])];
        [mapXBuffers addObject:x];
        [mapYBuffers addObject:y];
        [weightBuffers addObject:weight];
    }
    VPStitchMetalTile *tile = [[VPStitchMetalTile alloc] init];
    tile.width = width;
    tile.height = height;
    tile.cameraIndices = indices;
    tile.sourceWidths = widths;
    tile.sourceHeights = heights;
    tile.mapXBuffers = mapXBuffers;
    tile.mapYBuffers = mapYBuffers;
    tile.weightBuffers = weightBuffers;
    return tile;
}

static int executeTile(
    VPStitchMetalContext *context,
    VPStitchMetalTile *tile,
    uint32_t tileX,
    uint32_t tileY,
    uint32_t frameIndex,
    uint32_t dither,
    uint32_t seed,
    uint16_t *output
) {
    if (context.pipeline == nil || context.queue == nil || tile == nil || output == nullptr) {
        setError(context, @"Metal pipeline or prepared tile is not ready");
        return 0;
    }
    const NSUInteger pixels = static_cast<NSUInteger>(tile.width) * tile.height;
    id<MTLBuffer> outputBuffer = [context.device newBufferWithLength:pixels * 3 * sizeof(uint16_t)
                                                              options:MTLResourceStorageModeShared];
    id<MTLBuffer> dummy = [context.device newBufferWithLength:sizeof(float) * 3
                                                      options:MTLResourceStorageModeShared];
    if (outputBuffer == nil || dummy == nil) {
        setError(context, @"Metal output allocation failed");
        return 0;
    }
    RenderParams params = {};
    params.width = tile.width;
    params.height = tile.height;
    params.activeCount = static_cast<uint32_t>(tile.cameraIndices.count);
    params.frameIndex = frameIndex;
    params.tileX = tileX;
    params.tileY = tileY;
    params.dither = dither;
    params.seed = seed;
    params.outputWidth = tile.width;

    id<MTLCommandBuffer> commandBuffer = [context.queue commandBuffer];
    id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
    [encoder setComputePipelineState:context.pipeline];
    [encoder setBuffer:outputBuffer offset:0 atIndex:0];
    for (NSUInteger slot = 0; slot < kMaxCameras; ++slot) {
        id<MTLBuffer> source = dummy;
        id<MTLBuffer> x = dummy;
        id<MTLBuffer> y = dummy;
        id<MTLBuffer> weight = dummy;
        if (slot < tile.cameraIndices.count) {
            uint32_t camera = tile.cameraIndices[slot].unsignedIntValue;
            source = arrayValue(context.sources, camera);
            x = tile.mapXBuffers[slot];
            y = tile.mapYBuffers[slot];
            weight = tile.weightBuffers[slot];
            params.sourceWidths[slot] = tile.sourceWidths[slot].unsignedIntValue;
            params.sourceHeights[slot] = tile.sourceHeights[slot].unsignedIntValue;
        }
        if (source == nil) {
            setError(context, @"Metal source disappeared before render");
            return 0;
        }
        [encoder setBuffer:source offset:0 atIndex:2 + slot];
        [encoder setBuffer:x offset:0 atIndex:7 + slot];
        [encoder setBuffer:y offset:0 atIndex:12 + slot];
        [encoder setBuffer:weight offset:0 atIndex:17 + slot];
    }
    [encoder setBytes:&params length:sizeof(params) atIndex:1];
    for (NSUInteger binding = 0; binding < kMaxTextures; ++binding) {
        id<MTLTexture> texture = arrayValue(context.textures, binding);
        id<MTLSamplerState> sampler = arrayValue(context.samplers, binding);
        if (texture != nil) [encoder setTexture:texture atIndex:binding];
        if (sampler != nil) [encoder setSamplerState:sampler atIndex:binding];
    }
    NSUInteger threadWidth = context.pipeline.threadExecutionWidth;
    NSUInteger threadHeight = std::max<NSUInteger>(
        1,
        std::min<NSUInteger>(16, context.pipeline.maxTotalThreadsPerThreadgroup / threadWidth)
    );
    [encoder dispatchThreads:MTLSizeMake(tile.width, tile.height, 1)
       threadsPerThreadgroup:MTLSizeMake(threadWidth, threadHeight, 1)];
    [encoder endEncoding];
    [commandBuffer commit];
    [commandBuffer waitUntilCompleted];
    if (commandBuffer.status == MTLCommandBufferStatusError) {
        setError(context, [NSString stringWithFormat:@"Metal execution failed: %@",
                                                       commandBuffer.error.localizedDescription]);
        return 0;
    }
    std::memcpy(output, outputBuffer.contents, pixels * 3 * sizeof(uint16_t));
    return 1;
}

extern "C" int vpstitch_metal_available(void) {
    @autoreleasepool {
        return MTLCreateSystemDefaultDevice() != nil ? 1 : 0;
    }
}

extern "C" void *vpstitch_metal_create(
    const char *shaderSource,
    const char *kernelName
) {
    @autoreleasepool {
        if (shaderSource == nullptr || kernelName == nullptr) {
            return nullptr;
        }
        VPStitchMetalContext *context = [[VPStitchMetalContext alloc] init];
        context.device = MTLCreateSystemDefaultDevice();
        context.lastError = @"";
        if (context.device == nil) {
            context.lastError = @"Metal device is unavailable";
            return (__bridge_retained void *)context;
        }
        context.queue = [context.device newCommandQueue];
        context.sources = [NSMutableArray arrayWithCapacity:kMaxCameras];
        context.inputTransforms = [NSMutableArray arrayWithCapacity:kMaxCameras];
        for (int index = 0; index < kMaxCameras; ++index) {
            [context.sources addObject:(id)[NSNull null]];
            [context.inputTransforms addObject:(id)[NSNull null]];
        }
        context.textures = [NSMutableArray arrayWithCapacity:kMaxTextures];
        context.samplers = [NSMutableArray arrayWithCapacity:kMaxTextures];
        context.preparedTiles = [NSMutableDictionary dictionary];
        for (int index = 0; index < kMaxTextures; ++index) {
            [context.textures addObject:(id)[NSNull null]];
            [context.samplers addObject:(id)[NSNull null]];
        }

        NSError *libraryError = nil;
        NSString *source = [NSString stringWithUTF8String:shaderSource];
        MTLCompileOptions *options = [[MTLCompileOptions alloc] init];
        options.fastMathEnabled = YES;
        id<MTLLibrary> library = [context.device newLibraryWithSource:source
                                                               options:options
                                                                 error:&libraryError];
        if (library == nil) {
            setError(context, [NSString stringWithFormat:@"Metal shader compile failed: %@",
                                                           libraryError.localizedDescription]);
            return (__bridge_retained void *)context;
        }
        NSString *functionName = [NSString stringWithUTF8String:kernelName];
        id<MTLFunction> function = [library newFunctionWithName:functionName];
        if (function == nil) {
            setError(context, [NSString stringWithFormat:@"Metal kernel not found: %@",
                                                           functionName]);
            return (__bridge_retained void *)context;
        }
        NSError *pipelineError = nil;
        context.pipeline = [context.device newComputePipelineStateWithFunction:function
                                                                          error:&pipelineError];
        if (context.pipeline == nil) {
            setError(context, [NSString stringWithFormat:@"Metal pipeline failed: %@",
                                                           pipelineError.localizedDescription]);
        }
        return (__bridge_retained void *)context;
    }
}

extern "C" void vpstitch_metal_destroy(void *opaque) {
    if (opaque == nullptr) {
        return;
    }
    @autoreleasepool {
        CFBridgingRelease(opaque);
    }
}

extern "C" const char *vpstitch_metal_last_error(void *opaque) {
    if (opaque == nullptr) {
        return "Metal context is null";
    }
    VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
    return context.lastError.UTF8String ?: "unknown Metal error";
}

extern "C" int vpstitch_metal_ready(void *opaque) {
    if (opaque == nullptr) {
        return 0;
    }
    VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
    return context.device != nil && context.queue != nil && context.pipeline != nil;
}

extern "C" void *vpstitch_metal_encoder_create(
    void *opaque,
    const char *outputPath,
    uint32_t width,
    uint32_t height,
    uint32_t fpsNumerator,
    uint32_t fpsDenominator,
    const char *primaries,
    const char *transfer,
    const char *matrix,
    const char *range
) {
    @autoreleasepool {
        VPStitchMetalVideoEncoder *encoder = [[VPStitchMetalVideoEncoder alloc] init];
        encoder.lastError = @"";
        if (opaque == nullptr || outputPath == nullptr || width == 0 || height == 0 ||
            fpsNumerator == 0 || fpsDenominator == 0) {
            setEncoderError(encoder, @"invalid native ProRes encoder arguments");
            return (__bridge_retained void *)encoder;
        }
        if ((width & 1u) != 0 || range == nullptr || std::strcmp(range, "tv") != 0) {
            setEncoderError(encoder, @"native ProRes requires even-width limited-range output");
            return (__bridge_retained void *)encoder;
        }
        const bool primariesSupported = primaries != nullptr &&
            (std::strcmp(primaries, "bt709") == 0 ||
             std::strcmp(primaries, "smpte432") == 0 ||
             std::strcmp(primaries, "bt2020") == 0);
        const bool transferSupported = transfer != nullptr &&
            (std::strcmp(transfer, "bt709") == 0 ||
             std::strcmp(transfer, "smpte2084") == 0 ||
             std::strcmp(transfer, "arib-std-b67") == 0);
        const bool matrixSupported = matrix != nullptr &&
            (std::strcmp(matrix, "bt709") == 0 ||
             std::strcmp(matrix, "bt2020nc") == 0);
        if (!primariesSupported || !transferSupported || !matrixSupported) {
            setEncoderError(
                encoder,
                @"native ProRes requires explicit supported primaries, transfer, and "
                 "non-constant-luminance matrix metadata"
            );
            return (__bridge_retained void *)encoder;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        if (context.device == nil || context.queue == nil || context.pipeline == nil) {
            setEncoderError(encoder, @"Metal context is not ready for native ProRes");
            return (__bridge_retained void *)encoder;
        }

        @try {
            NSString *path = [NSString stringWithUTF8String:outputPath];
            if (path == nil) {
                setEncoderError(encoder, @"native ProRes output path is not valid UTF-8");
                return (__bridge_retained void *)encoder;
            }
            NSURL *url = [NSURL fileURLWithPath:path];
            NSError *removeError = nil;
            if ([[NSFileManager defaultManager] fileExistsAtPath:path] &&
                ![[NSFileManager defaultManager] removeItemAtURL:url error:&removeError]) {
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:@"could not replace native ProRes output: %@",
                        removeError.localizedDescription]
                );
                return (__bridge_retained void *)encoder;
            }
            NSError *writerError = nil;
            encoder.writer = [[AVAssetWriter alloc] initWithURL:url
                                                       fileType:AVFileTypeQuickTimeMovie
                                                          error:&writerError];
            if (encoder.writer == nil) {
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:@"native ProRes writer creation failed: %@",
                        writerError.localizedDescription]
                );
                return (__bridge_retained void *)encoder;
            }

            NSMutableDictionary *settings = [@{
                AVVideoCodecKey: AVVideoCodecTypeAppleProRes422HQ,
                AVVideoWidthKey: @(width),
                AVVideoHeightKey: @(height),
            } mutableCopy];
            if (![encoder.writer canApplyOutputSettings:settings forMediaType:AVMediaTypeVideo]) {
                setEncoderError(encoder, @"native ProRes HQ settings are not supported");
                return (__bridge_retained void *)encoder;
            }
            encoder.input = [AVAssetWriterInput assetWriterInputWithMediaType:AVMediaTypeVideo
                                                                outputSettings:settings];
            encoder.input.expectsMediaDataInRealTime = NO;
            encoder.input.mediaTimeScale = fpsNumerator;
            if (![encoder.writer canAddInput:encoder.input]) {
                setEncoderError(encoder, @"native ProRes input cannot be added to the writer");
                return (__bridge_retained void *)encoder;
            }
            [encoder.writer addInput:encoder.input];

            NSDictionary *attributes = @{
                (NSString *)kCVPixelBufferPixelFormatTypeKey:
                    @(kCVPixelFormatType_422YpCbCr10BiPlanarVideoRange),
                (NSString *)kCVPixelBufferWidthKey: @(width),
                (NSString *)kCVPixelBufferHeightKey: @(height),
                (NSString *)kCVPixelBufferMetalCompatibilityKey: [NSNumber numberWithBool:YES],
                (NSString *)kCVPixelBufferIOSurfacePropertiesKey: @{},
            };
            encoder.adaptor = [AVAssetWriterInputPixelBufferAdaptor
                assetWriterInputPixelBufferAdaptorWithAssetWriterInput:encoder.input
                                            sourcePixelBufferAttributes:attributes];

            NSString *conversionSource = @
                "#include <metal_stdlib>\n"
                "using namespace metal;\n"
                "struct EncodeParams { uint width; uint height; uint frameIndex; uint dither; uint seed; uint matrix2020; uint lumaOffsetWords; uint chromaOffsetWords; uint lumaStrideWords; uint chromaStrideWords; };\n"
                "uint mixbits(uint value) { value ^= value >> 16; value *= 0x7feb352du; value ^= value >> 15; value *= 0x846ca68bu; value ^= value >> 16; return value; }\n"
                "float noise(uint x, uint y, uint component, constant EncodeParams &p) { uint value = x * 0x1f123bb5u ^ y * 0x5f356495u ^ component * 0x9e3779b9u ^ p.frameIndex * 0x85ebca6bu ^ p.seed; return (float(mixbits(value)) / 4294967295.0f) - 0.5f; }\n"
                "float3 toYuv(float3 rgb, bool use2020) { float kr = use2020 ? 0.2627f : 0.2126f; float kb = use2020 ? 0.0593f : 0.0722f; float kg = 1.0f - kr - kb; float y = dot(rgb, float3(kr, kg, kb)); return float3(y, (rgb.b - y) / (2.0f * (1.0f - kb)), (rgb.r - y) / (2.0f * (1.0f - kr))); }\n"
                "ushort code(float value) { return ushort(clamp(rint(value), 0.0f, 1023.0f)) << 6; }\n"
                "kernel void rgb16_to_x422(device const ushort *rgb [[buffer(0)]], device ushort *surface [[buffer(1)]], constant EncodeParams &p [[buffer(2)]], uint2 gid [[thread_position_in_grid]]) { if (gid.x * 2u >= p.width || gid.y >= p.height) return; uint x0 = gid.x * 2u; uint x1 = min(x0 + 1u, p.width - 1u); uint i0 = (gid.y * p.width + x0) * 3u; uint i1 = (gid.y * p.width + x1) * 3u; float3 rgb0 = float3(rgb[i0], rgb[i0 + 1u], rgb[i0 + 2u]) / 65535.0f; float3 rgb1 = float3(rgb[i1], rgb[i1 + 1u], rgb[i1 + 2u]) / 65535.0f; float3 yuv0 = toYuv(rgb0, p.matrix2020 != 0u); float3 yuv1 = toYuv(rgb1, p.matrix2020 != 0u); float d0 = p.dither != 0u ? noise(x0, gid.y, 0u, p) : 0.0f; float d1 = p.dither != 0u ? noise(x1, gid.y, 0u, p) : 0.0f; float dc = p.dither != 0u ? noise(gid.x, gid.y, 1u, p) : 0.0f; ushort y0 = code(64.0f + 876.0f * clamp(yuv0.x, 0.0f, 1.0f) + d0); ushort y1 = code(64.0f + 876.0f * clamp(yuv1.x, 0.0f, 1.0f) + d1); float cb = 0.5f * (yuv0.y + yuv1.y); float cr = 0.5f * (yuv0.z + yuv1.z); ushort u = code(512.0f + 896.0f * clamp(cb, -0.5f, 0.5f) + dc); ushort v = code(512.0f + 896.0f * clamp(cr, -0.5f, 0.5f) + dc); uint yRow = p.lumaOffsetWords + gid.y * p.lumaStrideWords; uint uvRow = p.chromaOffsetWords + gid.y * p.chromaStrideWords; surface[yRow + x0] = y0; surface[yRow + x1] = y1; surface[uvRow + gid.x * 2u] = u; surface[uvRow + gid.x * 2u + 1u] = v; }\n";
            NSError *libraryError = nil;
            id<MTLLibrary> library = [context.device newLibraryWithSource:conversionSource
                                                                   options:nil
                                                                     error:&libraryError];
            id<MTLFunction> function = [library newFunctionWithName:@"rgb16_to_x422"];
            NSError *pipelineError = nil;
            encoder.conversionPipeline = function == nil
                ? nil
                : [context.device newComputePipelineStateWithFunction:function error:&pipelineError];
            if (encoder.conversionPipeline == nil) {
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:@"native ProRes conversion pipeline failed: %@ %@",
                        libraryError.localizedDescription, pipelineError.localizedDescription]
                );
                return (__bridge_retained void *)encoder;
            }

            if (![encoder.writer startWriting]) {
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:@"native ProRes writer start failed: %@",
                        encoder.writer.error.localizedDescription]
                );
                return (__bridge_retained void *)encoder;
            }
            [encoder.writer startSessionAtSourceTime:kCMTimeZero];

            // Validate the actual pool, 20K IOSurface layout, and zero-copy Metal
            // mapping before auto-selection is reported to the caller. This keeps
            // unsupported devices on the established FFmpeg path before frame 0.
            CVPixelBufferRef preflightBuffer = nullptr;
            CVReturn preflightStatus = CVPixelBufferPoolCreatePixelBuffer(
                kCFAllocatorDefault,
                encoder.adaptor.pixelBufferPool,
                &preflightBuffer
            );
            IOSurfaceRef preflightSurface = preflightBuffer == nullptr
                ? nullptr
                : CVPixelBufferGetIOSurface(preflightBuffer);
            id<MTLBuffer> preflightMetalBuffer = nil;
            if (preflightStatus == kCVReturnSuccess && preflightSurface != nullptr &&
                IOSurfaceLock(preflightSurface, 0, nullptr) == KERN_SUCCESS) {
                void *base = IOSurfaceGetBaseAddress(preflightSurface);
                size_t length = IOSurfaceGetAllocSize(preflightSurface);
                if (base != nullptr && length > 0) {
                    preflightMetalBuffer = [context.device
                        newBufferWithBytesNoCopy:base
                        length:length
                        options:MTLResourceStorageModeShared
                        deallocator:nil];
                }
                IOSurfaceUnlock(preflightSurface, 0, nullptr);
            }
            if (preflightBuffer != nullptr) CVPixelBufferRelease(preflightBuffer);
            if (preflightStatus != kCVReturnSuccess || preflightSurface == nullptr ||
                preflightMetalBuffer == nil) {
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:
                        @"native ProRes IOSurface preflight failed (%d)", preflightStatus]
                );
                return (__bridge_retained void *)encoder;
            }
            encoder.width = width;
            encoder.height = height;
            encoder.fpsNumerator = fpsNumerator;
            encoder.fpsDenominator = fpsDenominator;
            encoder.nextFrame = 0;
            encoder.matrix2020 = std::strcmp(matrix, "bt2020nc") == 0;
            encoder.colorPrimariesAttachment = colorPrimariesAttachment(primaries);
            encoder.transferFunctionAttachment = transferFunctionAttachment(transfer);
            encoder.yCbCrMatrixAttachment = yCbCrMatrixAttachment(matrix);
            encoder.ready = YES;
            encoder.finished = NO;
            encoder.cancelled = NO;
        } @catch (NSException *exception) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes setup exception: %@", exception.reason]
            );
        }
        return (__bridge_retained void *)encoder;
    }
}

extern "C" int vpstitch_metal_encoder_ready(void *opaque) {
    if (opaque == nullptr) return 0;
    VPStitchMetalVideoEncoder *encoder = (__bridge VPStitchMetalVideoEncoder *)opaque;
    return encoder.ready && !encoder.finished;
}

extern "C" const char *vpstitch_metal_encoder_last_error(void *opaque) {
    if (opaque == nullptr) return "native ProRes encoder is null";
    VPStitchMetalVideoEncoder *encoder = (__bridge VPStitchMetalVideoEncoder *)opaque;
    return encoder.lastError.UTF8String ?: "unknown native ProRes error";
}

extern "C" int vpstitch_metal_encoder_finish(void *opaque) {
    @autoreleasepool {
        if (opaque == nullptr) return 0;
        VPStitchMetalVideoEncoder *encoder = (__bridge VPStitchMetalVideoEncoder *)opaque;
        if (encoder.finished) return encoder.writer.status == AVAssetWriterStatusCompleted;
        encoder.finished = YES;
        encoder.ready = NO;
        if (encoder.nextFrame > 0) {
            [encoder.writer endSessionAtSourceTime:CMTimeMake(
                static_cast<int64_t>(encoder.nextFrame) * encoder.fpsDenominator,
                encoder.fpsNumerator
            )];
        }
        [encoder.input markAsFinished];
        dispatch_semaphore_t semaphore = dispatch_semaphore_create(0);
        [encoder.writer finishWritingWithCompletionHandler:^{
            dispatch_semaphore_signal(semaphore);
        }];
        const int64_t timeoutNanoseconds = 120LL * NSEC_PER_SEC;
        if (dispatch_semaphore_wait(
                semaphore,
                dispatch_time(DISPATCH_TIME_NOW, timeoutNanoseconds)
            ) != 0) {
            encoder.cancelled = YES;
            [encoder.writer cancelWriting];
            setEncoderError(encoder, @"native ProRes finalize timed out after 120 seconds");
            return 0;
        }
        if (encoder.writer.status != AVAssetWriterStatusCompleted) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes finalize failed: %@",
                    encoder.writer.error.localizedDescription]
            );
            return 0;
        }
        return 1;
    }
}

extern "C" void vpstitch_metal_encoder_cancel(void *opaque) {
    if (opaque == nullptr) return;
    @autoreleasepool {
        VPStitchMetalVideoEncoder *encoder = (__bridge VPStitchMetalVideoEncoder *)opaque;
        encoder.cancelled = YES;
        encoder.ready = NO;
        if (encoder.writer.status == AVAssetWriterStatusWriting) {
            [encoder.writer cancelWriting];
        }
    }
}

extern "C" void vpstitch_metal_encoder_destroy(void *opaque) {
    if (opaque == nullptr) return;
    @autoreleasepool {
        VPStitchMetalVideoEncoder *encoder = (__bridge VPStitchMetalVideoEncoder *)opaque;
        if (!encoder.finished) vpstitch_metal_encoder_cancel(opaque);
        CFBridgingRelease(opaque);
    }
}

extern "C" int vpstitch_metal_set_texture(
    void *opaque,
    uint32_t binding,
    const float *values,
    uint32_t width,
    uint32_t height,
    uint32_t channels,
    uint32_t dimensions,
    uint32_t linear
) {
    @autoreleasepool {
        if (opaque == nullptr || values == nullptr || binding >= kMaxTextures ||
            width == 0 || height == 0 || (channels != 1 && channels != 4) ||
            (dimensions != 1 && dimensions != 2)) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        MTLPixelFormat pixelFormat = channels == 1
            ? MTLPixelFormatR32Float
            : MTLPixelFormatRGBA32Float;
        MTLTextureDescriptor *descriptor = [[MTLTextureDescriptor alloc] init];
        descriptor.textureType = dimensions == 1 ? MTLTextureType1D : MTLTextureType2D;
        descriptor.pixelFormat = pixelFormat;
        descriptor.width = width;
        descriptor.height = dimensions == 1 ? 1 : height;
        descriptor.mipmapLevelCount = 1;
        descriptor.storageMode = MTLStorageModeShared;
        descriptor.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> texture = [context.device newTextureWithDescriptor:descriptor];
        if (texture == nil) {
            setError(context, @"Metal OCIO texture allocation failed");
            return 0;
        }
        NSUInteger bytesPerPixel = channels * sizeof(float);
        MTLRegion region = dimensions == 1
            ? MTLRegionMake1D(0, width)
            : MTLRegionMake2D(0, 0, width, height);
        [texture replaceRegion:region
                   mipmapLevel:0
                     withBytes:values
                   bytesPerRow:width * bytesPerPixel];

        MTLSamplerDescriptor *samplerDescriptor = [[MTLSamplerDescriptor alloc] init];
        samplerDescriptor.minFilter = linear ? MTLSamplerMinMagFilterLinear
                                             : MTLSamplerMinMagFilterNearest;
        samplerDescriptor.magFilter = linear ? MTLSamplerMinMagFilterLinear
                                             : MTLSamplerMinMagFilterNearest;
        samplerDescriptor.sAddressMode = MTLSamplerAddressModeClampToEdge;
        samplerDescriptor.tAddressMode = MTLSamplerAddressModeClampToEdge;
        id<MTLSamplerState> sampler = [context.device newSamplerStateWithDescriptor:samplerDescriptor];
        if (sampler == nil) {
            setError(context, @"Metal OCIO sampler allocation failed");
            return 0;
        }
        context.textures[binding] = texture;
        context.samplers[binding] = sampler;
        return 1;
    }
}

extern "C" int vpstitch_metal_upload_source(
    void *opaque,
    uint32_t camera,
    const float *source,
    uint32_t width,
    uint32_t height
) {
    @autoreleasepool {
        if (opaque == nullptr || source == nullptr || camera >= kMaxCameras ||
            width == 0 || height == 0) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        NSUInteger length = static_cast<NSUInteger>(width) * height * 3 * sizeof(float);
        id<MTLBuffer> buffer = bufferWithBytes(context, source, length);
        if (buffer == nil) {
            setError(context, @"Metal source upload failed");
            return 0;
        }
        context.sources[camera] = buffer;
        return 1;
    }
}

extern "C" int vpstitch_metal_set_input_pipeline(
    void *opaque,
    uint32_t camera,
    const char *shaderSource,
    const char *kernelName
) {
    @autoreleasepool {
        if (opaque == nullptr || camera >= kMaxCameras ||
            shaderSource == nullptr || kernelName == nullptr) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        NSError *libraryError = nil;
        NSString *source = [NSString stringWithUTF8String:shaderSource];
        MTLCompileOptions *options = [[MTLCompileOptions alloc] init];
        options.fastMathEnabled = YES;
        id<MTLLibrary> library = [context.device newLibraryWithSource:source
                                                               options:options
                                                                 error:&libraryError];
        if (library == nil) {
            setError(context, [NSString stringWithFormat:
                @"Metal input shader compile failed for camera %u: %@",
                camera + 1,
                libraryError.localizedDescription]);
            return 0;
        }
        NSString *functionName = [NSString stringWithUTF8String:kernelName];
        id<MTLFunction> function = [library newFunctionWithName:functionName];
        if (function == nil) {
            setError(context, [NSString stringWithFormat:
                @"Metal input kernel not found for camera %u: %@",
                camera + 1,
                functionName]);
            return 0;
        }
        NSError *pipelineError = nil;
        id<MTLComputePipelineState> pipeline =
            [context.device newComputePipelineStateWithFunction:function
                                                           error:&pipelineError];
        if (pipeline == nil) {
            setError(context, [NSString stringWithFormat:
                @"Metal input pipeline failed for camera %u: %@",
                camera + 1,
                pipelineError.localizedDescription]);
            return 0;
        }
        VPStitchMetalInputTransform *transform = [[VPStitchMetalInputTransform alloc] init];
        transform.pipeline = pipeline;
        transform.textures = [NSMutableArray arrayWithCapacity:kMaxTextures];
        transform.samplers = [NSMutableArray arrayWithCapacity:kMaxTextures];
        for (int index = 0; index < kMaxTextures; ++index) {
            [transform.textures addObject:(id)[NSNull null]];
            [transform.samplers addObject:(id)[NSNull null]];
        }
        context.inputTransforms[camera] = transform;
        return 1;
    }
}

extern "C" int vpstitch_metal_set_input_texture(
    void *opaque,
    uint32_t camera,
    uint32_t binding,
    const float *values,
    uint32_t width,
    uint32_t height,
    uint32_t channels,
    uint32_t dimensions,
    uint32_t linear
) {
    @autoreleasepool {
        if (opaque == nullptr || values == nullptr || camera >= kMaxCameras ||
            binding >= kMaxTextures || width == 0 || height == 0 ||
            (channels != 1 && channels != 4) ||
            (dimensions != 1 && dimensions != 2)) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        VPStitchMetalInputTransform *transform = arrayValue(context.inputTransforms, camera);
        if (transform == nil) {
            setError(context, @"Metal input pipeline must be set before its texture");
            return 0;
        }
        MTLPixelFormat pixelFormat = channels == 1
            ? MTLPixelFormatR32Float
            : MTLPixelFormatRGBA32Float;
        MTLTextureDescriptor *descriptor = [[MTLTextureDescriptor alloc] init];
        descriptor.textureType = dimensions == 1 ? MTLTextureType1D : MTLTextureType2D;
        descriptor.pixelFormat = pixelFormat;
        descriptor.width = width;
        descriptor.height = dimensions == 1 ? 1 : height;
        descriptor.mipmapLevelCount = 1;
        descriptor.storageMode = MTLStorageModeShared;
        descriptor.usage = MTLTextureUsageShaderRead;
        id<MTLTexture> texture = [context.device newTextureWithDescriptor:descriptor];
        if (texture == nil) {
            setError(context, @"Metal input OCIO texture allocation failed");
            return 0;
        }
        NSUInteger bytesPerPixel = channels * sizeof(float);
        MTLRegion region = dimensions == 1
            ? MTLRegionMake1D(0, width)
            : MTLRegionMake2D(0, 0, width, height);
        [texture replaceRegion:region
                   mipmapLevel:0
                     withBytes:values
                   bytesPerRow:width * bytesPerPixel];

        MTLSamplerDescriptor *samplerDescriptor = [[MTLSamplerDescriptor alloc] init];
        samplerDescriptor.minFilter = linear ? MTLSamplerMinMagFilterLinear
                                             : MTLSamplerMinMagFilterNearest;
        samplerDescriptor.magFilter = linear ? MTLSamplerMinMagFilterLinear
                                             : MTLSamplerMinMagFilterNearest;
        samplerDescriptor.sAddressMode = MTLSamplerAddressModeClampToEdge;
        samplerDescriptor.tAddressMode = MTLSamplerAddressModeClampToEdge;
        id<MTLSamplerState> sampler =
            [context.device newSamplerStateWithDescriptor:samplerDescriptor];
        if (sampler == nil) {
            setError(context, @"Metal input OCIO sampler allocation failed");
            return 0;
        }
        transform.textures[binding] = texture;
        transform.samplers[binding] = sampler;
        return 1;
    }
}

extern "C" int vpstitch_metal_transform_source(
    void *opaque,
    uint32_t camera,
    const uint16_t *source,
    uint32_t width,
    uint32_t height,
    float gainR,
    float gainG,
    float gainB
) {
    @autoreleasepool {
        if (opaque == nullptr || source == nullptr || camera >= kMaxCameras ||
            width == 0 || height == 0) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        VPStitchMetalInputTransform *transform = arrayValue(context.inputTransforms, camera);
        if (transform == nil || transform.pipeline == nil || context.queue == nil) {
            setError(context, @"Metal input transform is not ready");
            return 0;
        }
        const NSUInteger pixels = static_cast<NSUInteger>(width) * height;
        id<MTLBuffer> input = bufferWithBytes(
            context,
            source,
            pixels * 3 * sizeof(uint16_t)
        );
        id<MTLBuffer> working = [context.device newBufferWithLength:pixels * 3 * sizeof(float)
                                                             options:MTLResourceStorageModePrivate];
        if (input == nil || working == nil) {
            setError(context, @"Metal input working-buffer allocation failed");
            return 0;
        }
        InputParams params = {};
        params.width = width;
        params.height = height;
        params.gainR = gainR;
        params.gainG = gainG;
        params.gainB = gainB;

        id<MTLCommandBuffer> commandBuffer = [context.queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:transform.pipeline];
        [encoder setBuffer:input offset:0 atIndex:0];
        [encoder setBuffer:working offset:0 atIndex:1];
        [encoder setBytes:&params length:sizeof(params) atIndex:2];
        for (NSUInteger binding = 0; binding < kMaxTextures; ++binding) {
            id<MTLTexture> texture = arrayValue(transform.textures, binding);
            id<MTLSamplerState> sampler = arrayValue(transform.samplers, binding);
            if (texture != nil) [encoder setTexture:texture atIndex:binding];
            if (sampler != nil) [encoder setSamplerState:sampler atIndex:binding];
        }
        NSUInteger threadWidth = transform.pipeline.threadExecutionWidth;
        NSUInteger groupWidth = std::max<NSUInteger>(1, std::min<NSUInteger>(
            transform.pipeline.maxTotalThreadsPerThreadgroup,
            threadWidth * 4
        ));
        [encoder dispatchThreads:MTLSizeMake(pixels, 1, 1)
           threadsPerThreadgroup:MTLSizeMake(groupWidth, 1, 1)];
        [encoder endEncoding];
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        if (commandBuffer.status == MTLCommandBufferStatusError) {
            setError(context, [NSString stringWithFormat:
                @"Metal input transform failed for camera %u: %@",
                camera + 1,
                commandBuffer.error.localizedDescription]);
            return 0;
        }
        context.sources[camera] = working;
        return 1;
    }
}

extern "C" int vpstitch_metal_prepare_tile(
    void *opaque,
    uint32_t tileID,
    const uint32_t *cameraIndices,
    const float *const *mapX,
    const float *const *mapY,
    const float *const *weights,
    const uint32_t *sourceWidths,
    const uint32_t *sourceHeights,
    uint32_t activeCount,
    uint32_t width,
    uint32_t height
) {
    @autoreleasepool {
        if (opaque == nullptr) return 0;
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        VPStitchMetalTile *tile = prepareTile(
            context,
            cameraIndices,
            mapX,
            mapY,
            weights,
            sourceWidths,
            sourceHeights,
            activeCount,
            width,
            height
        );
        if (tile == nil) return 0;
        context.preparedTiles[@(tileID)] = tile;
        return 1;
    }
}

extern "C" int vpstitch_metal_render_prepared_tile(
    void *opaque,
    uint32_t tileID,
    uint32_t tileX,
    uint32_t tileY,
    uint32_t frameIndex,
    uint32_t dither,
    uint32_t seed,
    uint16_t *output
) {
    @autoreleasepool {
        if (opaque == nullptr || output == nullptr) return 0;
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        VPStitchMetalTile *tile = context.preparedTiles[@(tileID)];
        if (tile == nil) {
            setError(context, @"Metal prepared tile was not found");
            return 0;
        }
        return executeTile(
            context,
            tile,
            tileX,
            tileY,
            frameIndex,
            dither,
            seed,
            output
        );
    }
}

extern "C" int vpstitch_metal_render_prepared_frame(
    void *opaque,
    const uint32_t *tileIDs,
    const uint32_t *tileXs,
    const uint32_t *tileYs,
    uint32_t tileCount,
    uint32_t outputWidth,
    uint32_t outputHeight,
    uint32_t frameIndex,
    uint32_t dither,
    uint32_t seed,
    uint16_t *output
) {
    @autoreleasepool {
        if (opaque == nullptr || tileIDs == nullptr || tileXs == nullptr ||
            tileYs == nullptr || tileCount == 0 || outputWidth == 0 ||
            outputHeight == 0 || output == nullptr) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        if (context.pipeline == nil || context.queue == nil) {
            setError(context, @"Metal frame pipeline is not ready");
            return 0;
        }
        const NSUInteger pixels = static_cast<NSUInteger>(outputWidth) * outputHeight;
        const NSUInteger outputLength = pixels * 3 * sizeof(uint16_t);
        if (outputLength > context.device.maxBufferLength) {
            setError(context, @"Metal frame output exceeds the device buffer limit");
            return 0;
        }
        if (context.frameOutput == nil || context.frameOutputLength != outputLength) {
            context.frameOutput = [context.device newBufferWithLength:outputLength
                                                               options:MTLResourceStorageModeShared];
            context.frameOutputLength = outputLength;
        }
        if (context.frameOutput == nil) {
            setError(context, @"Metal frame output allocation failed");
            return 0;
        }
        id<MTLBuffer> dummy = [context.device newBufferWithLength:sizeof(float) * 3
                                                          options:MTLResourceStorageModeShared];
        if (dummy == nil) {
            setError(context, @"Metal frame dummy-buffer allocation failed");
            return 0;
        }

        id<MTLCommandBuffer> commandBuffer = [context.queue commandBuffer];
        id<MTLBlitCommandEncoder> blit = [commandBuffer blitCommandEncoder];
        [blit fillBuffer:context.frameOutput range:NSMakeRange(0, outputLength) value:0];
        [blit endEncoding];

        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:context.pipeline];
        [encoder setBuffer:context.frameOutput offset:0 atIndex:0];
        for (NSUInteger binding = 0; binding < kMaxTextures; ++binding) {
            id<MTLTexture> texture = arrayValue(context.textures, binding);
            id<MTLSamplerState> sampler = arrayValue(context.samplers, binding);
            if (texture != nil) [encoder setTexture:texture atIndex:binding];
            if (sampler != nil) [encoder setSamplerState:sampler atIndex:binding];
        }

        bool failed = false;
        for (uint32_t tileIndex = 0; tileIndex < tileCount; ++tileIndex) {
            VPStitchMetalTile *tile = context.preparedTiles[@(tileIDs[tileIndex])];
            if (tile == nil || tileXs[tileIndex] + tile.width > outputWidth ||
                tileYs[tileIndex] + tile.height > outputHeight) {
                setError(context, @"Metal prepared frame tile is missing or out of bounds");
                failed = true;
                break;
            }
            RenderParams params = {};
            params.width = tile.width;
            params.height = tile.height;
            params.activeCount = static_cast<uint32_t>(tile.cameraIndices.count);
            params.frameIndex = frameIndex;
            params.tileX = tileXs[tileIndex];
            params.tileY = tileYs[tileIndex];
            params.dither = dither;
            params.seed = seed;
            params.outputWidth = outputWidth;
            params.outputOriginX = tileXs[tileIndex];
            params.outputOriginY = tileYs[tileIndex];

            for (NSUInteger slot = 0; slot < kMaxCameras; ++slot) {
                id<MTLBuffer> sourceBuffer = dummy;
                id<MTLBuffer> x = dummy;
                id<MTLBuffer> y = dummy;
                id<MTLBuffer> weight = dummy;
                if (slot < tile.cameraIndices.count) {
                    uint32_t camera = tile.cameraIndices[slot].unsignedIntValue;
                    sourceBuffer = arrayValue(context.sources, camera);
                    x = tile.mapXBuffers[slot];
                    y = tile.mapYBuffers[slot];
                    weight = tile.weightBuffers[slot];
                    params.sourceWidths[slot] = tile.sourceWidths[slot].unsignedIntValue;
                    params.sourceHeights[slot] = tile.sourceHeights[slot].unsignedIntValue;
                }
                if (sourceBuffer == nil) {
                    setError(context, @"Metal source disappeared before frame render");
                    failed = true;
                    break;
                }
                [encoder setBuffer:sourceBuffer offset:0 atIndex:2 + slot];
                [encoder setBuffer:x offset:0 atIndex:7 + slot];
                [encoder setBuffer:y offset:0 atIndex:12 + slot];
                [encoder setBuffer:weight offset:0 atIndex:17 + slot];
            }
            if (failed) break;
            [encoder setBytes:&params length:sizeof(params) atIndex:1];
            NSUInteger threadWidth = context.pipeline.threadExecutionWidth;
            NSUInteger threadHeight = std::max<NSUInteger>(
                1,
                std::min<NSUInteger>(
                    16,
                    context.pipeline.maxTotalThreadsPerThreadgroup / threadWidth
                )
            );
            [encoder dispatchThreads:MTLSizeMake(tile.width, tile.height, 1)
               threadsPerThreadgroup:MTLSizeMake(threadWidth, threadHeight, 1)];
        }
        [encoder endEncoding];
        if (failed) return 0;

        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        if (commandBuffer.status == MTLCommandBufferStatusError) {
            setError(context, [NSString stringWithFormat:@"Metal frame execution failed: %@",
                                                           commandBuffer.error.localizedDescription]);
            return 0;
        }
        if (output != context.frameOutput.contents) {
            std::memcpy(output, context.frameOutput.contents, outputLength);
        }
        return 1;
    }
}

extern "C" int vpstitch_metal_render_encode_prepared_frame(
    void *opaque,
    void *encoderOpaque,
    const uint32_t *tileIDs,
    const uint32_t *tileXs,
    const uint32_t *tileYs,
    uint32_t tileCount,
    uint32_t outputWidth,
    uint32_t outputHeight,
    uint32_t frameIndex,
    uint32_t dither,
    uint32_t seed
) {
    @autoreleasepool {
        if (opaque == nullptr || encoderOpaque == nullptr || tileIDs == nullptr ||
            tileXs == nullptr || tileYs == nullptr || tileCount == 0) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        VPStitchMetalVideoEncoder *encoder =
            (__bridge VPStitchMetalVideoEncoder *)encoderOpaque;
        if (!encoder.ready || encoder.finished || encoder.width != outputWidth ||
            encoder.height != outputHeight || encoder.nextFrame != frameIndex) {
            setEncoderError(encoder, @"native ProRes frame state does not match the render");
            return 0;
        }
        const NSUInteger pixels = static_cast<NSUInteger>(outputWidth) * outputHeight;
        const NSUInteger outputLength = pixels * 3 * sizeof(uint16_t);
        if (outputLength > context.device.maxBufferLength) {
            setEncoderError(encoder, @"native ProRes Metal frame exceeds the device buffer limit");
            return 0;
        }
        if (context.frameOutput == nil || context.frameOutputLength != outputLength) {
            context.frameOutput = [context.device newBufferWithLength:outputLength
                                                               options:MTLResourceStorageModeShared];
            context.frameOutputLength = outputLength;
        }
        if (context.frameOutput == nil) {
            setEncoderError(encoder, @"native ProRes Metal frame allocation failed");
            return 0;
        }
        if (!vpstitch_metal_render_prepared_frame(
                opaque,
                tileIDs,
                tileXs,
                tileYs,
                tileCount,
                outputWidth,
                outputHeight,
                frameIndex,
                dither,
                seed,
                static_cast<uint16_t *>(context.frameOutput.contents))) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes stitch failed: %@", context.lastError]
            );
            return 0;
        }
        if (!waitUntilWriterReady(encoder)) return 0;

        CVPixelBufferRef pixelBuffer = nullptr;
        CVReturn poolStatus = CVPixelBufferPoolCreatePixelBuffer(
            kCFAllocatorDefault, encoder.adaptor.pixelBufferPool, &pixelBuffer
        );
        if (poolStatus != kCVReturnSuccess || pixelBuffer == nullptr) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes pixel buffer allocation failed (%d)",
                    poolStatus]
            );
            return 0;
        }
        CVBufferSetAttachment(
            pixelBuffer,
            kCVImageBufferColorPrimariesKey,
            encoder.colorPrimariesAttachment,
            kCVAttachmentMode_ShouldPropagate
        );
        CVBufferSetAttachment(
            pixelBuffer,
            kCVImageBufferTransferFunctionKey,
            encoder.transferFunctionAttachment,
            kCVAttachmentMode_ShouldPropagate
        );
        CVBufferSetAttachment(
            pixelBuffer,
            kCVImageBufferYCbCrMatrixKey,
            encoder.yCbCrMatrixAttachment,
            kCVAttachmentMode_ShouldPropagate
        );

        IOSurfaceRef surface = CVPixelBufferGetIOSurface(pixelBuffer);
        if (surface == nullptr) {
            CVPixelBufferRelease(pixelBuffer);
            setEncoderError(encoder, @"native ProRes pixel buffer has no IOSurface");
            return 0;
        }
        kern_return_t surfaceLock = IOSurfaceLock(surface, 0, nullptr);
        if (surfaceLock != KERN_SUCCESS) {
            CVPixelBufferRelease(pixelBuffer);
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes IOSurface lock failed (%d)",
                    surfaceLock]
            );
            return 0;
        }
        char *surfaceBase = static_cast<char *>(IOSurfaceGetBaseAddress(surface));
        char *lumaBase = static_cast<char *>(IOSurfaceGetBaseAddressOfPlane(surface, 0));
        char *chromaBase = static_cast<char *>(IOSurfaceGetBaseAddressOfPlane(surface, 1));
        if (surfaceBase == nullptr || lumaBase == nullptr || chromaBase == nullptr) {
            IOSurfaceUnlock(surface, 0, nullptr);
            CVPixelBufferRelease(pixelBuffer);
            setEncoderError(encoder, @"native ProRes IOSurface planes are unavailable");
            return 0;
        }
        const size_t surfaceLength = IOSurfaceGetAllocSize(surface);
        const size_t lumaOffset = static_cast<size_t>(lumaBase - surfaceBase);
        const size_t chromaOffset = static_cast<size_t>(chromaBase - surfaceBase);
        const size_t lumaStride = IOSurfaceGetBytesPerRowOfPlane(surface, 0);
        const size_t chromaStride = IOSurfaceGetBytesPerRowOfPlane(surface, 1);
        const size_t encodedRowBytes = static_cast<size_t>(outputWidth) * sizeof(uint16_t);
        if ((lumaOffset | chromaOffset | lumaStride | chromaStride) % sizeof(uint16_t) != 0 ||
            std::max({lumaOffset, chromaOffset, lumaStride, chromaStride}) /
                sizeof(uint16_t) > UINT32_MAX ||
            !surfacePlaneFits(
                surfaceLength, lumaOffset, lumaStride, encodedRowBytes, outputHeight
            ) ||
            !surfacePlaneFits(
                surfaceLength, chromaOffset, chromaStride, encodedRowBytes, outputHeight
            )) {
            IOSurfaceUnlock(surface, 0, nullptr);
            CVPixelBufferRelease(pixelBuffer);
            setEncoderError(encoder, @"native ProRes IOSurface layout is unsupported");
            return 0;
        }
        id<MTLBuffer> surfaceBuffer = [context.device
            newBufferWithBytesNoCopy:surfaceBase
            length:surfaceLength
            options:MTLResourceStorageModeShared
            deallocator:nil];
        if (surfaceBuffer == nil) {
            IOSurfaceUnlock(surface, 0, nullptr);
            CVPixelBufferRelease(pixelBuffer);
            setEncoderError(encoder, @"native ProRes IOSurface Metal mapping failed");
            return 0;
        }
        EncodeParams params = {
            outputWidth,
            outputHeight,
            frameIndex,
            dither,
            seed,
            encoder.matrix2020 ? 1u : 0u,
            static_cast<uint32_t>(lumaOffset / sizeof(uint16_t)),
            static_cast<uint32_t>(chromaOffset / sizeof(uint16_t)),
            static_cast<uint32_t>(lumaStride / sizeof(uint16_t)),
            static_cast<uint32_t>(chromaStride / sizeof(uint16_t)),
        };
        id<MTLCommandBuffer> commandBuffer = [context.queue commandBuffer];
        id<MTLComputeCommandEncoder> compute = [commandBuffer computeCommandEncoder];
        [compute setComputePipelineState:encoder.conversionPipeline];
        [compute setBuffer:context.frameOutput offset:0 atIndex:0];
        [compute setBuffer:surfaceBuffer offset:0 atIndex:1];
        [compute setBytes:&params length:sizeof(params) atIndex:2];
        NSUInteger threadWidth = encoder.conversionPipeline.threadExecutionWidth;
        NSUInteger threadHeight = std::max<NSUInteger>(
            1,
            std::min<NSUInteger>(
                16,
                encoder.conversionPipeline.maxTotalThreadsPerThreadgroup / threadWidth
            )
        );
        [compute dispatchThreads:MTLSizeMake((outputWidth + 1) / 2, outputHeight, 1)
            threadsPerThreadgroup:MTLSizeMake(threadWidth, threadHeight, 1)];
        [compute endEncoding];
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        IOSurfaceUnlock(surface, 0, nullptr);
        if (commandBuffer.status == MTLCommandBufferStatusError) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes RGB-to-YUV conversion failed: %@",
                    commandBuffer.error.localizedDescription]
            );
            CVPixelBufferRelease(pixelBuffer);
            return 0;
        }

        CMTime presentationTime = CMTimeMake(
            static_cast<int64_t>(frameIndex) * encoder.fpsDenominator,
            encoder.fpsNumerator
        );
        if (encoder.formatDescription == nullptr) {
            CMVideoFormatDescriptionRef formatDescription = nullptr;
            OSStatus formatStatus = CMVideoFormatDescriptionCreateForImageBuffer(
                kCFAllocatorDefault, pixelBuffer, &formatDescription
            );
            encoder.formatDescription = formatDescription;
            if (formatStatus != noErr || encoder.formatDescription == nullptr) {
                CVPixelBufferRelease(pixelBuffer);
                setEncoderError(
                    encoder,
                    [NSString stringWithFormat:@"native ProRes format description failed (%d)",
                        formatStatus]
                );
                return 0;
            }
        }
        CMSampleTimingInfo timing = {
            CMTimeMake(encoder.fpsDenominator, encoder.fpsNumerator),
            presentationTime,
            kCMTimeInvalid,
        };
        CMSampleBufferRef sampleBuffer = nullptr;
        OSStatus sampleStatus = CMSampleBufferCreateReadyWithImageBuffer(
            kCFAllocatorDefault,
            pixelBuffer,
            encoder.formatDescription,
            &timing,
            &sampleBuffer
        );
        BOOL appended = sampleStatus == noErr && sampleBuffer != nullptr
            ? [encoder.input appendSampleBuffer:sampleBuffer]
            : NO;
        if (sampleBuffer != nullptr) CFRelease(sampleBuffer);
        CVPixelBufferRelease(pixelBuffer);
        if (!appended) {
            setEncoderError(
                encoder,
                [NSString stringWithFormat:@"native ProRes append failed (%d): %@",
                    sampleStatus, encoder.writer.error.localizedDescription]
            );
            return 0;
        }
        encoder.nextFrame += 1;
        return 1;
    }
}

extern "C" int vpstitch_metal_render_tile(
    void *opaque,
    const uint32_t *cameraIndices,
    const float *const *mapX,
    const float *const *mapY,
    const float *const *weights,
    const uint32_t *sourceWidths,
    const uint32_t *sourceHeights,
    uint32_t activeCount,
    uint32_t width,
    uint32_t height,
    uint32_t tileX,
    uint32_t tileY,
    uint32_t frameIndex,
    uint32_t dither,
    uint32_t seed,
    uint16_t *output
) {
    @autoreleasepool {
        if (opaque == nullptr || cameraIndices == nullptr || mapX == nullptr ||
            mapY == nullptr || weights == nullptr || sourceWidths == nullptr ||
            sourceHeights == nullptr || output == nullptr || activeCount == 0 ||
            activeCount > kMaxCameras || width == 0 || height == 0) {
            return 0;
        }
        VPStitchMetalContext *context = (__bridge VPStitchMetalContext *)opaque;
        if (context.pipeline == nil || context.queue == nil) {
            setError(context, @"Metal pipeline is not ready");
            return 0;
        }
        const NSUInteger pixels = static_cast<NSUInteger>(width) * height;
        id<MTLBuffer> outputBuffer = [context.device newBufferWithLength:pixels * 3 * sizeof(uint16_t)
                                                                  options:MTLResourceStorageModeShared];
        if (outputBuffer == nil) {
            setError(context, @"Metal output allocation failed");
            return 0;
        }
        RenderParams params = {};
        params.width = width;
        params.height = height;
        params.activeCount = activeCount;
        params.frameIndex = frameIndex;
        params.tileX = tileX;
        params.tileY = tileY;
        params.dither = dither;
        params.seed = seed;
        params.outputWidth = width;

        id<MTLBuffer> sourceBuffers[kMaxCameras] = {};
        id<MTLBuffer> mapXBuffers[kMaxCameras] = {};
        id<MTLBuffer> mapYBuffers[kMaxCameras] = {};
        id<MTLBuffer> weightBuffers[kMaxCameras] = {};
        id<MTLBuffer> dummy = [context.device newBufferWithLength:sizeof(float) * 3
                                                          options:MTLResourceStorageModeShared];
        for (uint32_t slot = 0; slot < kMaxCameras; ++slot) {
            sourceBuffers[slot] = dummy;
            mapXBuffers[slot] = dummy;
            mapYBuffers[slot] = dummy;
            weightBuffers[slot] = dummy;
        }
        for (uint32_t slot = 0; slot < activeCount; ++slot) {
            uint32_t camera = cameraIndices[slot];
            if (camera >= kMaxCameras) {
                setError(context, @"Metal camera index is out of range");
                return 0;
            }
            id<MTLBuffer> sourceBuffer = arrayValue(context.sources, camera);
            if (sourceBuffer == nil) {
                setError(context, @"Metal source was not uploaded");
                return 0;
            }
            sourceBuffers[slot] = sourceBuffer;
            params.sourceWidths[slot] = sourceWidths[slot];
            params.sourceHeights[slot] = sourceHeights[slot];
            mapXBuffers[slot] = bufferWithBytes(context, mapX[slot], pixels * sizeof(float));
            mapYBuffers[slot] = bufferWithBytes(context, mapY[slot], pixels * sizeof(float));
            weightBuffers[slot] = bufferWithBytes(context, weights[slot], pixels * sizeof(float));
            if (mapXBuffers[slot] == nil || mapYBuffers[slot] == nil || weightBuffers[slot] == nil) {
                setError(context, @"Metal map upload failed");
                return 0;
            }
        }

        id<MTLCommandBuffer> commandBuffer = [context.queue commandBuffer];
        id<MTLComputeCommandEncoder> encoder = [commandBuffer computeCommandEncoder];
        [encoder setComputePipelineState:context.pipeline];
        [encoder setBuffer:outputBuffer offset:0 atIndex:0];
        [encoder setBytes:&params length:sizeof(params) atIndex:1];
        for (NSUInteger slot = 0; slot < kMaxCameras; ++slot) {
            [encoder setBuffer:sourceBuffers[slot] offset:0 atIndex:2 + slot];
            [encoder setBuffer:mapXBuffers[slot] offset:0 atIndex:7 + slot];
            [encoder setBuffer:mapYBuffers[slot] offset:0 atIndex:12 + slot];
            [encoder setBuffer:weightBuffers[slot] offset:0 atIndex:17 + slot];
        }
        for (NSUInteger binding = 0; binding < kMaxTextures; ++binding) {
            id<MTLTexture> texture = arrayValue(context.textures, binding);
            id<MTLSamplerState> sampler = arrayValue(context.samplers, binding);
            if (texture != nil) {
                [encoder setTexture:texture atIndex:binding];
            }
            if (sampler != nil) {
                [encoder setSamplerState:sampler atIndex:binding];
            }
        }
        NSUInteger threadWidth = context.pipeline.threadExecutionWidth;
        NSUInteger threadHeight = std::max<NSUInteger>(
            1,
            std::min<NSUInteger>(16, context.pipeline.maxTotalThreadsPerThreadgroup / threadWidth)
        );
        [encoder dispatchThreads:MTLSizeMake(width, height, 1)
           threadsPerThreadgroup:MTLSizeMake(threadWidth, threadHeight, 1)];
        [encoder endEncoding];
        [commandBuffer commit];
        [commandBuffer waitUntilCompleted];
        if (commandBuffer.status == MTLCommandBufferStatusError) {
            setError(context, [NSString stringWithFormat:@"Metal execution failed: %@",
                                                           commandBuffer.error.localizedDescription]);
            return 0;
        }
        std::memcpy(output, outputBuffer.contents, pixels * 3 * sizeof(uint16_t));
        return 1;
    }
}
