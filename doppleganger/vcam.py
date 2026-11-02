"""The "Doppleganger" camera (Windows) and the list of real cameras.

- The camera itself is a DirectShow filter DLL (built from softcam on CI, see driver/).
  It's installed once into %ProgramData%\\Doppleganger and registered with regsvr32,
  which needs administrator rights (one Windows permission prompt).
- The same DLL exports the sender API (scCreateCamera / scSendFrame), which the app
  calls through ctypes to put frames on the camera.
"""
from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import tempfile
import time

import numpy as np

CAMERA_NAME = "HappyHacks Virtual Camera"   # what Zoom/Teams list (renamed from "Doppleganger" in driver 3)
CLSID = "{548A881E-7657-4231-8FEA-A9877D9743C9}"
DRIVER_VERSION = "3"  # bump when the driver DLL changes, so installed copies get refreshed
# 2: neutral placeholder when the app is not running (fixes a Zoom crash); 3: renamed "HappyHacks Virtual Camera"
DLL64, DLL32 = "doppleganger_cam64.dll", "doppleganger_cam32.dll"
VIRTUAL_CAMERA_HINTS = ("doppleganger", "happyhacks", "obs virtual", "virtual cam", "unity video capture",
                        "softcam", "xsplit", "snap camera", "manycam")


def is_windows() -> bool:
    return os.name == "nt"


def bundle_dir() -> str:
    """Where the packaged driver files are (inside the .exe, or driver_build/ when run from source)."""
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    for sub in ("driver", "driver_build"):
        d = os.path.join(base, sub)
        if os.path.exists(os.path.join(d, DLL64)):
            return d
    return os.path.join(base, "driver")


def install_dir() -> str:
    return os.path.join(os.environ.get("ProgramData", r"C:\ProgramData"), "Doppleganger")


def bundled() -> bool:
    return is_windows() and os.path.exists(os.path.join(bundle_dir(), DLL64))


def installed_version() -> str | None:
    """Driver version registered on this PC, or None if the camera isn't installed."""
    if not is_windows():
        return None
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CLASSES_ROOT, rf"CLSID\{CLSID}\InprocServer32") as k:
            path = winreg.QueryValue(k, None)
    except OSError:
        return None
    if not os.path.exists(path):
        return None
    try:
        with open(os.path.join(install_dir(), "version.txt"), encoding="utf-8", errors="ignore") as f:
            v = f.read().strip()
        return v if v.isdigit() else "0"  # older builds wrote a broken stamp: treat as outdated
    except OSError:
        return "0"


def needs_install() -> bool:
    return bundled() and installed_version() != DRIVER_VERSION


# ---------- install / remove (one elevated step each) ----------

def elevated_script(body: str, marker: str) -> str:
    """Batch text that runs `body` as a subroutine and ALWAYS records its exit code in `marker`
    (the body may `exit /b` early - that only leaves the subroutine, not the script)."""
    return ("@echo off\r\n"
            "call :main\r\n"
            f'(echo %ERRORLEVEL%)> "{marker}"\r\n'
            "exit /b\r\n"
            ":main\r\n" + body + "\r\nexit /b 0\r\n")


def _run_elevated_script(body: str, timeout: float = 120.0) -> tuple[bool, str]:
    """Run a batch script as administrator (Windows shows its permission prompt) and wait for it."""
    tmp = tempfile.mkdtemp(prefix="doppleganger_")
    script, marker = os.path.join(tmp, "run.bat"), os.path.join(tmp, "result.txt")
    with open(script, "w", encoding="utf-8") as f:
        f.write(elevated_script(body, marker))
    rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", "cmd.exe", f'/c "{script}"', None, 0)
    if rc <= 32:
        return False, "Permission was not granted."
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if os.path.exists(marker):
            time.sleep(0.2)
            with open(marker, encoding="utf-8", errors="ignore") as f:
                code = f.read().strip()
            return code == "0", f"exit code {code}"
        time.sleep(0.25)
    return False, "Timed out waiting for the installer."


