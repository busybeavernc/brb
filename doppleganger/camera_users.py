"""Which other programs are using a camera right now (Windows).

Windows records camera use per program for its privacy settings and the "camera in use" indicator, under
  HKCU\\Software\\Microsoft\\Windows\\CurrentVersion\\CapabilityAccessManager\\ConsentStore\\webcam
one key per Store app, and under NonPackaged one per desktop program (its path, with "\\" written as "#").
A program is using a camera now when its LastUsedTimeStart is set and its LastUsedTimeStop is 0.

It says which program, not which camera; BRB uses it to explain a webcam it can't get a picture from: usually a
meeting app still has it, instead of the HappyHacks Virtual Camera. Reading it needs no special permissions.
Elsewhere (macOS) this returns nothing and BRB gives the general advice.
"""
from __future__ import annotations

import os
from typing import Callable, Iterable

KEY = r"Software\Microsoft\Windows\CurrentVersion\CapabilityAccessManager\ConsentStore\webcam"

# Program file names (lower case) and Store app names -> what people call them
FRIENDLY = {
    "zoom.exe": "Zoom", "ms-teams.exe": "Microsoft Teams", "teams.exe": "Microsoft Teams",
    "msteams": "Microsoft Teams", "microsoftteams": "Microsoft Teams",
    "webex.exe": "Webex", "ciscocollabhost.exe": "Webex", "webexmta.exe": "Webex", "atmgr.exe": "Webex",
    "chrome.exe": "Google Chrome", "msedge.exe": "Microsoft Edge", "firefox.exe": "Firefox", "brave.exe": "Brave",
    "opera.exe": "Opera", "slack.exe": "Slack", "discord.exe": "Discord", "skype.exe": "Skype",
    "obs64.exe": "OBS Studio", "obs32.exe": "OBS Studio", "gotomeeting.exe": "GoTo Meeting",
    "g2mcomm.exe": "GoTo Meeting", "bluejeans.exe": "BlueJeans", "ringcentral.exe": "RingCentral",
    "microsoft.windowscamera": "the Windows Camera app", "whatsapp.exe": "WhatsApp",
    "5319275a.whatsappdesktop": "WhatsApp",
}
OURSELVES = ("brb.exe", "doppleganger.exe", "python.exe", "pythonw.exe")
BROWSERS = ("Google Chrome", "Microsoft Edge", "Firefox", "Brave", "Opera")


def friendly_name(key_name: str) -> str | None:
    """A registry key name -> a program's name, or None for BRB itself."""
    if "#" in key_name or key_name.lower().endswith(".exe"):           # a desktop program's path
        exe = key_name.replace("#", "\\").rsplit("\\", 1)[-1].lower()
        if exe in OURSELVES:
            return None
        if exe in FRIENDLY:
            return FRIENDLY[exe]
        return exe[:-4].replace("_", " ").title() if exe.endswith(".exe") else exe
    pkg = key_name.split("_", 1)[0].lower()                               # a Store app: Name_publisherid
    return FRIENDLY.get(pkg) or pkg.rsplit(".", 1)[-1].title()


def in_use(start: int | None, stop: int | None) -> bool:
    return bool(start) and not stop


def users(entries: Iterable[tuple[str, int | None, int | None]]) -> list[str]:
    """(key name, LastUsedTimeStart, LastUsedTimeStop) for each program -> names of those using a camera now."""
    out: list[str] = []
    for key_name, start, stop in entries:
        if in_use(start, stop):
            name = friendly_name(key_name)
            if name and name not in out:
                out.append(name)
    return out


def _registry_entries() -> list[tuple[str, int | None, int | None]]:
    import winreg

    def times(k) -> tuple[int | None, int | None]:
        vals = []
        for v in ("LastUsedTimeStart", "LastUsedTimeStop"):
            try:
                vals.append(int(winreg.QueryValueEx(k, v)[0]))
            except OSError:
                vals.append(None)
        return vals[0], vals[1]

    def subkeys(k):
        i = 0
        while True:
            try:
                yield winreg.EnumKey(k, i)
            except OSError:
                return
            i += 1

    entries = []
    for hive in (winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE):
        try:
            root = winreg.OpenKey(hive, KEY)
        except OSError:
            continue
        with root:
            for name in list(subkeys(root)):
                try:
                    with winreg.OpenKey(root, name) as k:
                        if name == "NonPackaged":
                            for sub in list(subkeys(k)):
                                try:
                                    with winreg.OpenKey(k, sub) as s:
                                        entries.append((sub, *times(s)))
                                except OSError:
                                    pass
                        else:
                            entries.append((name, *times(k)))
                except OSError:
                    pass
    return entries


def current(read: Callable[[], list] | None = None) -> list[str]:
    """Names of other programs using a camera right now ([] if unknown, or not on Windows)."""
    if read is None:
        if os.name != "nt":
            return []
        read = _registry_entries
    try:
        return users(read())
    except Exception:  # noqa: BLE001 - this only improves a message; never let it break anything
        return []


def advice(camera: str, apps: list[str], unplugged: bool = False,
           virtual: str = "HappyHacks Virtual Camera") -> str:
    """What to tell someone whose webcam BRB can't get a picture from."""
    if unplugged:
        return f"{camera or 'Your webcam'} is disconnected. Plug it back in, or choose another under Webcam."
    cam = camera or "your webcam"
    if apps:
        who = " and ".join(apps[:2])
        where = (f"the meeting's video settings in {who}" if apps[0] in BROWSERS else f"{who}'s video settings")
        return (f"{who} {'are' if len(apps[:2]) > 1 else 'is'} using a camera, so BRB can't get a picture from "
                f"{cam}. In {where}, choose \"{virtual}\" as the camera: BRB then picks up {cam} "
                "by itself.")
    return (f"BRB can't get a picture from {cam}. If a meeting app (Zoom, Teams, a browser...) is using it, choose "
            f"\"{virtual}\" as the camera in that app's video settings first.")
