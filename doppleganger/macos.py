"""macOS support. Everything here is a no-op (or returns "unknown") on other systems.

On a Mac, Doppleganger sends its picture through the **OBS Virtual Camera**:
OBS Studio 30+ ships a macOS camera extension that Zoom/Teams/Meet list as
"OBS Virtual Camera". OBS itself does not need to be running - it only has to be
installed once and its virtual camera started once, so macOS installs the
extension. This module checks for both and explains what's missing.

Also here: the webcam list (AVFoundation), muting the microphone while the loop
is on air (system input volume), hiding windows from screen sharing, starting
at login (LaunchAgent) and the global F9/F10 hotkeys (a listen-only Quartz event
tap - pynput's listener can crash on recent macOS when used next to Tk).
"""
from __future__ import annotations

import logging
import os
import plistlib
import subprocess
import sys
import threading
from typing import Callable

log = logging.getLogger("doppleganger")

OBS_DOWNLOAD_URL = "https://obsproject.com/download"
OBS_BUNDLE_ID = "com.obsproject.obs-studio"
OBS_EXTENSION_ID = "com.obsproject.obs-studio.mac-camera-extension"
OBS_LEGACY_PLUGIN = "/Library/CoreMediaIO/Plug-Ins/DAL/obs-mac-virtualcam.plugin"
LAUNCH_AGENT_ID = "com.busybeavernc.doppleganger"


def is_mac() -> bool:
    return sys.platform == "darwin"


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, timeout=timeout)
        return r.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return ""


# ---------- OBS Studio + its virtual camera ----------

def obs_app_path() -> str | None:
    """Where OBS.app is, or None if OBS Studio isn't installed."""
    for p in ("/Applications/OBS.app", os.path.expanduser("~/Applications/OBS.app")):
        if os.path.isdir(p):
            return p
    found = _run(["mdfind", f"kMDItemCFBundleIdentifier == '{OBS_BUNDLE_ID}'"]).strip().splitlines()
    return found[0] if found else None


def obs_camera_state(systemextensions_output: str | None = None) -> str:
    """'enabled'  - the OBS camera extension is installed and switched on,
    'waiting'  - installed but waiting for you to allow it in System Settings,
    'missing'  - not installed (start OBS's virtual camera once to install it),
    'unknown'  - couldn't tell (we then just try to connect)."""
    out = systemextensions_output
    if out is None:
        out = _run(["systemextensionsctl", "list"])
    if out:
        lines = [ln for ln in out.splitlines() if OBS_EXTENSION_ID in ln]
        if any("activated enabled" in ln for ln in lines):
            return "enabled"
        if lines:
            return "waiting"
        if os.path.isdir(OBS_LEGACY_PLUGIN):  # OBS 26-29 style camera (older macOS)
            return "enabled"
        return "missing"
    return "enabled" if os.path.isdir(OBS_LEGACY_PLUGIN) else "unknown"


def obs_status() -> dict:
    """Everything the setup screens need: {'obs': bool, 'camera': state, 'ready': bool, 'message': str}."""
    app = obs_app_path()
    cam = obs_camera_state()
    ready = cam in ("enabled", "unknown") and (app is not None or cam == "enabled")
    return {"obs": app is not None, "camera": cam, "ready": ready, "message": obs_message(app is not None, cam)}


def obs_message(obs_installed: bool, camera: str) -> str:
    if not obs_installed and camera != "enabled":
        return ("Doppleganger uses the OBS Virtual Camera on Mac. Install OBS Studio (free), "
                "then start its virtual camera once.")
    if camera == "missing":
        return ("OBS Studio is installed, but its virtual camera isn't yet. Open OBS, click "
                "'Start Virtual Camera', then 'Stop Virtual Camera', and quit OBS.")
    if camera == "waiting":
        return ("Almost there: allow the OBS camera in System Settings > General > Login Items & "
                "Extensions > Camera Extensions (you may need to restart your Mac).")
    return "OBS Virtual Camera is installed. Choose 'OBS Virtual Camera' in your meeting app's video settings."


