#import <Foundation/Foundation.h>
#import <Metal/Metal.h>

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
@property(nonatomic, copy) NSString *lastError;
@end

@implementation VPStitchMetalContext
@end

static void setError(VPStitchMetalContext *context, NSString *message) {
    if (context != nil) {
        context.lastError = message ?: @"unknown Metal error";
    }
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
