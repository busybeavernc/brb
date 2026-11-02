"""Mac logic that can be tested on any OS (no macOS APIs called)."""
from doppleganger import macos

SYSEXT_ENABLED = """1 extension(s)
--- com.apple.system_extension.cmio
enabled\tactive\tteamID\tbundleID (version)\tname\t[state]
*\t*\t2MMRE5MTB8\tcom.obsproject.obs-studio.mac-camera-extension (30.2.3/30.2.3)\tOBS Virtual Camera\t[activated enabled]
"""
SYSEXT_WAITING = SYSEXT_ENABLED.replace("[activated enabled]", "[activated waiting for user]")
SYSEXT_NONE = """0 extension(s)
"""


def test_obs_camera_state():
    assert macos.obs_camera_state(SYSEXT_ENABLED) == "enabled"
    assert macos.obs_camera_state(SYSEXT_WAITING) == "waiting"
    assert macos.obs_camera_state(SYSEXT_NONE) == "missing"


def test_obs_messages_point_to_the_next_step():
    assert "Install OBS Studio" in macos.obs_message(False, "missing")
    assert "Start Virtual Camera" in macos.obs_message(True, "missing")
    assert "System Settings" in macos.obs_message(True, "waiting")
    assert macos.OBS_DOWNLOAD_URL == "https://obsproject.com/download"


def test_parse_hotkey():
    assert macos.parse_hotkey("<f9>") == (101, 0)
    assert macos.parse_hotkey("<f10>") == (109, 0)
    code, mods = macos.parse_hotkey("<ctrl>+<alt>+l")
    assert code == 37 and mods == macos.MODIFIERS["ctrl"] | macos.MODIFIERS["alt"]


def test_mac_hotkeys_press_release_and_autorepeat():
    log = []
    hk = macos.MacHotkeys({"<f9>": (lambda: log.append("down"), lambda: log.append("up")),
                           "<f10>": (lambda: log.append("view"), None)})
    hk.handle(True, 101, 0)
    hk.handle(True, 101, 0, autorepeat=True)  # held: no repeats
    hk.handle(False, 101, 0)
    hk.handle(True, 109, 0)
    hk.handle(False, 109, 0)
    hk.handle(False, 101, 0)                  # stray key-up: ignored
    assert log == ["down", "up", "view"]


def test_mic_runner_restores_previous_volume(monkeypatch):
    calls = []

    def fake_run(cmd, timeout=6.0):
        calls.append(cmd[-1])
        return "72\n" if "input volume of" in cmd[-1] else ""

    monkeypatch.setattr(macos, "_run", fake_run)
    ids = macos.mic_runner(["mute"]).split()
    assert ids == ["volume:72"] and calls[-1] == "set volume input volume 0"
    macos.mic_runner(["unmute", *ids])
    assert calls[-1] == "set volume input volume 72"