def open_obs_download() -> None:
    open_url(OBS_DOWNLOAD_URL)


def open_url(url: str) -> None:
    import webbrowser
    webbrowser.open(url)


def open_obs() -> bool:
    app = obs_app_path()
    if not app:
        return False
    subprocess.Popen(["open", app])
    return True


def open_camera_extension_settings() -> None:
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.LoginItems-Settings.extension"])


# ---------- webcams ----------

AV_VIDEO, AV_MUXED = "vide", "muxx"  # AVMediaTypeVideo / AVMediaTypeMuxed


def _capture_device_class():
    """AVCaptureDevice straight from the system framework via the Objective-C runtime.
    (pyobjc's AVFoundation wrapper pulls in CoreMedia's Swift libraries, which don't load
    inside the packaged app.)"""
    import objc
    from Foundation import NSBundle
    NSBundle.bundleWithPath_("/System/Library/Frameworks/AVFoundation.framework").load()
    cls = objc.lookUpClass("AVCaptureDevice")
    objc.registerMetaDataForSelector(
        b"AVCaptureDevice", b"requestAccessForMediaType:completionHandler:",
        {"arguments": {3: {"callable": {"retval": {"type": b"v"},
                                        "arguments": {0: {"type": b"^v"}, 1: {"type": b"Z"}}}}}})
    return cls


def list_cameras() -> list[tuple[int, str]]:
    """(index, name) in the order OpenCV's AVFoundation backend opens them."""
    try:
        dev = _capture_device_class()
        devs = list(dev.devicesWithMediaType_(AV_VIDEO) or [])
        try:
            devs += list(dev.devicesWithMediaType_(AV_MUXED) or [])
        except Exception:  # noqa: BLE001
            pass
        devs.sort(key=lambda d: str(d.uniqueID()))  # OpenCV sorts by uniqueID too
        return [(i, str(d.localizedName())) for i, d in enumerate(devs)]
    except Exception as e:  # noqa: BLE001
        log.info("AVFoundation camera list unavailable (%s); using system_profiler", e)
    import json
    try:
        data = json.loads(_run(["system_profiler", "-json", "SPCameraDataType"], timeout=10) or "{}")
        names = [c.get("_name", "") for c in data.get("SPCameraDataType", [])]
        return [(i, n) for i, n in enumerate(names) if n]
    except ValueError:
        return []


def ensure_camera_access(timeout: float = 120.0) -> bool | None:
    """Ask for camera permission up front (main thread), so the first launch shows macOS's prompt
    instead of failing to open the webcam. True = allowed, False = denied, None = couldn't tell."""
    try:
        import Foundation
        dev = _capture_device_class()
    except Exception:  # noqa: BLE001
        return None
    media = AV_VIDEO
    status = dev.authorizationStatusForMediaType_(media)
    if status == 3:   # authorized
        return True
    if status in (1, 2):  # restricted / denied
        return False
    answer: dict = {}
    dev.requestAccessForMediaType_completionHandler_(
        media, lambda granted: answer.setdefault("granted", bool(granted)))
    import time
    end = time.monotonic() + timeout
    loop = Foundation.NSRunLoop.currentRunLoop()
    while "granted" not in answer and time.monotonic() < end:
        loop.runUntilDate_(Foundation.NSDate.dateWithTimeIntervalSinceNow_(0.1))
    return answer.get("granted")


def camera_permission() -> str:
    """'allowed' / 'denied' / 'not asked' / 'unknown' (doesn't prompt)."""
    try:
        status = int(_capture_device_class().authorizationStatusForMediaType_(AV_VIDEO))
    except Exception:  # noqa: BLE001
        return "unknown"
    return {3: "allowed", 2: "denied", 1: "denied", 0: "not asked"}.get(status, "unknown")


def relaunch() -> None:
    """Quit and reopen Doppleganger (macOS applies Input Monitoring only to a freshly started app)."""
    app = app_bundle()
    if app:
        subprocess.Popen(["/bin/sh", "-c", f"sleep 1.5; open -n '{app}'"], start_new_session=True)
    else:  # running from source
        subprocess.Popen([sys.executable, *sys.argv], start_new_session=True)


