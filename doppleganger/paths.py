"""Where Doppleganger keeps your loops, calibrations and settings.

- Installed with the installer: %APPDATA%\\Doppleganger (survives uninstall/reinstall);
  on a Mac: ~/Library/Application Support/Doppleganger
- "Portable" copy (Doppleganger.exe in its own folder that already has config.json
  or loops/ next to it, like earlier versions): that folder, so nothing moves.
- Run from source: the project folder.
"""
from __future__ import annotations

import os
import sys


def exe_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def is_portable() -> bool:
    if sys.platform == "darwin":
        return False  # a Mac .app keeps its data in Application Support
    d = exe_dir()
    return any(os.path.exists(os.path.join(d, n)) for n in ("config.json", "loops", "portable.txt"))


def data_dir() -> str:
    if not getattr(sys, "frozen", False) or is_portable():
        return exe_dir()
    if sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("APPDATA") or os.path.expanduser("~")
    d = os.path.join(base, "Doppleganger")   # folder name kept from before the rename to BRB
    os.makedirs(d, exist_ok=True)
    return d
