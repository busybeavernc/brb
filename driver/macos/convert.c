/* BRB: convert BGR frames (OpenCV order) into the pixel formats a macOS camera extension asks for.
   Plain C, no Apple frameworks, so it's unit-tested on any machine (tests/test_macobs_convert.py).
   Video-range BT.601 for the YUV formats: Y 16..235, U/V 16..240. */
#include <stdint.h>

static inline uint8_t clamp8(int v) { return (uint8_t)(v < 0 ? 0 : v > 255 ? 255 : v); }

static inline void yuv601(int b, int g, int r, int *y, int *u, int *v) {
    *y = ((66 * r + 129 * g + 25 * b + 128) >> 8) + 16;
    *u = ((-38 * r - 74 * g + 112 * b + 128) >> 8) + 128;
    *v = ((112 * r - 94 * g - 18 * b + 128) >> 8) + 128;
}

/* '2vuy' (kCVPixelFormatType_422YpCbCr8): U0 Y0 V0 Y1 per pixel pair. width must be even. */
void brb_bgr_to_uyvy(const uint8_t *src, int src_stride, uint8_t *dst, int dst_stride, int width, int height) {
    for (int row = 0; row < height; row++) {
        const uint8_t *s = src + (long)row * src_stride;
        uint8_t *d = dst + (long)row * dst_stride;
        for (int x = 0; x + 1 < width; x += 2, s += 6, d += 4) {
            int y0, u0, v0, y1, u1, v1;
            yuv601(s[0], s[1], s[2], &y0, &u0, &v0);
            yuv601(s[3], s[4], s[5], &y1, &u1, &v1);
            d[0] = clamp8((u0 + u1 + 1) >> 1);
            d[1] = clamp8(y0);
            d[2] = clamp8((v0 + v1 + 1) >> 1);
            d[3] = clamp8(y1);
        }
    }
}

/* 'yuvs' (kCVPixelFormatType_422YpCbCr8_yuvs, YUY2): Y0 U0 Y1 V0 per pixel pair. */
void brb_bgr_to_yuy2(const uint8_t *src, int src_stride, uint8_t *dst, int dst_stride, int width, int height) {
    for (int row = 0; row < height; row++) {
        const uint8_t *s = src + (long)row * src_stride;
        uint8_t *d = dst + (long)row * dst_stride;
        for (int x = 0; x + 1 < width; x += 2, s += 6, d += 4) {
            int y0, u0, v0, y1, u1, v1;
            yuv601(s[0], s[1], s[2], &y0, &u0, &v0);
            yuv601(s[3], s[4], s[5], &y1, &u1, &v1);
            d[0] = clamp8(y0);
            d[1] = clamp8((u0 + u1 + 1) >> 1);
            d[2] = clamp8(y1);
            d[3] = clamp8((v0 + v1 + 1) >> 1);
        }
    }
}

/* 'BGRA' (kCVPixelFormatType_32BGRA). */
void brb_bgr_to_bgra(const uint8_t *src, int src_stride, uint8_t *dst, int dst_stride, int width, int height) {
    for (int row = 0; row < height; row++) {
        const uint8_t *s = src + (long)row * src_stride;
        uint8_t *d = dst + (long)row * dst_stride;
        for (int x = 0; x < width; x++, s += 3, d += 4) {
            d[0] = s[0]; d[1] = s[1]; d[2] = s[2]; d[3] = 255;
        }
    }
}

/* '420v' (kCVPixelFormatType_420YpCbCr8BiPlanarVideoRange, NV12): a Y plane, then interleaved U V at half size.
   width and height must be even. */
void brb_bgr_to_nv12(const uint8_t *src, int src_stride, uint8_t *y_plane, int y_stride,
                     uint8_t *uv_plane, int uv_stride, int width, int height) {
    for (int row = 0; row + 1 < height; row += 2) {
        const uint8_t *s0 = src + (long)row * src_stride, *s1 = s0 + src_stride;
        uint8_t *y0p = y_plane + (long)row * y_stride, *y1p = y0p + y_stride;
        uint8_t *uv = uv_plane + (long)(row / 2) * uv_stride;
        for (int x = 0; x + 1 < width; x += 2) {
            int ys[4], us = 0, vs = 0;
            const uint8_t *px[4] = {s0 + 3 * x, s0 + 3 * (x + 1), s1 + 3 * x, s1 + 3 * (x + 1)};
            for (int k = 0; k < 4; k++) {
                int y, u, v;
                yuv601(px[k][0], px[k][1], px[k][2], &y, &u, &v);
                ys[k] = y; us += u; vs += v;
            }
            y0p[x] = clamp8(ys[0]); y0p[x + 1] = clamp8(ys[1]);
            y1p[x] = clamp8(ys[2]); y1p[x + 1] = clamp8(ys[3]);
            uv[x] = clamp8((us + 2) >> 2); uv[x + 1] = clamp8((vs + 2) >> 2);
        }
    }
}
