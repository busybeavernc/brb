"""Tray icon (by the clock) with the app's menu. Optional: skipped if pystray isn't installed."""
from __future__ import annotations

from typing import Callable


def start_tray(request: Callable[[str], None], version: str, state=None):
    """`state` (the App) supplies live menu contents: cameras, driver status."""
    try:
        import pystray
        from PIL import Image
    except Exception:  # noqa: BLE001 - no tray available (missing package or no desktop session)
        return None

    from . import brand
    try:
        img = Image.open(brand.asset("icon.png")).resize((64, 64), Image.LANCZOS)
    except OSError:
        img = Image.new("RGBA", (64, 64), brand.VIOLET)

    item = pystray.MenuItem

    def pick(name):
        return lambda icon, it: request(f"camera:{name}")

    def is_current(name):
        return lambda it: state.tray_current_camera() == name

    def camera_items():
        names = state.tray_cameras() if state else []
        if not names:
            yield item("(no cameras found)", None, enabled=False)
        for name in names:
            yield item(name, pick(name), checked=is_current(name), radio=True)

    def driver_ok():
        return bool(state and state.tray_driver_installed())

    def driver_offered():
        return bool(state and state.tray_driver_available())

    menu = pystray.Menu(
        item("Cut LIVE / LOOP", lambda: request("toggle"), default=True),
        item("Fake bad connection (glitch)", lambda: request("glitch")),
        item(lambda it: "Cancel recording" if (state and state.recording_active) else "Record new loop",
             lambda: request("record")),
        item("Calibrate silhouette", lambda: request("calibrate")),
        pystray.Menu.SEPARATOR,
        item("Camera", pystray.Menu(camera_items)),
        item("Install HappyHacks Virtual Camera", lambda: request("install_driver"),
             visible=lambda it: driver_offered() and not driver_ok()),
        item("Remove HappyHacks Virtual Camera", lambda: request("uninstall_driver"),
             visible=lambda it: driver_ok()),
        pystray.Menu.SEPARATOR,
        item("Check for updates", lambda: request("update")),
        item(f"Version {version}", None, enabled=False),
        pystray.Menu.SEPARATOR,
        item("Quit", lambda: request("quit")),
    )
    icon = pystray.Icon("BRB", img, "BRB", menu)
    icon.run_detached()
    return icon
