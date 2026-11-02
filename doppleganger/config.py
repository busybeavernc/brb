"""User settings, loaded from / saved to config.json next to the app."""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields


LEGACY_UPDATE_REPO = "busybeavernc/doppleganger"


@dataclass
class Config:
    # Real webcam (remembered by name; the index is only a fallback)
    camera_name: str = ""
    camera_index: int = 0
    width: int = 1280
    height: int = 720
    fps: int = 30

    # Loop recording
    loop_seconds: float = 90.0          # loop length in seconds: 60-600 (Settings > Recording)
    hold_still_seconds: float = 10.0    # "hold still" zone at the start AND end of the loop
    return_prompt_seconds: float = 10.0 # show the first frame this long before the end
    countdown_seconds: int = 3
    max_bridge_frames: int = 8          # motion-matched frames inserted at the loop seam
    loops_dir: str = "loops"
    active_loop: str = ""               # loop file in use (newest if empty)

    # Live <-> loop switching
    crossfade_ms: int = 0               # 0 = hard cut (default)
    overlay_opacity: float = 0.35       # preview: see-through strength of the off-air feed
    hotkey: str = "<f9>"                # global hotkey (works while Zoom is focused)
    view_hotkey: str = "<f10>"          # global: switch between the full window and the Mini Bar

    # Output camera: "auto" = Windows: our HappyHacks Virtual Camera; Mac: OBS Virtual Camera
    output_camera: str = "auto"         # auto | doppleganger | obs
    driver_prompted: bool = False       # (old) asked once whether to install the camera
    driver_auto_install: str = ""        # driver version the app last installed by itself (installs each new one once)

    # Updates (installed Doppleganger.exe only)
    auto_update: bool = True            # check GitHub Releases each time the app opens
    update_repo: str = "busybeavernc/doppleganger-public"  # public downloads (the code repo may be private)
    update_token: str = ""              # read-only GitHub token, needed only while the repo is private
    eula_accepted: str = ""             # EULA version accepted (asked again only when the EULA changes)
    license_key: str = ""               # Doppleganger license key (checked with brb.happyhacks.io)

    # Privacy
    mute_mic_on_loop: bool = True       # silence: mute the PC's microphones while the LOOP is on air
    loop_badge: bool = False            # a small BRB badge (bottom right) in what the meeting sees while the LOOP is on air
    hide_from_screen_share: bool = True # Doppleganger's windows are invisible to screen sharing

    # Window
    mirror_preview: bool = True
    text_scale: float = 1.0             # Text size: 0.85 (smaller) ... 1.75 (largest); Ctrl/Cmd + and -
    show_guides: bool = True
    show_outlines: bool = True          # Aqua (you) / Violet (loop) outlines in the preview
    show_record_tips: bool = True       # show "how to record a good loop" before each recording
    mac_permissions_seen: bool = False  # Mac: the permissions page has been shown once
    setup_done: bool = False            # first-run setup wizard completed (or skipped)
    mini_bar: bool = False              # start in the compact always-on-top bar
    mini_bar_pos: str = ""              # "+x+y" where you last left the mini bar

    @classmethod
    def load(cls, path: str) -> "Config":
        cfg = cls()
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            known = {f.name for f in fields(cls)}
            for k, v in data.items():
                if k in known:
                    setattr(cfg, k, v)
            try:
                cfg.loop_seconds = float(max(60, min(600, float(cfg.loop_seconds))))
            except (TypeError, ValueError):
                cfg.loop_seconds = 90.0
            if cfg.update_repo == LEGACY_UPDATE_REPO:  # releases moved to the public downloads repo
                cfg.update_repo = cls.update_repo
        return cfg

    def save(self, path: str) -> None:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2)
