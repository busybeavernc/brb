"""This computer's device id, for licensing: the same value after reinstalling Doppleganger (or Windows).

It comes from the hardware, read fresh each time the app starts and never saved anywhere, so there's no file or
registry value to edit:
  Windows  the motherboard's SMBIOS system UUID (read from the firmware table), else Windows' MachineGuid
  Mac      IOPlatformUUID (the hardware UUID shown in System Information)
  Linux    /etc/machine-id (development only)
The raw value never leaves the computer: the server only sees a one-way hash of it.
"""
from __future__ import annotations

import ctypes
import hashlib
import logging
import os
import platform
import re
import subprocess
import sys
import uuid

log = logging.getLogger("doppleganger")
SALT = "doppleganger-device-v1|"
_BAD = {"00000000-0000-0000-0000-000000000000", "ffffffff-ffff-ffff-ffff-ffffffffffff",
        "03000200-0400-0500-0006-000700080009"}   # placeholder UUIDs some boards ship with
_cached: str | None = None


def _ok(u: str | None) -> str | None:
    u = (u or "").strip().lower()
    return u if re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", u) and u not in _BAD else None


def smbios_uuid_from_table(raw: bytes) -> str | None:
    """System UUID from a RawSMBIOSData blob (GetSystemFirmwareTable 'RSMB'): 8-byte header, then the structures."""
    if len(raw) < 8:
        return None
    data = raw[8:8 + int.from_bytes(raw[4:8], "little")]
    i = 0
    while i + 4 <= len(data):
        typ, length = data[i], data[i + 1]
        if length < 4:
            break
        if typ == 1 and length >= 0x19 and i + 24 <= len(data):
            return _ok(str(uuid.UUID(bytes_le=bytes(data[i + 8:i + 24]))))
        j = i + length                      # skip the formatted part, then the strings (ending in a double NUL)
        while j + 1 < len(data) and not (data[j] == 0 and data[j + 1] == 0):
            j += 1
        i = j + 2
        if typ == 127:                      # end of table
            break
    return None


def _windows() -> str | None:
    try:
        k32 = ctypes.windll.kernel32
        rsmb = 0x52534D42
        size = k32.GetSystemFirmwareTable(rsmb, 0, None, 0)
        if size:
            buf = ctypes.create_string_buffer(size)
            if k32.GetSystemFirmwareTable(rsmb, 0, buf, size):
                u = smbios_uuid_from_table(buf.raw)
                if u:
                    return "smbios:" + u
    except Exception as e:  # noqa: BLE001
        log.info("SMBIOS UUID unavailable: %s", e)
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography", 0,
                            winreg.KEY_READ | getattr(winreg, "KEY_WOW64_64KEY", 0)) as k:
            return "machineguid:" + str(winreg.QueryValueEx(k, "MachineGuid")[0]).lower()
    except OSError:
        return None


def _mac() -> str | None:
    try:
        out = subprocess.run(["ioreg", "-rd1", "-c", "IOPlatformExpertDevice"], capture_output=True, timeout=5).stdout
        m = re.search(rb'"IOPlatformUUID"\s*=\s*"([0-9A-Fa-f-]{36})"', out)
        u = _ok(m.group(1).decode()) if m else None
        return "platform:" + u if u else None
    except (OSError, subprocess.SubprocessError):
        return None


def _linux() -> str | None:
    for p in ("/etc/machine-id", "/var/lib/dbus/machine-id"):
        try:
            with open(p, encoding="ascii") as f:
                v = f.read().strip()
            if v:
                return "machine-id:" + v
        except OSError:
            pass
    return None


def device_id() -> str:
    """64 hex characters, stable for this computer."""
    global _cached
    if _cached is None:
        raw = _windows() if os.name == "nt" else _mac() if sys.platform == "darwin" else _linux()
        if not raw:   # last resort: the network adapter's address
            raw = "node:%012x" % uuid.getnode()
            log.warning("No hardware id found; using the network adapter address for licensing")
        _cached = hashlib.sha256((SALT + raw).encode()).hexdigest()
    return _cached


def device_name() -> str:
    return (platform.node() or "Computer")[:80]


def platform_name() -> str:
    return "windows" if os.name == "nt" else "mac" if sys.platform == "darwin" else sys.platform