def open_camera_privacy_settings() -> None:
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_Camera"])


# ---------- microphone (silence while the loop is on air) ----------

def mic_runner(args: list[str]) -> str:
    """micmute.exe-compatible: 'mute' -> prints an id to restore; 'unmute <ids>' restores.
    On a Mac the id is the input volume it had ("volume:75"); muting sets it to 0."""
    if args[0] == "mute":
        vol = _run(["osascript", "-e", "input volume of (get volume settings)"]).strip()
        if not vol.isdigit() or int(vol) == 0:
            return ""  # already silent (or unknown): nothing to restore
        _run(["osascript", "-e", "set volume input volume 0"])
        return f"volume:{vol}\n"
    if args[0] == "unmute":
        for ident in args[1:]:
            if ident.startswith("volume:") and ident[7:].isdigit():
                _run(["osascript", "-e", f"set volume input volume {int(ident[7:])}"])
        return ""
    return ""


# ---------- windows hidden from screen sharing ----------

def hide_all_windows_from_capture(hide: bool = True) -> bool:
    """NSWindow.sharingType = none: the window isn't included in screen shares/recordings."""
    try:
        import AppKit
        for w in AppKit.NSApplication.sharedApplication().windows():
            w.setSharingType_(AppKit.NSWindowSharingNone if hide else AppKit.NSWindowSharingReadOnly)
        return True
    except Exception as e:  # noqa: BLE001
        log.info("Can't hide windows from screen sharing: %s", e)
        return False


# ---------- start at login ----------

def app_bundle() -> str | None:
    """/Applications/Doppleganger.app when running the packaged app."""
    if not getattr(sys, "frozen", False):
        return None
    p = os.path.abspath(sys.executable)
    while p and p != "/":
        if p.endswith(".app"):
            return p
        p = os.path.dirname(p)
    return None


def _agent_path() -> str:
    return os.path.expanduser(f"~/Library/LaunchAgents/{LAUNCH_AGENT_ID}.plist")


def set_start_at_login(enabled: bool) -> bool:
    app = app_bundle()
    if app is None:
        return False
    path = _agent_path()
    if enabled:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            plistlib.dump({"Label": LAUNCH_AGENT_ID,
                           "ProgramArguments": ["/usr/bin/open", "-a", app, "--args", "--minimized"],
                           "RunAtLoad": True}, f)
    else:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
    return True


def starts_at_login() -> bool:
    return os.path.exists(_agent_path())


# ---------- global hotkeys ----------

# macOS virtual key codes (Carbon kVK_*) for the keys Doppleganger's hotkeys use.
KEYCODES = {"f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
            "f9": 101, "f10": 109, "f11": 103, "f12": 111, "f13": 105, "f14": 107, "f15": 113,
            "space": 49, "esc": 53, "enter": 36, "tab": 48}
LETTERS = {"a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
           "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "o": 31, "u": 32, "i": 34, "p": 35,
           "l": 37, "j": 38, "k": 40, "n": 45, "m": 46}
# modifier flag masks (CGEventFlags)
MODIFIERS = {"shift": 0x20000, "ctrl": 0x40000, "alt": 0x80000, "cmd": 0x100000}


def parse_hotkey(spec: str) -> tuple[int, int]:
    """'<f9>' / '<ctrl>+<alt>+l' -> (keycode, required modifier mask)."""
    code, mods = None, 0
    for part in spec.lower().split("+"):
        k = part.strip().strip("<>")
        if k in MODIFIERS:
            mods |= MODIFIERS[k]
        elif k in KEYCODES:
            code = KEYCODES[k]
        elif k in LETTERS:
            code = LETTERS[k]
        else:
            raise ValueError(f"Unsupported hotkey on Mac: {spec}")
    if code is None:
        raise ValueError(f"Hotkey has no key: {spec}")
    return code, mods