def install() -> tuple[bool, str]:
    """Copy the camera DLLs to ProgramData and register them. Safe to re-run (upgrades in place)."""
    src, dst = bundle_dir(), install_dir()
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    lines = [f'mkdir "{dst}" 2>nul', f'del /q "{dst}\\*.old" 2>nul']
    for dll in (DLL64, DLL32):
        if not os.path.exists(os.path.join(src, dll)):
            continue
        # A DLL that Zoom has open can't be overwritten, but it can be renamed out of the way.
        lines.append(f'if exist "{dst}\\{dll}" move /y "{dst}\\{dll}" "{dst}\\{dll}.{int(time.time())}.old" >nul')
        lines.append(f'copy /y "{src}\\{dll}" "{dst}\\{dll}" >nul || exit /b 1')
    lines.append(f'copy /y "{src}\\SOFTCAM-LICENSE.txt" "{dst}\\" >nul 2>nul')
    lines.append(f'"{sysroot}\\System32\\regsvr32.exe" /s "{dst}\\{DLL64}" || exit /b 2')
    if os.path.exists(os.path.join(src, DLL32)):
        lines.append(f'"{sysroot}\\SysWOW64\\regsvr32.exe" /s "{dst}\\{DLL32}"')
    lines.append(f'(echo {DRIVER_VERSION})> "{dst}\\version.txt"')
    lines.append("exit /b 0")
    return _run_elevated_script("\r\n".join(lines))


def uninstall() -> tuple[bool, str]:
    dst = install_dir()
    sysroot = os.environ.get("SystemRoot", r"C:\Windows")
    lines = [f'"{sysroot}\\System32\\regsvr32.exe" /s /u "{dst}\\{DLL64}"',
             f'if exist "{dst}\\{DLL32}" "{sysroot}\\SysWOW64\\regsvr32.exe" /s /u "{dst}\\{DLL32}"',
             f'del /q "{dst}\\*.dll" "{dst}\\*.old" "{dst}\\version.txt" 2>nul',
             "exit /b 0"]
    return _run_elevated_script("\r\n".join(lines))


# ---------- sending frames ----------

class DopplegangerCamera:
    """Sender side of the HappyHacks Virtual Camera (same API shape as pyvirtualcam.Camera)."""

    device = CAMERA_NAME

    def __init__(self, width: int, height: int, fps: float):
        if width % 4 or height % 4:
            raise ValueError("width and height must be multiples of 4")
        self._dll = ctypes.CDLL(os.path.join(install_dir(), DLL64))
        self._dll.scCreateCamera.restype = ctypes.c_void_p
        self._dll.scCreateCamera.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_float]
        self._dll.scSendFrame.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        self._dll.scDeleteCamera.argtypes = [ctypes.c_void_p]
        self._dll.scIsConnected.argtypes = [ctypes.c_void_p]
        self._dll.scIsConnected.restype = ctypes.c_bool
        # framerate 0 = deliver each frame immediately; the app paces itself.
        self._cam = self._dll.scCreateCamera(width, height, 0.0)
        if not self._cam:
            raise RuntimeError("the HappyHacks Virtual Camera is already in use by another copy of Doppleganger")
        self.width, self.height = width, height

    def send(self, frame: np.ndarray) -> None:
        if frame.shape != (self.height, self.width, 3):
            raise ValueError(f"frame must be {self.width}x{self.height} BGR")
        frame = np.ascontiguousarray(frame, dtype=np.uint8)
        self._dll.scSendFrame(self._cam, frame.ctypes.data)

    @property
    def viewer_connected(self) -> bool:
        return bool(self._cam) and bool(self._dll.scIsConnected(self._cam))

    def close(self) -> None:
        if self._cam:
            self._dll.scDeleteCamera(self._cam)
            self._cam = None


# ---------- real cameras ----------

def list_cameras() -> list[tuple[int, str]]:
    """(index, name) of video devices in the order OpenCV opens them. Empty if unknown."""
    if sys.platform == "darwin":
        from . import macos
        return macos.list_cameras()
    if not is_windows():
        return []
    exe = os.path.join(bundle_dir(), "listcams.exe")
    if not os.path.exists(exe):
        return []
    try:
        out = subprocess.run([exe], capture_output=True, timeout=10,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout
    except (OSError, subprocess.SubprocessError):
        return []
    names = [ln.strip() for ln in out.decode("utf-8", "replace").splitlines() if ln.strip()]
    return list(enumerate(names))


def is_virtual(name: str) -> bool:
    n = name.lower()
    return any(h in n for h in VIRTUAL_CAMERA_HINTS)


def real_cameras() -> list[tuple[int, str]]:
    """Physical cameras only - choosing a virtual one would feed the output back into itself."""
    return [(i, n) for i, n in list_cameras() if not is_virtual(n)]


def resolve_camera(name: str, fallback_index: int) -> tuple[int, str]:
    """Index for the remembered camera name; else the configured index if it's a real camera;
    else the first real camera; else the configured index as-is."""
    cams = list_cameras()
    real = [(i, n) for i, n in cams if not is_virtual(n)]
    for i, n in real:
        if name and n == name:
            return i, n
    for i, n in real:
        if i == fallback_index:
            return i, n
    if real:
        return real[0]
    return fallback_index, name or f"Camera {fallback_index}"
