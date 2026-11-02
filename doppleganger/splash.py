"""Splash screen while BRB starts (loading the AI model and opening the webcam takes a few seconds).

- Packaged Windows .exe: PyInstaller's own splash appears the moment you double-click,
  before Python even starts (while the app unpacks); we close it when the window is up.
- Everywhere else (Mac .app, running from source): a small Tk window shown before the
  heavy libraries are imported, with a live status line.

Keep this module light: only tkinter and the standard library.
"""
from __future__ import annotations

import os
import sys

MIDNIGHT, CLOUD, MUTED, VIOLET, AQUA, TRACK = "#FAF8F3", "#244B4B", "#6B807C", "#244B4B", "#8FB9AA", "#E6E1D8"

_native = None   # PyInstaller's pyi_splash module, if this build has one
_tk = None       # our Tk splash


def _asset(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base, "doppleganger", "assets", name)


class _TkSplash:
    W = 560

    def __init__(self):
        import tkinter as tk
        self.tk = tk
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.configure(bg=MIDNIGHT)
        try:
            self.logo = tk.PhotoImage(file=_asset("splash_logo.png"))
            tk.Label(self.root, image=self.logo, bg=MIDNIGHT, bd=0).pack()
        except tk.TclError:
            tk.Label(self.root, text="BRB - Be Right Back", bg=MIDNIGHT, fg=CLOUD,
                     font=("Helvetica", 28, "bold"), pady=60).pack(fill="x")
        self.msg = tk.Label(self.root, text="Starting...", bg=MIDNIGHT, fg=MUTED, font=("Helvetica", 12))
        self.msg.pack(pady=(0, 14))
        self.bar = tk.Canvas(self.root, width=self.W, height=6, bg=TRACK, highlightthickness=0, bd=0)
        self.bar.pack()
        self.fill = self.bar.create_rectangle(0, 0, 0, 6, fill=VIOLET, width=0)
        self.root.update_idletasks()
        w, h = self.root.winfo_reqwidth(), self.root.winfo_reqheight()
        x = (self.root.winfo_screenwidth() - w) // 2
        y = (self.root.winfo_screenheight() - h) // 3
        self.root.geometry(f"{w}x{h}+{x}+{y}")
        self.root.deiconify()
        self.root.lift()
        self.root.update()

    def status(self, text: str, progress: float | None) -> None:
        self.msg.configure(text=text)
        if progress is not None:
            x = int(self.W * max(0.0, min(1.0, progress)))
            self.bar.coords(self.fill, 0, 0, x, 6)
            self.bar.itemconfigure(self.fill, fill=AQUA if progress >= 1 else VIOLET)
        self.root.update()

    def close(self) -> None:
        try:
            self.root.destroy()
        except self.tk.TclError:
            pass


def show() -> None:
    """Call first thing at launch (before importing OpenCV/MediaPipe)."""
    global _native, _tk
    try:
        import pyi_splash  # only exists inside a PyInstaller build made with --splash
        _native = pyi_splash
        return
    except ImportError:
        pass
    try:
        _tk = _TkSplash()
    except Exception:  # noqa: BLE001 - no display, Tk missing...: start without a splash
        _tk = None


def dismiss_native() -> None:
    """Close the packaged app's splash right away (for runs that open no window)."""
    try:
        import pyi_splash
        pyi_splash.close()
    except Exception:  # noqa: BLE001 - not a packaged build with a splash, or it's already closed
        pass


def status(text: str, progress: float | None = None) -> None:
    """Update the line under the logo ("Loading AI person detection...")."""
    try:
        if _tk is not None:
            _tk.status(text, progress)
        elif _native is not None:
            _native.update_text(text)
    except Exception:  # noqa: BLE001 - a splash must never stop the app from starting
        pass


def close() -> None:
    global _native, _tk
    try:
        if _tk is not None:
            _tk.close()
        if _native is not None and _native.is_alive():
            _native.close()
    except Exception:  # noqa: BLE001
        pass
    _native = _tk = None


def is_showing() -> bool:
    return _tk is not None or _native is not None