class MacHotkeys:
    """Global hotkeys with key-down and key-up, via a listen-only Quartz event tap.
    Needs 'Input Monitoring' permission (macOS asks the first time)."""

    def __init__(self, bindings: dict[str, tuple[Callable[[], None], Callable[[], None] | None]]):
        self.bindings = {parse_hotkey(spec): cbs for spec, cbs in bindings.items()}
        self.down: set[int] = set()
        self._loop = None
        self._thread: threading.Thread | None = None
        self.ok = False

    def handle(self, is_down: bool, code: int, flags: int, autorepeat: bool = False) -> None:
        """Pure logic (tested on any OS)."""
        for (kc, mods), (on_press, on_release) in self.bindings.items():
            if kc != code:
                continue
            if is_down:
                if autorepeat or code in self.down or (flags & mods) != mods:
                    continue
                self.down.add(code)
                on_press()
            elif code in self.down:
                self.down.discard(code)
                if on_release:
                    on_release()

    def start(self) -> None:
        import Quartz  # pyobjc-framework-Quartz
        if has_input_monitoring() is False:
            # Without permission the tap may be created yet never see a key - say so instead.
            request_input_monitoring()
            raise RuntimeError("Input Monitoring isn't allowed for Doppleganger yet")

        def callback(_proxy, etype, event, _refcon):
            try:
                if etype in (Quartz.kCGEventKeyDown, Quartz.kCGEventKeyUp):
                    code = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventKeycode)
                    rep = Quartz.CGEventGetIntegerValueField(event, Quartz.kCGKeyboardEventAutorepeat)
                    self.handle(etype == Quartz.kCGEventKeyDown, int(code),
                                int(Quartz.CGEventGetFlags(event)), bool(rep))
                elif etype in (Quartz.kCGEventTapDisabledByTimeout, Quartz.kCGEventTapDisabledByUserInput):
                    Quartz.CGEventTapEnable(self._tap, True)
            except Exception:  # noqa: BLE001
                log.exception("hotkey")
            return event

        mask = Quartz.CGEventMaskBit(Quartz.kCGEventKeyDown) | Quartz.CGEventMaskBit(Quartz.kCGEventKeyUp)
        self._tap = Quartz.CGEventTapCreate(Quartz.kCGSessionEventTap, Quartz.kCGHeadInsertEventTap,
                                            Quartz.kCGEventTapOptionListenOnly, mask, callback, None)
        if not self._tap:
            raise RuntimeError("macOS didn't allow listening for hotkeys - allow Doppleganger in System "
                               "Settings > Privacy & Security > Input Monitoring, then reopen it")
        self.ok = True

        def run():
            src = Quartz.CFMachPortCreateRunLoopSource(None, self._tap, 0)
            self._loop = Quartz.CFRunLoopGetCurrent()
            Quartz.CFRunLoopAddSource(self._loop, src, Quartz.kCFRunLoopCommonModes)
            Quartz.CGEventTapEnable(self._tap, True)
            Quartz.CFRunLoopRun()

        self._thread = threading.Thread(target=run, daemon=True, name="hotkeys")
        self._thread.start()

    def stop(self) -> None:
        if self._loop is not None:
            try:
                import Quartz
                Quartz.CFRunLoopStop(self._loop)
            except Exception:  # noqa: BLE001
                pass


def request_input_monitoring() -> None:
    """Ask macOS for the Input Monitoring permission the hotkeys need (shows the system prompt once)."""
    try:
        import Quartz
        if hasattr(Quartz, "CGRequestListenEventAccess"):
            Quartz.CGRequestListenEventAccess()
    except Exception:  # noqa: BLE001
        pass


def has_input_monitoring() -> bool | None:
    try:
        import Quartz
        if hasattr(Quartz, "CGPreflightListenEventAccess"):
            return bool(Quartz.CGPreflightListenEventAccess())
    except Exception:  # noqa: BLE001
        pass
    return None


def open_input_monitoring_settings() -> None:
    subprocess.Popen(["open", "x-apple.systempreferences:com.apple.preference.security?Privacy_ListenEvent"])
