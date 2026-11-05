"""doppleganger.macobs: the Python side of BRB's OBS helper, with a stand-in for the native library."""
import numpy as np
import pytest

from doppleganger import macobs


class FakeLib:
    def __init__(self, rc=0, w=1920, h=1080, fourcc=0x32767579):   # '2vuy'
        self.rc, self.w, self.h, self.fourcc, self.sent, self.closed = rc, w, h, fourcc, [], False

    def brb_obs_open(self, name, w, h, fourcc, err, errlen):
        if self.rc:
            err.value = b"not found"
            return self.rc
        w._obj.value, h._obj.value, fourcc._obj.value = self.w, self.h, self.fourcc
        return 0

    def brb_obs_send(self, data, stride, w, h):
        self.sent.append((stride, w, h))
        return 0

    def brb_obs_close(self):
        self.closed = True


def test_sends_at_the_size_obs_asks_for():
    lib = FakeLib()
    cam = macobs.ObsCamera(1280, 720, 30, lib=lib)
    assert (cam.device, cam.width, cam.height, cam.pixel_format) == ("OBS Virtual Camera", 1920, 1080, "2vuy")
    cam.send(np.zeros((720, 1280, 3), np.uint8))            # BRB's 720p picture, resized for OBS's 1080p camera
    assert lib.sent == [(1920 * 3, 1920, 1080)]
    cam.close()
    assert lib.closed


def test_explains_a_missing_obs_camera():
    with pytest.raises(RuntimeError, match="OBS Studio 30 or newer"):
        macobs.ObsCamera(1280, 720, 30, lib=FakeLib(rc=1))
