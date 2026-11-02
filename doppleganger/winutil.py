"""Platform helpers: hide from screen sharing, start at sign-in. Windows here; Mac via macos.py."""
from __future__ import annotations

import logging
import os

log = logging.getLogger("doppleganger")

WDA_NONE = 0x0
WDA_MONITOR = 0x1
WDA_EXCLUDEFROMCAPTURE = 0x11  # Windows 10 2004+: window is invisible to screen sharing/recording


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "BRB"


def startup_command() -> str | None:
    """Command Windows runs at sign-in (installed .exe only). On a Mac: the .app (a LaunchAgent opens it)."""
    import sys
    if sys.platform == "darwin":
        from . import macos
        return macos.app_bundle()
    if os.name != "nt" or not getattr(sys, "frozen", False):
        return None
    return f'"{sys.executable}" --minimized'


def set_start_with_windows(enabled: bool) -> bool:
    """Start Doppleganger (minimized) when you sign in to Windows, so the camera always has a picture."""
    import sys
    if sys.platform == "darwin":
        from . import macos
        return macos.set_start_at_login(enabled)
    cmd = startup_command()
    if cmd is None:
        return False
    import winreg
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as k:
        if enabled:
            winreg.SetValueEx(k, RUN_NAME, 0, winreg.REG_SZ, cmd)
        else:
            try:
                winreg.DeleteValue(k, RUN_NAME)
            except FileNotFoundError:
                pass
    return True


def starts_with_windows() -> bool:
    import sys
    if sys.platform == "darwin":
        from . import macos
        return macos.starts_at_login()
    if os.name != "nt":
        return False
    import winreg
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as k:
            winreg.QueryValueEx(k, RUN_NAME)
        return True
    except OSError:
        return False


def toplevel_hwnd(widget) -> int | None:
    if os.name != "nt":
        return None
    import ctypes
    hwnd = widget.winfo_id()
    return ctypes.windll.user32.GetAncestor(hwnd, 2) or hwnd  # GA_ROOT


def hide_from_capture(widget, hide: bool = True) -> bool:
    """Exclude a window from screen sharing / screenshots (you still see it; viewers don't).
    Returns True if Windows accepted it."""
    import sys
    if sys.platform == "darwin":
        from . import macos
        try:
            widget.update_idletasks()
        except Exception:  # noqa: BLE001
            pass
        return macos.hide_all_windows_from_capture(hide)
    hwnd = toplevel_hwnd(widget)
    if not hwnd:
        return False
    import ctypes
    user32 = ctypes.windll.user32
    if not hide:
        return bool(user32.SetWindowDisplayAffinity(hwnd, WDA_NONE))
    if user32.SetWindowDisplayAffinity(hwnd, WDA_EXCLUDEFROMCAPTURE):
        return True
    # Older Windows: the window shows as a black box in captures instead of disappearing.
    ok = bool(user32.SetWindowDisplayAffinity(hwnd, WDA_MONITOR))
    log.info("Exclude-from-capture unavailable; using black-box fallback: %s", ok)
    return ok
