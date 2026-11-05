"""Where BRB keeps your loops, calibrations and settings.

- Installed with the installer: %APPDATA%\\BRB (survives uninstall/reinstall);
  on a Mac: ~/Library/Application Support/BRB.
  Earlier versions used a "Doppleganger" folder there; it's renamed to BRB the first time this version runs.
- "Portable" copy (Doppleganger.exe in its own folder that already has config.json
  or loops/ next to it, like earlier versions): that folder, so nothing moves.
- Run from source: the project folder.
"""
from __future__ import annotations

import logging
import os
import sys

DATA_FOLDER = "BRB"
OLD_DATA_FOLDER = "Doppleganger"
OLD_LOGS = ("doppleganger.log", "doppleganger-setup.log", "doppleganger-error.log")


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
    d = move_old_folder(os.path.join(base, OLD_DATA_FOLDER), os.path.join(base, DATA_FOLDER))
    os.makedirs(d, exist_ok=True)
    return d


def move_old_folder(old: str, new: str) -> str:
    """Rename the data folder of an earlier version to its new name, once. Returns the folder to use: the new one,
    or the old one if it can't be moved right now (another copy of BRB is open, say) - nothing is ever lost."""
    if os.path.isdir(new) or not os.path.isdir(old):
        return new
    try:
        os.rename(old, new)
    except OSError as e:
        logging.getLogger("doppleganger").warning("couldn't rename %s to %s (%s); using it as it is", old, new, e)
        return old
    for name in OLD_LOGS:   # replaced by brb*.log; they only described earlier runs
        try:
            os.remove(os.path.join(new, name))
        except OSError:
            pass
    return new
