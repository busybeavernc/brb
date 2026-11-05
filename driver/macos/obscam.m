/* BRB: send video to "OBS Virtual Camera" on macOS.

   OBS Studio 30+ installs a camera extension (macOS 13+) with two streams: the one meeting apps read from, and a
   "sink" stream that accepts frames. This library finds that device through CoreMediaIO, asks the sink which picture
   size and pixel format it wants, and enqueues one CMSampleBuffer per frame - the documented way to feed a camera
   extension. Built by CI into libbrbobscam.dylib and loaded by doppleganger/macobs.py with ctypes.

   C API (one camera at a time):
     int  brb_obs_open(const char *name, int *width, int *height, uint32_t *fourcc, char *err, int errlen)  0 = ok
     int  brb_obs_send(const uint8_t *bgr, int stride, int width, int height)   0 = sent, 1 = queue full, <0 = error
     void brb_obs_close(void)
*/
#import <Foundation/Foundation.h>
#import <CoreMediaIO/CMIOHardware.h>
#import <CoreMedia/CoreMedia.h>
#import <CoreVideo/CoreVideo.h>
#include <stdio.h>
#include <string.h>
#include "convert.c"

static CMIODeviceID g_dev = 0;
static CMIOStreamID g_sink = 0;
static CMSimpleQueueRef g_queue = NULL;
static CVPixelBufferPoolRef g_pool = NULL;
static CMVideoFormatDescriptionRef g_fmt = NULL;
static int32_t g_w = 0, g_h = 0;
static OSType g_pix = 0;
static int64_t g_frame = 0;

static CMIOObjectPropertyAddress addr(CMIOObjectPropertySelector sel) {
    CMIOObjectPropertyAddress a = {sel, kCMIOObjectPropertyScopeGlobal, 0 /* element main */};
    return a;
}

static void set_err(char *err, int errlen, const char *msg) {
    if (err && errlen > 0) { strncpy(err, msg, (size_t)errlen - 1); err[errlen - 1] = 0; }
}

static void queue_altered(CMIOStreamID stream, void *token, void *refcon) { (void)stream; (void)token; (void)refcon; }

/* Object IDs held by a property (devices of the system, streams of a device). Caller frees. */
static CMIOObjectID *ids_of(CMIOObjectID obj, CMIOObjectPropertySelector sel, UInt32 *count) {
    CMIOObjectPropertyAddress a = addr(sel);
    UInt32 size = 0, used = 0;
    *count = 0;
    if (CMIOObjectGetPropertyDataSize(obj, &a, 0, NULL, &size) != noErr || size == 0) return NULL;
    CMIOObjectID *ids = (CMIOObjectID *)malloc(size);
    if (CMIOObjectGetPropertyData(obj, &a, 0, NULL, size, &used, ids) != noErr) { free(ids); return NULL; }
    *count = used / sizeof(CMIOObjectID);
    return ids;
}

static int name_is(CMIOObjectID obj, const char *want) {
    CMIOObjectPropertyAddress a = addr(kCMIOObjectPropertyName);
    CFStringRef name = NULL;
    UInt32 used = 0;
    if (CMIOObjectGetPropertyData(obj, &a, 0, NULL, sizeof(name), &used, &name) != noErr || !name) return 0;
    char buf[256] = {0};
    CFStringGetCString(name, buf, sizeof(buf), kCFStringEncodingUTF8);
    CFRelease(name);
    return strcmp(buf, want) == 0;
}

void brb_obs_close(void) {
    if (g_dev && g_sink) CMIODeviceStopStream(g_dev, g_sink);
    if (g_queue) { CFRelease(g_queue); g_queue = NULL; }
    if (g_pool) { CVPixelBufferPoolRelease(g_pool); g_pool = NULL; }
    if (g_fmt) { CFRelease(g_fmt); g_fmt = NULL; }
    g_dev = g_sink = 0;
    g_w = g_h = 0;
    g_pix = 0;
}

