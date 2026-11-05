"""Mac: send BRB's picture to "OBS Virtual Camera" through BRB's own helper library (driver/macos/obscam.m).

The library finds OBS's camera extension (OBS Studio 30+, macOS 13+), asks it which picture size and pixel format it
wants, and hands it one frame at a time. Same shape as the Windows camera: .device, .send(frame), .close().
"""
from __future__ import annotations

import ctypes
import os
import sys

import cv2
import numpy as np

LIB_NAME = "libbrbobscam.dylib"
CAMERA = "OBS Virtual Camera"
_lib = None


def _lib_path() -> str:
    base = getattr(sys, "_MEIPASS", None)
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for d in ([os.path.join(base, "driver"), base] if base else []) + [os.path.join(here, "driver_build")]:
        p = os.path.join(d, LIB_NAME)
        if os.path.exists(p):
            return p
    return LIB_NAME


def load():
    global _lib
    if _lib is None:
        lib = ctypes.CDLL(_lib_path())
        lib.brb_obs_open.argtypes = [ctypes.c_char_p, ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_int),
                                     ctypes.POINTER(ctypes.c_uint32), ctypes.c_char_p, ctypes.c_int]
        lib.brb_obs_open.restype = ctypes.c_int
        lib.brb_obs_send.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int, ctypes.c_int]
        lib.brb_obs_send.restype = ctypes.c_int
        lib.brb_obs_close.argtypes = []
        lib.brb_obs_close.restype = None
        _lib = lib
    return _lib


def fourcc_text(code: int) -> str:
    return "".join(chr((code >> s) & 0xFF) for s in (24, 16, 8, 0))


class ObsCamera:
    """Frames (BGR, any size) to OBS Virtual Camera, resized to the size OBS's camera asks for."""

    def __init__(self, width: int, height: int, fps: int, lib=None, name: str = CAMERA):
        self._lib = lib or load()
        w, h, fourcc = ctypes.c_int(0), ctypes.c_int(0), ctypes.c_uint32(0)
        err = ctypes.create_string_buffer(200)
        rc = self._lib.brb_obs_open(name.encode(), ctypes.byref(w), ctypes.byref(h), ctypes.byref(fourcc), err, 200)
        if rc != 0:
            why = err.value.decode(errors="replace") or f"error {rc}"
            raise RuntimeError(f"{name} not available ({why}). Install OBS Studio 30 or newer, start its virtual "
                               "camera once, then stop it and quit OBS.")
        self.device = name
        self.width, self.height = w.value, h.value
        self.pixel_format = fourcc_text(fourcc.value)
        self.viewer_connected = False   # OBS's extension doesn't say whether a meeting app is watching

    def send(self, frame: np.ndarray) -> None:
        if frame.shape[1] != self.width or frame.shape[0] != self.height:
            frame = cv2.resize(frame, (self.width, self.height), interpolation=cv2.INTER_AREA)
        frame = np.ascontiguousarray(frame, dtype=np.uint8)
        rc = self._lib.brb_obs_send(frame.ctypes.data, frame.strides[0], self.width, self.height)
        if rc < 0:
            raise RuntimeError(f"Sending to {self.device} failed ({rc})")
        # rc == 1: OBS's camera hasn't taken the previous frame yet - this one is skipped, the next one goes

    def close(self) -> None:
        try:
            self._lib.brb_obs_close()
        except Exception:  # noqa: BLE001
            pass
