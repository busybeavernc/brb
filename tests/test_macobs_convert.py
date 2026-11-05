"""The Mac OBS helper's colour conversion (driver/macos/convert.c), compiled and checked here on any machine:
OpenCV's own decoders turn each format back into BGR, so the byte layout and the colours are both verified."""
import ctypes
import os
import shutil
import subprocess

import cv2
import numpy as np
import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CC = shutil.which("cc") or shutil.which("gcc") or shutil.which("clang")


@pytest.fixture(scope="module")
def conv(tmp_path_factory):
    if not CC:
        pytest.skip("no C compiler")
    so = str(tmp_path_factory.mktemp("conv") / "conv.so")
    subprocess.run([CC, "-shared", "-fPIC", "-O2", "-o", so, os.path.join(ROOT, "driver", "macos", "convert.c")],
                   check=True)
    return ctypes.CDLL(so)


def picture(w=64, h=48):
    rng = np.random.default_rng(1)
    img = cv2.GaussianBlur(rng.integers(0, 256, (h, w, 3), dtype=np.uint8), (0, 0), 2)   # smooth, like video
    img[:8, :16] = (255, 0, 0)       # pure blue, green, red, white, black patches
    img[:8, 16:32] = (0, 255, 0)
    img[:8, 32:48] = (0, 0, 255)
    img[8:16, :16] = 255
    img[8:16, 16:32] = 0
    return np.ascontiguousarray(img)


def ptr(a):
    return ctypes.c_void_p(a.ctypes.data)


def close(a, b, tol):
    return np.abs(a.astype(int) - b.astype(int)).mean() < tol


def test_uyvy(conv):
    src = picture()
    h, w = src.shape[:2]
    out = np.zeros((h, w * 2), np.uint8)
    conv.brb_bgr_to_uyvy(ptr(src), w * 3, ptr(out), w * 2, w, h)
    back = cv2.cvtColor(out.reshape(h, w, 2), cv2.COLOR_YUV2BGR_UYVY)
    assert close(back, src, 6)
    assert close(back[:8, 32:48], src[:8, 32:48], 6)   # red stays red (U and V not swapped)


def test_yuy2(conv):
    src = picture()
    h, w = src.shape[:2]
    out = np.zeros((h, w * 2), np.uint8)
    conv.brb_bgr_to_yuy2(ptr(src), w * 3, ptr(out), w * 2, w, h)
    assert close(cv2.cvtColor(out.reshape(h, w, 2), cv2.COLOR_YUV2BGR_YUY2), src, 6)


def test_nv12(conv):
    src = picture()
    h, w = src.shape[:2]
    y = np.zeros((h, w), np.uint8)
    uv = np.zeros((h // 2, w), np.uint8)
    conv.brb_bgr_to_nv12(ptr(src), w * 3, ptr(y), w, ptr(uv), w, w, h)
    back = cv2.cvtColor(np.vstack([y, uv]), cv2.COLOR_YUV2BGR_NV12)
    assert close(back, src, 8)
    assert close(back[:8, :16], src[:8, :16], 8)       # blue stays blue


def test_bgra_with_padded_rows(conv):
    src = picture()
    h, w = src.shape[:2]
    stride = w * 4 + 64                                  # CoreVideo rows are often padded
    out = np.zeros((h, stride), np.uint8)
    conv.brb_bgr_to_bgra(ptr(src), w * 3, ptr(out), stride, w, h)
    bgra = out[:, :w * 4].reshape(h, w, 4)
    assert np.array_equal(bgra[..., :3], src) and (bgra[..., 3] == 255).all()
    assert not out[:, w * 4:].any()                      # padding untouched