int brb_obs_open(const char *name, int *width, int *height, uint32_t *fourcc, char *err, int errlen) {
    brb_obs_close();
    UInt32 n = 0;
    CMIOObjectID *devs = ids_of(kCMIOObjectSystemObject, kCMIOHardwarePropertyDevices, &n);
    for (UInt32 i = 0; i < n && !g_dev; i++)
        if (name_is(devs[i], name)) g_dev = devs[i];
    free(devs);
    if (!g_dev) { set_err(err, errlen, "not found"); return 1; }

    UInt32 ns = 0;
    CMIOStreamID *streams = ids_of(g_dev, kCMIODevicePropertyStreams, &ns);
    if (ns < 2) {   /* the source stream only: an older OBS (before 30) or a different kind of camera */
        free(streams);
        g_dev = 0;
        set_err(err, errlen, "no sink stream (OBS Studio 30 or newer is needed)");
        return 2;
    }
    g_sink = streams[ns - 1];   /* camera extensions list the source stream first, then the sink */
    free(streams);

    CMIOObjectPropertyAddress fa = addr(kCMIOStreamPropertyFormatDescription);
    UInt32 used = 0;
    if (CMIOObjectGetPropertyData(g_sink, &fa, 0, NULL, sizeof(g_fmt), &used, &g_fmt) != noErr || !g_fmt) {
        g_fmt = NULL;
        brb_obs_close();
        set_err(err, errlen, "could not read the camera's picture format");
        return 3;
    }
    CMVideoDimensions dim = CMVideoFormatDescriptionGetDimensions(g_fmt);
    g_w = dim.width; g_h = dim.height;
    g_pix = CMFormatDescriptionGetMediaSubType(g_fmt);
    if (g_pix != kCVPixelFormatType_422YpCbCr8 && g_pix != kCVPixelFormatType_422YpCbCr8_yuvs &&
        g_pix != kCVPixelFormatType_32BGRA && g_pix != kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
        char msg[96];
        snprintf(msg, sizeof(msg), "unsupported pixel format %c%c%c%c",
                 (char)(g_pix >> 24), (char)(g_pix >> 16), (char)(g_pix >> 8), (char)g_pix);
        brb_obs_close();
        set_err(err, errlen, msg);
        return 4;
    }

    NSDictionary *attrs = @{(id)kCVPixelBufferPixelFormatTypeKey: @(g_pix),
                            (id)kCVPixelBufferWidthKey: @(g_w), (id)kCVPixelBufferHeightKey: @(g_h),
                            (id)kCVPixelBufferIOSurfacePropertiesKey: @{}};   /* IOSurface: shared with the extension */
    if (CVPixelBufferPoolCreate(kCFAllocatorDefault, NULL, (__bridge CFDictionaryRef)attrs, &g_pool) != kCVReturnSuccess) {
        g_pool = NULL;
        brb_obs_close();
        set_err(err, errlen, "could not allocate picture buffers");
        return 5;
    }
    if (CMIOStreamCopyBufferQueue(g_sink, queue_altered, NULL, &g_queue) != noErr || !g_queue) {
        g_queue = NULL;
        brb_obs_close();
        set_err(err, errlen, "could not connect to the camera's input");
        return 6;
    }
    if (CMIODeviceStartStream(g_dev, g_sink) != noErr) {
        brb_obs_close();
        set_err(err, errlen, "could not start the camera's input");
        return 7;
    }
    *width = g_w; *height = g_h; *fourcc = (uint32_t)g_pix;
    g_frame = 0;
    return 0;
}

int brb_obs_send(const uint8_t *bgr, int stride, int width, int height) {
    if (!g_queue || !g_pool) return -1;
    if (width != g_w || height != g_h) return -2;
    if (CMSimpleQueueGetCount(g_queue) >= CMSimpleQueueGetCapacity(g_queue)) return 1;   /* reader is behind: skip */

    CVPixelBufferRef pb = NULL;
    if (CVPixelBufferPoolCreatePixelBuffer(kCFAllocatorDefault, g_pool, &pb) != kCVReturnSuccess || !pb) return -3;
    CVPixelBufferLockBaseAddress(pb, 0);
    if (g_pix == kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange) {
        brb_bgr_to_nv12(bgr, stride,
                        (uint8_t *)CVPixelBufferGetBaseAddressOfPlane(pb, 0), (int)CVPixelBufferGetBytesPerRowOfPlane(pb, 0),
                        (uint8_t *)CVPixelBufferGetBaseAddressOfPlane(pb, 1), (int)CVPixelBufferGetBytesPerRowOfPlane(pb, 1),
                        width, height);
    } else {
        uint8_t *dst = (uint8_t *)CVPixelBufferGetBaseAddress(pb);
        int dst_stride = (int)CVPixelBufferGetBytesPerRow(pb);
        if (g_pix == kCVPixelFormatType_422YpCbCr8) brb_bgr_to_uyvy(bgr, stride, dst, dst_stride, width, height);
        else if (g_pix == kCVPixelFormatType_422YpCbCr8_yuvs) brb_bgr_to_yuy2(bgr, stride, dst, dst_stride, width, height);
        else brb_bgr_to_bgra(bgr, stride, dst, dst_stride, width, height);
    }
    CVPixelBufferUnlockBaseAddress(pb, 0);

    CMVideoFormatDescriptionRef fd = NULL;
    if (CMVideoFormatDescriptionCreateForImageBuffer(kCFAllocatorDefault, pb, &fd) != noErr) { CVPixelBufferRelease(pb); return -4; }
    CMSampleTimingInfo timing = {CMTimeMake(1, 30), CMClockGetTime(CMClockGetHostTimeClock()), kCMTimeInvalid};
    CMSampleBufferRef sb = NULL;
    OSStatus st = CMSampleBufferCreateForImageBuffer(kCFAllocatorDefault, pb, true, NULL, NULL, fd, &timing, &sb);
    CFRelease(fd);
    CVPixelBufferRelease(pb);   /* the sample buffer holds it now */
    if (st != noErr || !sb) return -5;
    if (CMSimpleQueueEnqueue(g_queue, sb) != noErr) { CFRelease(sb); return 1; }   /* the extension releases it */
    g_frame++;
    return 0;
}
