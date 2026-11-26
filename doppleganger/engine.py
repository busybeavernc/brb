"""BRB engine: camera in, loop/live out to the virtual camera, preview for the window."""
from __future__ import annotations

import logging
import os
import queue
import re
import threading
import time

import cv2
import numpy as np

from . import guides, library, updater
from . import vcam as vcamlib
from .calibrate import Calibrator
from . import camera_users, facekeys, lightmatch, namealert, scenecheck, voice
from .camera import Camera, NoCamera, placeholder
from .config import Config
from .cutcontrol import SYNC_THRESHOLD, CutController
from .hotkeys import GlobalHotkeys
from .mic import MicMuter
from . import segment
from . import outline
from .outline import OutlineTracer
from .player import LoopPlayer, latest_loop
from .recorder import Recorder, State
from .silhouette import Silhouette, glow_amount
from . import macos
from .license import LicenseGate, format_left
from .tray import start_tray
from . import brand

log = logging.getLogger("doppleganger")
BACKGROUND_FILE = "background.png"  # (older versions) calibrated empty room


class Output:
    """Sends frames to the virtual camera Zoom watches.

    Prefers our own "Doppleganger" camera; falls back to OBS Virtual Camera when
    ours isn't installed (or `output_camera` says so). If it can't connect, it
    retries every few seconds and the preview shows why - never silently.
    """

    RETRY_SECONDS = 3.0

    def __init__(self, cfg: Config, enabled: bool):
        self.cfg = cfg
        self.enabled = enabled
        self.cam = None
        self.backend = ""
        self.error = "" if enabled else "Virtual camera disabled (--no-vcam)"
        self.device = ""
        self._next_try = 0.0
        self._last_logged = ""
        self._lock = threading.RLock()   # send() runs on the steady output thread, reconnect() on the engine's
        if enabled:
            self._connect()

    def _pick_backend(self) -> str:
        choice = self.cfg.output_camera
        if choice in ("brb", "doppleganger"):   # "doppleganger": the setting's value in earlier versions
            return "doppleganger"
        if choice == "obs":
            return choice
        # auto: Windows always uses our own HappyHacks Virtual Camera (installed by the app/installer), never OBS;
        # a Mac uses OBS Virtual Camera.
        return "doppleganger" if vcamlib.is_windows() else "obs"

    def _connect(self) -> None:
        self._next_try = time.monotonic() + self.RETRY_SECONDS
        backend = self._pick_backend()
        try:
            if backend == "doppleganger":
                self.cam = vcamlib.DopplegangerCamera(self.cfg.width, self.cfg.height, self.cfg.fps)
            elif macos.is_mac():
                from . import macobs   # BRB's own helper: frames to OBS's camera extension
                self.cam = macobs.ObsCamera(self.cfg.width, self.cfg.height, self.cfg.fps)
            else:
                raise RuntimeError("OBS output is only used on a Mac - set output_camera to auto")
            self.backend, self.device, self.error = backend, self.cam.device, ""
            log.info("Virtual camera connected: %s", self.device)
        except Exception as e:  # noqa: BLE001
            self.cam = None
            self.error = _explain_vcam_error(backend, str(e))
            if self.error != self._last_logged:  # log each distinct problem once
                log.warning("Virtual camera (%s) not connected: %s", backend, e)
                self._last_logged = self.error

    def reconnect(self) -> None:
        with self._lock:
            self.close()
            if self.enabled:
                self._connect()

    @property
    def connected(self) -> bool:
        return self.cam is not None

    @property
    def viewer_connected(self) -> bool:
        return bool(getattr(self.cam, "viewer_connected", False))

    def send(self, frame: np.ndarray) -> None:
        with self._lock:
            self._send(frame)

    def _send(self, frame: np.ndarray) -> None:
        if self.cam is None:
            if self.enabled and time.monotonic() >= self._next_try:
                self._connect()
            return
        try:
            self.cam.send(frame)
        except Exception as e:  # noqa: BLE001
            log.warning("Virtual camera send failed: %s", e)
            self.close()
            self.error = f"Lost connection to {self.device or 'the virtual camera'} - reconnecting..."

    def close(self) -> None:
        if self.cam:
            try:
                self.cam.close()
            except Exception:  # noqa: BLE001
                pass
            self.cam = None


def _explain_vcam_error(backend: str, msg: str) -> str:
    m = msg.lower()
    if macos.is_mac():
        st = macos.obs_status()
        if not st["ready"] or "not installed" in m:
            return st["message"]
        if "stream" in m or "multiple" in m or "in use" in m:
            return "OBS Virtual Camera is busy - in OBS click 'Stop Virtual Camera' (or quit OBS)"
        return f"OBS Virtual Camera not connected: {msg}"
    if backend == "doppleganger":
        if vcamlib.installed_version() is None:
            return f"The {vcamlib.CAMERA_NAME} isn't installed yet - install it in Settings (Windows asks once)"
        if "already in use" in m:
            return "The HappyHacks Virtual Camera is in use by another copy of BRB - close it"
        return f"HappyHacks Virtual Camera not available ({msg}) - reinstall it in Settings"
    if "not installed" in m:
        return ("No camera to send to - install the HappyHacks Virtual Camera (Settings) "
                "or OBS Studio")
    if "could not be started" in m or "in use" in m or "already" in m:
        return "OBS Virtual Camera is busy - in OBS click 'Stop Virtual Camera' (or close OBS)"
    return f"Virtual camera not connected: {msg}"


def background_file(camera_name: str) -> str:
    """Calibration is per camera (each one sees the room differently)."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", camera_name).strip("-").lower() or "camera"
    return f"background-{slug}.png"


def use_loop_advice(info) -> str:
    """Shown when you pick a loop: it only looks natural if you, and the room, still look like that."""
    when = library.default_name(info.created)
    return (f"Using \"{info.name}\" (recorded {when}). If your lighting, clothes, hair or background have changed "
            "since then, record a new loop (R) so the switch looks natural.")


class Engine:
    """Everything that happens per video frame. Runs on its own thread (see run()).

    Other threads (the window, tray, hotkeys) never call methods that change state
    directly - they `request()` an action, which runs here between frames.
    """

    # Asked for while BRB is still starting up (webcam, AI and loop not ready yet): run them once it's ready.
    WAIT_FOR_STARTUP = ("toggle", "cut_press", "cut_release", "record", "record_now", "calibrate", "use_loop:")

    def __init__(self, cfg: Config, vcam: bool, config_path: str, progress=None, background_start: bool = True):
        """Quick: the window can open straight away. The slow parts - opening the webcam, loading the AI model and
        the loop - run on a background thread (start_up); until it's done, `starting` is True and the preview says
        so. background_start=False does them here instead (tests, tools)."""
        say = progress or (lambda _text, _p=None: None)  # startup progress for the splash screen
        self.cfg = cfg
        self.config_path = config_path
        self.starting = True
        self._deferred: list[str] = []
        self._cam_lock = threading.RLock()   # the camera watcher and camera switching take turns
        self._cams: list[str] = []
        self._cams_at = 0.0
        self.cameras_seq = 0                 # bumps when webcams are plugged in or removed (the window refreshes)
        self.camera_lost = False             # the webcam stopped delivering (unplugged, or taken by another app)
        self.camera_problem = ""             # while camera_lost: what's wrong and what to do (shown in the window)
        self.camera_unplugged = False
        self.camera_alert = ""               # set when the window should pop up camera_problem (once per episode)
        self._lost_since = 0.0
        self.camera = NoCamera()
        self.camera_name = cfg.camera_name or ""
        say("Connecting the virtual camera...", 0.6)
        self.output = Output(cfg, vcam)
        self.recorder = Recorder(cfg)
        self.seg = None                       # AI person segmentation (None -> room calibration fallback)
        self.mic = MicMuter()
        # LOOP time: free hour per computer or a license key, leased from brb.happyhacks.io
        self.lic = LicenseGate(cfg, lambda: cfg.save(config_path), updater.current_version())
        self.cut = CutController()
        self.mode = "live"
        self.fade_from: str | None = None
        self.fade_start = 0.0
        self.player: LoopPlayer | None = None
        self.loop_path: str | None = None
        self.loop_light = None
        self.live_light = None
        self.light = lightmatch.LightMatch()   # the loop, adjusted to the room's light now
        self.voice = voice.Voice(cfg)          # spoken guidance while recording, chimes on cuts
        self.names = namealert.Listener(cfg, self._name_heard)   # name alerts while the LOOP is on air
        self.name_alert: namealert.Alert | None = None           # showing until you're back (or dismiss it)
        self.name_alert_seq = 0
        # face guide and scene checks (analysis thread)
        self.face_target = self.face_live = None
        self.face_alignment: facekeys.Alignment | None = None
        self.scene_warnings: list[str] = []
        self._scene_seen: dict[str, int] = {}
        self._scene_loop: tuple[str, np.ndarray | None] | None = None
        self._latest_live: np.ndarray | None = None
        self._latest_loop: np.ndarray | None = None
        self.loop_sil: Silhouette | None = None
        self.loop_matcher: guides.PoseMatcher | None = None
        self.loop_running = False   # becomes True on the first cut to the loop
        self.sync = 0.0             # 0..1: how seamless a cut right now would be
        self.toast = ""
        self.toast_until = 0.0
        self._actions: queue.Queue[str] = queue.Queue()
        self._pending_update: str | None = None
        self._updating = False
        self.update_ready = ""                    # version downloaded in the background, installs at next start
        self.view_toggles = 0                     # bumps on the view hotkey: the window switches Normal / Mini Bar
        self.calibrator: Calibrator | None = None
        self._record_after_calibration = False
        self.tips_pending = False        # recording tips are showing; Start begins the recording
        self.ui_attached = False         # a window is open that can show the tips
        self.preview_size = (960, 540)            # set by the window
        self.compact_preview = False              # Mini Bar: the preview is a thumbnail
        self.rehearse_at: float | None = None     # Rehearse cut: when it started (preview only, nothing goes on air)
        self.badge_preview_rect = None            # where the watermark is in the preview (for dragging it)
        self.preview_rgb: np.ndarray | None = None
        self.library_version = 0                  # bumps when loops change, so the window refreshes
        self.running = False
        self.on_quit = None                       # set by the window
        self.ask = _ask_yes_no                    # replaced by the window's own dialogs
        self._light_at = 0.0
        self._tracer: OutlineTracer | None = None
        self._tracer_bg = None
        self.background = None
        self._start_global_hotkeys()
        # Mac: no tray icon (pystray needs the main thread, which Tk owns); the Dock icon and window do the job.
        self._tray = None if macos.is_mac() else start_tray(self.request, updater.current_version(), self)
        if background_start:
            threading.Thread(target=self.start_up, daemon=True, name="start-up").start()
        else:
            self.start_up()
            self._finish_start_up()

    # ---------- start-up (background) ----------
    def start_up(self) -> None:
        """The slow part of opening BRB: list and open the webcam, load the AI model. Then the engine thread loads
        the loop (_finish_start_up), which needs the AI. Never raises."""
        t0 = time.monotonic()
        try:
            self._open_first_camera()
        except Exception:  # noqa: BLE001
            log.exception("start-up: webcam")
        t1 = time.monotonic()
        try:
            self.seg = segment.get()
            self.recorder.segmenter = self.seg
        except Exception:  # noqa: BLE001
            log.exception("start-up: AI")
        log.info("Start-up: webcam %.1f s, AI %.1f s", t1 - t0, time.monotonic() - t1)
        self.request("_started")

    def _open_first_camera(self) -> None:
        cfg = self.cfg
        names = [n for _, n in vcamlib.real_cameras()]
        with self._cam_lock:
            self._cams, self._cams_at = names, time.monotonic()
            idx, self.camera_name = vcamlib.resolve_camera(cfg.camera_name, cfg.camera_index)
            try:
                self.camera = Camera(idx, cfg.width, cfg.height, cfg.fps)
                if (cfg.camera_name, cfg.camera_index) != (self.camera_name, idx):
                    cfg.camera_name, cfg.camera_index = self.camera_name, idx
                    cfg.save(self.config_path)
            except RuntimeError as e:   # no webcam (yet): the watcher opens one when it appears
                log.warning("No webcam at startup: %s", e)
                self.camera = NoCamera()
                self.camera_lost = True
                self._lost_since = time.monotonic()
                if not names:
                    self.camera_name = cfg.camera_name or ""
            self.cameras_seq += 1

    def _finish_start_up(self) -> None:
        """Engine thread: the webcam and AI are ready - load the calibration and the loop, then run what waited."""
        self._load_background()
        path = self._initial_loop()
        if path:
            try:
                self._load_loop(path, announce=False)
            except Exception:  # noqa: BLE001
                log.exception("Couldn't load the loop %s", path)
        self.starting = False
        for a in self._deferred:
            self.request(a)
        self._deferred.clear()

    # ---------- requests from other threads ----------
    def request(self, action: str) -> None:
        self._actions.put(action)

    # ---------- tray menu state (called from the tray thread) ----------
    def tray_cameras(self) -> list[str]:
        # kept fresh by the camera watcher (every few seconds); refreshed here too if the watcher is behind
        if time.monotonic() - self._cams_at > 10:
            self._cams = [n for _, n in vcamlib.real_cameras()]
            self._cams_at = time.monotonic()
        return list(self._cams)

    # ---------- webcams coming and going ----------
    WATCH_EVERY = 3.0     # how often to look for plugged-in / removed webcams (s)
    LOST_AFTER = 3.0      # no picture for this long = the webcam is gone

    def _start_camera_watcher(self) -> None:
        threading.Thread(target=self._watch_cameras, daemon=True, name="camera-watch").start()

    def _watch_cameras(self) -> None:
        """Background thread: notice webcams being plugged in or removed, and reconnect a lost one."""
        while self.running:
            time.sleep(self.WATCH_EVERY)
            if not self.running:
                break
            try:
                self._watch_once()
            except Exception:  # noqa: BLE001 - never let the watcher die
                log.exception("camera watcher")

    def _watch_once(self) -> None:
        if self.starting:   # start_up is opening the webcam
            return
        names = [n for _, n in vcamlib.real_cameras()]
        listed = bool(names) or not vcamlib.is_windows()
        old = self._cams
        self._cams, self._cams_at = names, time.monotonic()
        if names != old:
            self.cameras_seq += 1
            added = [n for n in names if n not in old]
            removed = [n for n in old if n not in names]
            log.info("Webcams changed: +%s -%s", added, removed)
            if added and not self.camera_lost:
                self.notify(f"New camera connected: {added[0]} - choose it under Webcam to use it.", 8)
        with self._cam_lock:
            silent = self.camera.silent_for()
            if not self.camera_lost and silent > self.LOST_AFTER:
                self.camera_lost, self._lost_since = True, time.monotonic()
                log.warning("Webcam %s stopped delivering (%.0f s)", self.camera_name, silent)
                self._explain_camera(names, listed)
                self.request("camera_lost")
                return
            if not self.camera_lost:
                return
            self._explain_camera(names, listed)
            if silent < 1.0:   # it came back by itself
                self._camera_back()
                return
            # Reconnect: the same webcam if it's back (its number may have changed), else - only if we never had
            # one - the first webcam there is.
            target = None
            if self.camera_name and (self.camera_name in names or not listed):
                target = self.camera_name
            elif isinstance(self.camera, NoCamera) and names:
                target = names[0]
            if target is None:
                return
            idx, name = vcamlib.resolve_camera(target, self.cfg.camera_index)
            if names and name != target:
                return
            try:
                cam = Camera(idx, self.cfg.width, self.cfg.height, self.cfg.fps)
            except RuntimeError:
                return
            deadline = time.monotonic() + 3
            while cam.latest() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if cam.latest() is None:
                cam.close()
                return
            old, self.camera = self.camera, cam
            old.close()
            changed = name != self.camera_name
            self.camera_name = name
            if changed or (self.cfg.camera_name, self.cfg.camera_index) != (name, idx):
                self.cfg.camera_name, self.cfg.camera_index = name, idx
                self.cfg.save(self.config_path)
            if changed:
                self.request("camera_background")
            self._camera_back()

    def _explain_camera(self, names: list[str], listed: bool) -> None:
        """Why there's no picture, in words: unplugged, none at all, or (usually) another app has it."""
        if listed and not names:
            self.camera_unplugged, problem = True, "No webcam found - plug one in."
        else:
            self.camera_unplugged = bool(self.camera_name) and listed and self.camera_name not in names
            apps = [] if self.camera_unplugged else camera_users.current()
            problem = camera_users.advice(self.camera_name, apps, self.camera_unplugged, self.output_camera_name())
        if problem != self.camera_problem:
            if not self.camera_problem and not self.camera_unplugged and names:
                self.camera_alert = problem   # first time: pop it up (unplugged / no webcam: the banner says enough)
            self.camera_problem = problem
            log.info("Webcam problem: %s", problem)

    def output_camera_name(self) -> str:
        return "HappyHacks Virtual Camera" if vcamlib.is_windows() else "OBS Virtual Camera"

    def _camera_back(self) -> None:
        self.camera_lost = False
        self.camera_problem, self.camera_unplugged = "", False
        self.cameras_seq += 1
        log.info("Webcam %s is back", self.camera_name)
        self.notify(f"Webcam connected: {self.camera_name}", 5)

    def tray_current_camera(self) -> str:
        return self.camera_name

    def tray_driver_installed(self) -> bool:
        return vcamlib.installed_version() is not None

    def tray_driver_current(self) -> bool:
        """Installed, and the version this BRB ships (not an earlier version's camera)."""
        return vcamlib.installed_version() == vcamlib.DRIVER_VERSION

    def tray_driver_available(self) -> bool:
        return vcamlib.bundled()

    # ---------- camera selection ----------
    def _load_background(self) -> None:
        path = background_file(self.camera_name)
        if not os.path.exists(path) and os.path.exists(BACKGROUND_FILE):
            path = BACKGROUND_FILE  # calibration saved by an older version
        self.background = cv2.imread(path, cv2.IMREAD_UNCHANGED) if os.path.exists(path) else None
        self.recorder.background = self.background
        if self.player is not None and self.player.first_frame is not None:
            self._make_loop_matcher()

    def switch_camera(self, name: str | None = None) -> None:
        """Use the named camera, or the next real camera if no name is given."""
        if self.recorder.state != State.IDLE or self.calibrator is not None:
            self.notify("Finish recording/calibrating before switching cameras.")
            return
        cams = vcamlib.real_cameras()
        if not cams:
            self.notify("Couldn't list cameras on this PC.")
            return
        if name is None:  # cycle
            names = [n for _, n in cams]
            name = names[(names.index(self.camera_name) + 1) % len(names)] if self.camera_name in names else names[0]
        match = next(((i, n) for i, n in cams if n == name), None)
        if match is None:
            self.notify(f"Camera not found: {name}")
            return
        if match[1] == self.camera_name and not self.camera_lost:
            return
        with self._cam_lock:
            old = self.camera
            try:
                self.camera = Camera(match[0], self.cfg.width, self.cfg.height, self.cfg.fps)
            except RuntimeError as e:
                log.info("Couldn't open %s: %s", match[1], e)
                msg = camera_users.advice(match[1], camera_users.current(), False, self.output_camera_name())
                self.notify(msg, 15)
                self.camera_alert = msg
                return
            old.close()
            self.camera_name = match[1]
            self.camera_lost = False
        self.cfg.camera_name, self.cfg.camera_index = match[1], match[0]
        self.cfg.save(self.config_path)
        self._load_background()
        tip = "" if self.background is not None else " - calibrate the silhouette for it"
        self.notify(f"Camera: {match[1]}{tip}", 6)
        log.info("Switched camera to %s (#%d)", match[1], match[0])

    def _on_camera_lost(self) -> None:
        """Engine thread: the webcam stopped. A recording can't continue; the LOOP, if on air, simply carries on."""
        if self.recorder.state != State.IDLE or self.calibrator is not None:
            self.cancel()   # a recording or calibration can't continue without a picture
        where = "Your LOOP stays on air. " if self.mode == "loop" else ""
        self.notify(where + (self.camera_problem or "Webcam disconnected. Plug it back in, or choose another under "
                                                    "Webcam."), 15)

    # ---------- HappyHacks Virtual Camera (our camera driver) ----------
    def install_driver(self, ask: bool = False) -> None:
        if not vcamlib.bundled():
            self.notify("The HappyHacks Virtual Camera is only included in the installed app.")
            return
        if ask and not self.ask(
                "Add the HappyHacks Virtual Camera?",
                "BRB can add its own camera, named \"HappyHacks Virtual Camera\", that your meeting apps and "
                "browsers can use - no OBS Studio needed.\n\nWindows will ask for permission once.\n\n"
                "Install it now?"):
            return
        self.notify("Installing the HappyHacks Virtual Camera - approve the Windows prompt...", 120)

        def work():
            ok, detail = vcamlib.install()
            log.info("Driver install: %s (%s)", ok, detail)
            if ok:
                self.request("driver_installed")
            else:
                self.notify(f"Camera install didn't finish ({detail}).", 8)

        threading.Thread(target=work, daemon=True).start()

    def uninstall_driver(self) -> None:
        if not self.ask("Remove the HappyHacks Virtual Camera?",
                        "Your meeting apps will no longer see the \"HappyHacks Virtual Camera\".\n\nRemove it?"):
            return
        self.output.close()  # release our handle on the DLL first

        def work():
            ok, detail = vcamlib.uninstall()
            self.notify("HappyHacks Virtual Camera removed." if ok else f"Removal didn't finish ({detail}).", 6)
            self.request("output_reconnect")

        threading.Thread(target=work, daemon=True).start()

    # ---------- loops ----------
    def _initial_loop(self) -> str | None:
        p = os.path.join(self.cfg.loops_dir, self.cfg.active_loop) if self.cfg.active_loop else ""
        return p if p and os.path.exists(p) else latest_loop(self.cfg.loops_dir)

    def _load_loop(self, path: str, announce: bool = True) -> None:
        new = LoopPlayer(path, (self.cfg.width, self.cfg.height))
        old, self.player = self.player, new   # swap first: the output thread always has a working player
        if old:
            old.close()
        self.loop_path = path
        self.loop_running = False
        self.sync = 0.0
        self.loop_light = library.info(path).lighting
        self.light.reset()
        self.scene_warnings, self._scene_seen, self._scene_loop = [], {}, None
        self.face_target = self.face_alignment = None
        # Start-pose silhouette + matcher, so LIVE preview shows when a cut to the loop will be invisible.
        self.loop_sil = Silhouette.load(path + ".mask.png") if os.path.exists(path + ".mask.png") else None
        self.loop_matcher = None
        if self.player.first_frame is not None:
            self._make_loop_matcher()
        if self.cfg.active_loop != os.path.basename(path):
            self.cfg.active_loop = os.path.basename(path)
            self.cfg.save(self.config_path)
        self.library_version += 1
        if announce:
            self.notify(f"Loop: {library.info(path).name}")

    def _make_loop_matcher(self) -> None:
        self.loop_matcher = guides.PoseMatcher(self.player.first_frame, background=self.background,
                                               segmenter=self.seg)
        if self.loop_sil is not None:
            self.loop_matcher.set_silhouette(self.loop_sil)

    def use_loop(self, path: str) -> None:
        if self.mode == "loop":
            self.notify("Cut back to LIVE before switching loops.")
            return
        if os.path.exists(path):
            self._load_loop(path, announce=False)
            self.notify(use_loop_advice(library.info(path)), secs=12.0)

    def delete_loop(self, path: str) -> None:
        if path == self.loop_path:
            if self.mode == "loop":
                self.notify("That loop is on air - cut to LIVE first.")
                return
            self.player.close()
            self.player, self.loop_path, self.loop_matcher, self.loop_sil = None, None, None, None
            self.loop_running = False
        library.delete(path)
        nxt = latest_loop(self.cfg.loops_dir)
        if self.player is None and nxt:
            self._load_loop(nxt, announce=False)
        elif self.player is None:
            self.cfg.active_loop = ""
            self.cfg.save(self.config_path)
        self.library_version += 1

    def _start_global_hotkeys(self) -> None:
        try:
            bindings = {
                self.cfg.hotkey: (lambda: self.request("cut_press"), lambda: self.request("cut_release")),
                self.cfg.view_hotkey: (lambda: self.request("toggle_view"), None),
            }
            # Mac: a Quartz event tap (pynput's listener can crash next to Tk on recent macOS).
            self._hk = macos.MacHotkeys(bindings) if macos.is_mac() else GlobalHotkeys(bindings)
            self._hk.start()
        except Exception as e:  # noqa: BLE001
            self._hk = None
            log.warning("Global hotkeys disabled: %s", e)

    def notify(self, msg: str, secs: float = 4.0) -> None:
        self.toast, self.toast_until = msg, time.monotonic() + secs

    @property
    def message(self) -> str:
        if self.recorder.state == State.PROCESSING:
            return self.recorder.message
        return self.toast if time.monotonic() < self.toast_until else ""

    # ---------- actions ----------
    def toggle(self) -> None:
        if self.recorder.state == State.READY:   # Space / F9 while getting ready = start the recording
            self.record_go()
            return
        if self.player is None:
            self.notify("No loop yet - record one first.")
            return
        if self.recorder.state == State.PROCESSING:
            self.notify("Still stitching your new loop - one moment.")
            return
        if self.recorder.state != State.IDLE or self.calibrator is not None:
            self.notify("Finish recording/calibrating first.")
            return
        new = "loop" if self.mode == "live" else "live"
        if new == "loop" and self.ui_attached and not self.cfg.eula_accepted:
            self.notify("Accept the license agreement first.", 6)
            return
        if new == "loop":
            ok, why = self.lic.can_start()
            if not ok:
                self.notify(why, 8)
                return
        if new == "loop" and not self.loop_running:
            # First activation starts at frame 1 (your start pose). From then on the loop
            # keeps playing in the background and is never restarted by a cut.
            self.player.restart()
            self.loop_running = True
        if self.cfg.crossfade_ms > 0:
            self.fade_from, self.fade_start = self.mode, time.monotonic()
        self.mode = new
        self._apply_mic()
        self.voice.chime(new)

    def _license_tick(self) -> None:
        """Every frame: keep LOOP time leased while the LOOP is on air; at 0:00 (or offline) cut back to LIVE."""
        on = self.mode == "loop"
        self.lic.on_air(on)
        if self.lic.unlimited != getattr(self, "_was_unlimited", self.lic.unlimited):
            if self.lic.unlimited:
                self.notify("License applied - unlimited LOOP time. Thank you!", 6)
        self._was_unlimited = self.lic.unlimited
        if not on:
            return
        why = self.lic.must_stop()
        if why:
            self._force_live(why)
        elif self.lic.warn_now():
            self.notify("5 minutes of free LOOP time left - then BRB cuts back to LIVE. "
                        "Click Get License Key for unlimited use.", 12)
            log.info("Free LOOP time: 5 minutes left")

    def _force_live(self, why: str) -> None:
        """Cut to LIVE now, whatever else is going on (free time used up, or no connection to the license server)."""
        log.info("Cut to LIVE by licensing: %s", why)
        self.fade_from = None
        self.mode = "live"
        self._apply_mic()
        self.notify(why, 15)

    def _apply_mic(self) -> None:
        """Silence while the LOOP is on air (if enabled); your mic is back the moment you're live.
        Name alerts listen only while the LOOP is on air, too."""
        if self.mode == "loop" and self.cfg.mute_mic_on_loop:
            self.mic.mute()
        else:
            self.mic.restore()
        self.apply_name_alerts()

    def apply_name_alerts(self) -> None:
        if self.mode == "loop" and self.cfg.name_alerts and namealert.supported():
            self.names.start()
        else:
            self.names.stop()
            if self.mode == "live":
                self.dismiss_name_alert()

    def _name_heard(self, alert: "namealert.Alert") -> None:
        if self.mode != "loop":
            return
        self.name_alert = alert
        self.name_alert_seq += 1

    def dismiss_name_alert(self) -> None:
        if self.name_alert is not None:
            self.name_alert = None
            self.name_alert_seq += 1

    def press_cut(self) -> None:
        self.cut.press(time.monotonic())

    def release_cut(self) -> None:
        r = self.cut.release(time.monotonic())
        if r == "cut":
            self.toggle()
        elif r == "cancel":
            self.notify("Hold-to-sync cancelled - you weren't in sync yet. Tap to cut anyway.", 5)

    @property
    def recording_active(self) -> bool:
        """Recording, counting down, or calibrating on the way to a recording."""
        return (self.recorder.state in (State.READY, State.COUNTDOWN, State.RECORDING)
                or (self.calibrator is not None and self._record_after_calibration))

    def record(self, confirmed: bool = False) -> None:
        """Record button / R: get ready for a recording (then Start: Enter / Space / the button) - or cancel the one
        in progress. With a window open, the first press shows the recording tips; their Start leads to the ready
        step."""
        if self.tips_pending and not confirmed:
            confirmed = True  # R pressed again while the tips are showing = start
        if self.recorder.state == State.READY:
            self.record_go()   # the Record button reads "Start recording" now
        elif self.recording_active:
            self.cancel_recording()
        elif self.recorder.state == State.PROCESSING:
            self.notify("Still stitching the last loop - one moment.")
        elif self.mode == "loop":
            self.notify("Cut back to LIVE before recording.")
        elif self.calibrator is not None:
            pass
        elif self.ui_attached and self.cfg.show_record_tips and not confirmed:
            self.tips_pending = True  # the window shows the tips; its Start button sends "record_now"
        elif self.background is None and self.seg is None:
            # No AI segmentation available: calibrate the room first, then record automatically.
            self.tips_pending = False
            self.calibrate(then_record=True)
        else:
            self.tips_pending = False
            self.recorder.prepare()

    def record_go(self) -> None:
        """Start the countdown - after "get into your starting position" has been said."""
        if self.recorder.state == State.READY:
            self.recorder.go(delay=self.voice.length("rec_get_ready"))

    def calibrate(self, then_record: bool = False) -> None:
        if self.mode == "loop" or self.recorder.state != State.IDLE:
            self.notify("Cut back to LIVE (and finish any recording) before calibrating.")
            return
        self.calibrator = Calibrator()
        self._record_after_calibration = then_record

    def cancel_recording(self) -> None:
        if self.calibrator is not None and self._record_after_calibration:
            self.calibrator = None
            self._record_after_calibration = False
        self.recorder.cancel()  # stops the countdown/recording and deletes the partial file
        self.recorder.message = ""
        self.notify("Recording cancelled - nothing was saved.")
        log.info("Recording cancelled by user")

    def cancel(self) -> None:
        """Esc: cancel whatever is in progress (tips, recording or calibration)."""
        if self.tips_pending:
            self.tips_pending = False
        elif self.recording_active:
            self.cancel_recording()
        elif self.calibrator is not None:
            self.calibrator = None
            self.notify("Calibration cancelled.")

    def toggle_outlines(self) -> None:
        self.cfg.show_outlines = not self.cfg.show_outlines
        self.cfg.save(self.config_path)
        if self.cfg.show_outlines and self.background is None and self.seg is None:
            self.notify("Outlines need a calibration for this webcam - see Settings.", 6)

    def _draw_face_guide(self, prev: np.ndarray) -> None:
        """The loop's eyes, nose and mouth, and yours, with a hint to line them up - LIVE (before cutting to the loop)
        and on the LOOP (before coming back). Not in the Mini Bar: too small to read there."""
        if not (self.cfg.show_face_guide and self.face_target is not None) or self.compact_preview:
            return
        al, target, live = self.face_alignment, self.face_target, self.face_live
        guides.draw_face_guide(prev, target.points(), live.points() if live is not None else None,
                               al.matched if al else [False] * 4,
                               al.hint if al else ("Look at the camera" if live is None else None),
                               self.cfg.mirror_preview, brand.CV_VIOLET, brand.CV_AQUA)

    def _ensure_tracer(self) -> bool:
        """The person outline finder (AI detection, else the calibrated room). False if neither is available."""
        if self.seg is not None:
            if self._tracer is None or self._tracer.segmenter is None:
                self._tracer = OutlineTracer(segmenter=self.seg)
            return True
        if self.background is None:
            return False
        if self._tracer is None or self._tracer_bg is not self.background:
            self._tracer, self._tracer_bg = OutlineTracer(self.background), self.background
        return True

    def _draw_sync_glow(self, prev: np.ndarray, live: np.ndarray) -> None:
        """A green glow around you when the cut would be seamless - the same signal as at the end of recording a
        loop. Fades in as you get close (glow_amount), full at the seamless point."""
        amount = glow_amount(self.sync)
        if amount <= 0 or not self._ensure_tracer():
            return
        h, w = prev.shape[:2]
        contour = self._tracer.contour(live, (w, h), self.cfg.mirror_preview, "live")
        if contour is None:
            return
        bold = 2 if self.compact_preview else 1
        glow = np.zeros_like(prev)
        cv2.drawContours(glow, [contour], -1, brand.CV_AQUA, max(8, w // 55) * bold, cv2.LINE_AA)
        glow = cv2.GaussianBlur(glow, (0, 0), max(4, w / 110) * bold)
        glow = cv2.add(glow, glow)
        inside = np.zeros((h, w), np.uint8)
        cv2.drawContours(inside, [contour], -1, 255, -1)
        glow[inside > 0] = 0                                   # around you, not over you
        cv2.addWeighted(prev, 1.0, glow, float(amount), 0, dst=prev)

    def _draw_outlines(self, prev: np.ndarray, live: np.ndarray, other: np.ndarray | None) -> None:
        """Green = you now, apricot = the loop. Overlapping lines = in sync."""
        if not self.cfg.show_outlines:
            return
        if not self._ensure_tracer():
            guides.put_text(prev, "Outlines: calibrate this webcam to see them", (12, prev.shape[0] - 14), 0.45)
            return
        size = (prev.shape[1], prev.shape[0])
        mirrored = self.cfg.mirror_preview
        # In the Mini Bar the preview is shrunk a lot: draw the lines bolder so they stay easy to see
        bold = max(1, round(prev.shape[1] / 300)) if self.compact_preview else 1
        if other is not None:
            # the loop as a soft violet shape, you as an aqua line on top: in sync = line on the shape's edge
            outline.draw_shape(prev, self._tracer.contour(other, size, mirrored, "other"), brand.CV_VIOLET,
                               edge=2 * bold)
        live_c = self._tracer.contour(live, size, mirrored, "live")
        outline.draw(prev, live_c, brand.CV_AQUA, thickness=2 * bold)
        now = time.monotonic()
        if now - getattr(self, "_outline_log_at", 0.0) > 10.0:  # diagnostics: what the tracer sees
            self._outline_log_at = now
            log.info("Outlines: preview %dx%d, live %s, loop %s, %s", size[0], size[1],
                     "drawn" if live_c is not None else "MISSING",
                     "n/a" if other is None else ("drawn" if self._tracer._cache.get("other", (0, 0, None))[2]
                                                  is not None else "MISSING"),
                     self._tracer.stats)
        if self.compact_preview:          # (no room for the labels in the Mini Bar)
            return
        h = prev.shape[0]
        x = 14 + guides.put_text(prev, "you", (14, h - 14), 0.45, brand.CV_AQUA, bold=True)
        if other is not None:
            guides.put_text(prev, "loop", (x + 12, h - 14), 0.45, brand.CV_VIOLET, bold=True)

    def _update_calibration(self, live: np.ndarray) -> None:
        cal = self.calibrator
        cal.add(live)
        if not cal.done():
            return
        self.calibrator = None
        bg = cal.background()
        if bg is None or cal.coverage < 0.6:
            self.notify("Calibration didn't see enough of the room - try again "
                        "(keep your face visible while you lean).", 10)
            return
        cv2.imwrite(background_file(self.camera_name), bg)
        self.background = bg
        self.recorder.background = bg
        if self.player is not None and self.player.first_frame is not None:
            self._make_loop_matcher()
        self.notify(f"Silhouette calibrated ({int(cal.coverage * 100)}% of the room seen).", 6)
        log.info("Calibrated background, coverage %.2f", cal.coverage)
        if self._record_after_calibration:
            self.recorder.start()

    AUTO_UPDATE_AFTER = 20.0   # s after opening: well clear of start-up, so it never slows it

    def _auto_update(self) -> None:
        """Background: find and download a newer version (or the move to the folder install). Installs the next
        time BRB opens - never in the middle of a meeting - or now via Settings > Restart to update."""
        time.sleep(self.AUTO_UPDATE_AFTER)
        if not (self.running and self.cfg.auto_update and updater.can_self_update()):
            return
        try:
            ready = updater.pending()
            if ready is None:
                rel = updater.check(self.cfg.update_repo, self.cfg.update_token)
                if rel is None:
                    return
                log.info("Downloading update v%s in the background", rel.version)
                ready = (rel.version, updater.stage(rel, self.cfg.update_token))
            self.update_ready, self._pending_update = ready
            log.info("Update v%s ready: installs next time BRB opens", ready[0])
            self.notify(f"BRB {ready[0]} is ready: it installs the next time you open BRB "
                        "(or now: Settings > Restart to update).", 12)
        except updater.UpdateError as e:
            log.info("Background update check: %s", e)
            if "couldn't install" in str(e):
                self.notify(str(e), 20)
        except Exception:  # noqa: BLE001
            log.exception("Background update check")

    def check_for_updates(self) -> None:
        """Manual check. Runs in the background; never blocks the camera."""
        if self._updating:
            return
        if not updater.can_self_update():
            self.notify("Updates are available in the installed app only.")
            return
        self._updating = True
        self.notify("Checking for updates...", 30)

        def work():
            try:
                rel = updater.check(self.cfg.update_repo, self.cfg.update_token)
                if rel is None:
                    self.notify(f"You're up to date (v{updater.current_version()}).")
                    return
                self.notify(f"Downloading v{rel.version}...", 600)
                path = updater.download(rel, self.cfg.update_token,
                                        lambda p: self.notify(f"Downloading v{rel.version}... {int(p * 100)}%", 600))
                with open(path + ".ready", "w", encoding="utf-8") as f:
                    f.write(rel.version)
                self._pending_update, self.update_ready = path, rel.version
                self.request("apply_update")
            except updater.UpdateError as e:
                self.notify(f"Update check failed: {e}", 8)
            finally:
                self._updating = False

        threading.Thread(target=work, daemon=True).start()

    def _handle_actions(self) -> bool:
        """Run queued actions on the engine thread. False = quit."""
        while not self._actions.empty():
            a = self._actions.get_nowait()
            arg = a.split(":", 1)[1] if ":" in a else ""
            if a == "_started":
                self._finish_start_up()
                continue
            if self.starting and a.startswith(self.WAIT_FOR_STARTUP):
                self._deferred.append(a)   # nothing to cut to or record with yet: do it in a moment
                continue
            if a == "toggle":
                self.toggle()
            elif a == "cut_press":
                self.press_cut()
            elif a == "cut_release":
                self.release_cut()
            elif a == "record":
                self.record()
            elif a == "record_go":
                self.record_go()
            elif a == "record_now":
                self.record(confirmed=True)
            elif a == "tips_closed":
                self.tips_pending = False
            elif a == "calibrate":
                self.calibrate()
            elif a == "cancel":
                self.cancel()
            elif a == "outlines":
                self.toggle_outlines()
            elif a == "rehearse":
                self.start_rehearsal()
            elif a == "preview_room":
                self.cfg.preview_room = not self.cfg.preview_room
                self.cfg.save(self.config_path)
                if not self.cfg.preview_room and self.seg is None:
                    self.notify("Hiding the background needs person detection, which isn't available on this "
                                "computer.", 8)
            elif a == "face_guide":
                self.cfg.show_face_guide = not self.cfg.show_face_guide
                self.cfg.save(self.config_path)
            elif a == "badge":
                self.cfg.loop_badge = not self.cfg.loop_badge
                self.cfg.save(self.config_path)
                self.notify("BRB badge on: the meeting sees it while the LOOP is on air." if self.cfg.loop_badge
                            else "BRB badge off.")
            elif a == "end_recording":
                self.recorder.end_now()
            elif a == "toggle_view":
                self.view_toggles += 1
            elif a.startswith("camera:"):
                self.switch_camera(arg or None)
            elif a.startswith("use_loop:"):
                self.use_loop(arg)
            elif a.startswith("delete_loop:"):
                self.delete_loop(arg)
            elif a == "camera_lost":
                self._on_camera_lost()
            elif a == "camera_background":
                self._load_background()
            elif a == "library_changed":
                self.library_version += 1
            elif a == "mic_setting":
                self._apply_mic()
            elif a == "name_alerts":
                self.names.stop()          # restart with the new names / sensitivity
                self.apply_name_alerts()
            elif a == "dismiss_name_alert":
                self.dismiss_name_alert()
            elif a == "install_driver":
                self.install_driver()
            elif a == "uninstall_driver":
                self.uninstall_driver()
            elif a == "driver_installed":
                self.output.reconnect()
                self.notify("HappyHacks Virtual Camera installed - choose \"HappyHacks Virtual Camera\" as your camera in your meeting app.", 10)
            elif a == "output_reconnect":
                self.output.reconnect()
            elif a == "update":
                self.check_for_updates()
            elif a == "quit":
                return False
            elif a == "apply_update" and self._pending_update:
                if self.recorder.state != State.IDLE or self.mode == "loop":
                    self.notify("Update downloaded - it installs when you're LIVE and not recording.", 10)
                    threading.Timer(3.0, self.request, ("apply_update",)).start()
                    continue
                self.close()
                updater.apply_and_restart(self._pending_update)
        return True

    # ---------- main loop (engine thread) ----------
    def run(self) -> None:
        self.running = True
        frame_dt = 1.0 / self.cfg.fps
        next_t = time.monotonic()
        live = self.camera.latest()
        waiting_placeholder = live is None
        if live is None:   # starting up: a calm placeholder until the webcam's first picture arrives
            live = placeholder(self.cfg.width, self.cfg.height, "Starting your webcam...")
        self._last_live = live
        self._start_camera_watcher()
        threading.Thread(target=self._auto_update, daemon=True, name="auto-update").start()
        threading.Thread(target=self._analyse, daemon=True, name="analysis").start()
        self._out_thread = threading.Thread(target=self._send_to_meeting, daemon=True, name="meeting-output")
        self._out_thread.start()
        self._ticks, self._ticks_at = 0, time.monotonic()
        try:
            while self.running:
                fresh = self.camera.latest()
                self._showing_message = waiting_placeholder
                if fresh is not None:
                    live, waiting_placeholder = fresh, False
                elif waiting_placeholder and not self.starting and self.camera_lost:
                    live, waiting_placeholder = placeholder(self.cfg.width, self.cfg.height,
                                                            "No webcam connected - plug one in"), False
                if self.calibrator is not None:
                    self._update_calibration(live)
                self.recorder.update(live)
                while self.recorder.events:
                    self.voice.say(self.recorder.events.pop(0))
                if self.recorder.result_path:
                    self._load_loop(self.recorder.result_path)
                    self.recorder.result_path = None
                if self.recorder.message and self.recorder.state == State.IDLE:
                    self.notify(self.recorder.message)
                    self.recorder.message = ""

                # Once activated, the loop runs on (by the clock) whether or not it's on air. What the meeting
                # sees is sent by the steady output thread (_send_to_meeting); this loop does the heavy work.
                self._last_live = live
                raw_loop = self.player.frame_now() if (self.loop_running and self.player) else None
                loop_frame = self.lit(raw_loop)
                if not waiting_placeholder:
                    self._latest_live, self._latest_loop = live, raw_loop
                out = self._compose(live, loop_frame, fade=False)

                self._license_tick()
                self._update_sync(live, loop_frame, out)
                if self.cut.tick(time.monotonic(), self.sync) == "cut":
                    self.toggle()
                now = time.monotonic()
                if now - self._light_at > 1.0:
                    self.live_light, self._light_at = library.lighting_of(live), now
                    if self.player is not None and not waiting_placeholder:
                        src = raw_loop if raw_loop is not None else self.player.first_frame
                        keep = lightmatch.background_mask(self.loop_sil.weight_small if self.loop_sil else None)
                        self.light.measure(src, live, keep)
                self.preview_rgb = self.render_preview(out, live, loop_frame)

                if not self._handle_actions():
                    break
                self._ticks += 1
                if now - self._ticks_at >= 30:   # how many pictures a second this computer manages (for the log)
                    log.info("Engine: %.1f frames/s (target %d) in %s mode", self._ticks / (now - self._ticks_at),
                             self.cfg.fps, self.mode.upper())
                    self._ticks, self._ticks_at = 0, now
                next_t += frame_dt
                delay = next_t - time.monotonic()
                if delay > 0:
                    time.sleep(delay)
                else:
                    next_t = time.monotonic()
        finally:
            self.close()
            if self.on_quit:
                self.on_quit()

    def stop(self) -> None:
        self.request("quit")

    def _send_to_meeting(self) -> None:
        """Output thread: exactly cfg.fps pictures a second to the virtual camera, on its own clock - the current
        loop frame (buffered, by time) or your newest camera picture - however busy the engine loop is."""
        dt = 1.0 / self.cfg.fps
        next_t = time.monotonic()
        while self.running:
            try:
                live = self.camera.latest()
                if live is None:
                    live = self._last_live
                player = self.player
                loop_frame = player.frame_for_output() if (self.loop_running and player is not None) else None
                frame = self._compose(live, self.lit(loop_frame))
                if self.cfg.loop_badge and self.mode == "loop":
                    frame = self.stamp_badge(frame)   # what the meeting sees
                self.output.send(frame)
            except Exception:  # noqa: BLE001 - keep sending; one bad frame must not stop the meeting's picture
                log.exception("meeting output")
                time.sleep(0.2)
            next_t += dt
            delay = next_t - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            else:
                next_t = time.monotonic()   # fell behind (the computer was busy): carry on from now

    def stamp_badge(self, frame: np.ndarray) -> np.ndarray:
        c = self.cfg
        return guides.stamp_badge(frame, c.badge_x, c.badge_y, c.badge_size)

    def _compose(self, live: np.ndarray, loop_frame: np.ndarray | None, fade: bool = True) -> np.ndarray:
        cur = loop_frame if (self.mode == "loop" and loop_frame is not None) else live
        if fade and self.fade_from and self.cfg.crossfade_ms > 0:
            t = (time.monotonic() - self.fade_start) * 1000 / self.cfg.crossfade_ms
            if t >= 1:
                self.fade_from = None
            else:
                prev = live if self.fade_from == "live" else loop_frame
                if prev is not None:
                    return cv2.addWeighted(prev, 1 - t, cur, t, 0)
        return cur

    def _update_sync(self, live, loop_frame, out) -> None:
        """How seamless would a cut be right now? (drives the overlay, the meter, and hold-to-sync)"""
        if self.loop_running and loop_frame is not None:
            target = loop_frame
        elif self.mode == "live" and self.loop_matcher is not None:
            self.sync = self.loop_matcher.update(live)  # first cut: the loop's start pose
            return
        else:
            self.sync = 0.0
            return
        weight = self.loop_sil.weight_small if self.loop_sil is not None else None
        diff = guides.frame_diff(guides.small_gray(live), guides.small_gray(target), weight)
        self.sync = 0.7 * self.sync + 0.3 * max(0.0, 1.0 - diff / 35.0)

    # ---------- face guide and scene checks (their own thread: they take a few to ~50 ms) ----------
    FACE_EVERY, SCENE_EVERY = 0.12, 2.5
    SCENE_CONFIRM, SCENE_CLEAR = 3, 2     # checks in a row to show a warning / to clear it

    def _analyse(self) -> None:
        last_scene = 0.0
        while self.running:
            time.sleep(self.FACE_EVERY)
            if self.starting or self.player is None or self._latest_live is None:
                continue
            try:
                self._face_tick()
                now = time.monotonic()
                if self.cfg.scene_checks and now - last_scene > self.SCENE_EVERY:
                    last_scene = now
                    self._scene_tick()
            except Exception:  # noqa: BLE001
                log.exception("analysis")
                time.sleep(2)

    def _face_tick(self) -> None:
        if not (self.cfg.show_face_guide and self.cfg.show_guides):
            self.face_live = self.face_alignment = None
            return
        finder = facekeys.get()
        if finder is None:
            return
        live, player = self._latest_live, self.player
        target_src = self._latest_loop if (self.loop_running and self._latest_loop is not None) else player.first_frame
        if target_src is None:
            return
        self.face_target = finder.find(target_src)
        self.face_live = finder.find(live)
        if self.face_target is not None and self.face_live is not None:
            h, w = live.shape[:2]
            self.face_alignment = facekeys.align(self.face_target, self.face_live, w / h, self.cfg.mirror_preview)
        else:
            self.face_alignment = None

    def _scene_tick(self) -> None:
        player, live = self.player, self._latest_live
        if player is None or player.first_frame is None or self.seg is None:
            return
        if self._scene_loop is None or self._scene_loop[0] != self.loop_path:
            self._scene_loop = (self.loop_path, self.seg.probability(player.first_frame))
        live_person = self.seg.probability(live)
        found = scenecheck.compare(self.light.apply(player.first_frame), self._scene_loop[1], live, live_person)
        msgs = found.messages()
        self.scene_warnings = self._confirm(msgs, found.detail)

    def _confirm(self, msgs: list[str], detail=None) -> list[str]:
        """Only what's been seen several checks in a row counts; it clears after a couple of checks without it.
        _scene_seen: + checks in a row seen, - checks in a row not seen."""
        seen = self._scene_seen
        for m in msgs:
            seen[m] = seen[m] + 1 if seen.get(m, 0) > 0 else 1
        for m in seen:
            if m not in msgs:
                seen[m] = seen[m] - 1 if seen[m] < 0 else -1
        shown = [m for m in self.scene_warnings if seen.get(m, 0) > -self.SCENE_CLEAR]
        for m, n in seen.items():
            if n >= self.SCENE_CONFIRM and m not in shown:
                shown.append(m)
                log.info("Scene: %s (%s)", m, detail)
                self.notify(f"⚠ {m}. If it shows in your meeting, record a new loop (R).", 12)
        return shown

    def lit(self, loop_frame: np.ndarray | None) -> np.ndarray | None:
        """A loop frame as the meeting should see it: matched to the room's light (if that's on)."""
        return self.light.apply(loop_frame) if self.cfg.match_lighting else loop_frame

    @property
    def lighting_warning(self) -> str | None:
        if self.cfg.match_lighting and self.light.loop_mean is not None:
            return self.light.warning()   # only what the adjustment can't make up for
        return library.lighting_warning(self.loop_light, self.live_light)

    # ---------- preview (drawn at the window's size, RGB for the window) ----------
    ROOM_BACKDROP = (58, 52, 48)      # BGR: a quiet dark grey behind you when the preview hides the room

    def _hide_room(self, img: np.ndarray, key: str = "base") -> np.ndarray:
        """Preview only: keep the person, replace the room with a plain backdrop (needs person detection)."""
        if self.seg is None:
            return img
        cache = self.__dict__.setdefault("_room_masks", {})
        tick = getattr(self, "_ticks", 0)
        hit = cache.get(key)
        if hit is not None and hit[1] == img.shape[:2] and 0 <= tick - hit[0] < 3:
            a = hit[2]                       # people move slowly: a mask a frame or two old is fine (saves CPU)
        else:
            try:
                prob = self.seg.probability(img)
            except Exception:  # noqa: BLE001
                return img
            a = cv2.resize(prob, (img.shape[1], img.shape[0]), interpolation=cv2.INTER_LINEAR)
            a = np.clip((cv2.GaussianBlur(a, (0, 0), 1.2) - 0.25) / 0.5, 0, 1)[..., None]
            cache[key] = (tick, img.shape[:2], a)
        back = np.empty_like(img)
        back[:] = self.ROOM_BACKDROP
        return (img * a + back * (1 - a)).astype(np.uint8)

    def _draw_offair_overlay(self, prev: np.ndarray, ghost_src: np.ndarray, label: str,
                             text_y: int = 34) -> np.ndarray:
        """Ghost `ghost_src` (the feed you'd cut to) over the preview of what's on air."""
        cfg = self.cfg
        ghost = cv2.flip(ghost_src, 1) if cfg.mirror_preview else ghost_src
        ghost = cv2.resize(ghost, (prev.shape[1], prev.shape[0]), interpolation=cv2.INTER_AREA)
        if not cfg.preview_room:
            ghost = self._hide_room(ghost, "ghost")
        prev = cv2.addWeighted(prev, 1 - cfg.overlay_opacity, ghost, cfg.overlay_opacity, 0)
        h, w = prev.shape[:2]
        if self.sync >= SYNC_THRESHOLD:
            cv2.rectangle(prev, (3, 3), (w - 4, h - 4), brand.CV_AQUA, 6)
            guides.put_text(prev, "In sync - cut is seamless", (12, text_y), 0.7, brand.CV_AQUA)
        else:
            guides.put_text(prev, f"Faded: {label}", (12, text_y), 0.55)
        return prev

    def _draw_calibration(self, prev: np.ndarray) -> None:
        cal = self.calibrator
        h, w = prev.shape[:2]
        mirrored = self.cfg.mirror_preview
        if cal.seen is not None:  # tint the parts of the room not seen yet
            unseen = (~cal.seen).astype(np.uint8) * 255
            unseen = cv2.resize(cv2.flip(unseen, 1) if mirrored else unseen, (w, h), interpolation=cv2.INTER_LINEAR)
            a = cv2.GaussianBlur(unseen, (0, 0), 3).astype(np.float32)[..., None] / 255.0 * 0.45
            prev[:] = (prev * (1 - a) + np.array([40, 40, 200], np.float32) * a).astype(np.uint8)
        need = cal.side_needed()
        if need is None:
            guides.banner(prev, "Got it - come back to the center", brand.CV_AQUA_DARK)
        else:
            # The uncovered side shows on the opposite side in a mirrored preview.
            screen_side = ({"left": "right", "right": "left"}[need]) if mirrored else need
            move = "LEFT" if screen_side == "right" else "RIGHT"
            guides.banner(prev, f"Calibrating - lean or step slowly to your {move}", brand.CV_VIOLET)
        guides.put_text(prev, "Keep facing the camera. Red areas haven't been seen yet.", (20, h - 70), 0.5)
        guides.draw_meter(prev, f"Room seen {int(cal.coverage * 100)}%", cal.coverage / cal.target, h - 45)
        if not cal.face_visible:
            guides.put_text(prev, "Can't see your face - turn toward the camera", (20, h - 95), 0.6, brand.CV_DANGER)

    def render_preview(self, out: np.ndarray, live: np.ndarray, loop_frame: np.ndarray | None) -> np.ndarray:
        cfg = self.cfg
        recording = self.recorder.state in (State.READY, State.COUNTDOWN, State.RECORDING)
        base = live if (recording or self.calibrator is not None) else out
        # a message instead of your picture (starting up / no webcam) isn't mirrored: it reads normally
        message_only = getattr(self, "_showing_message", False) and self.mode == "live"
        prev = cv2.flip(base, 1) if (cfg.mirror_preview and not message_only) else base
        w, h = self.preview_size
        prev = cv2.resize(prev, (max(2, w), max(2, h)), interpolation=cv2.INTER_AREA)
        if self.rehearse_at is not None and not recording and self.calibrator is None:
            shown = self._rehearsal_frame(out, live, loop_frame)
            if shown is not None:
                return cv2.cvtColor(shown, cv2.COLOR_BGR2RGB)
        hide_room = not cfg.preview_room and not recording and self.calibrator is None
        if hide_room:
            prev = self._hide_room(prev)
        if self.calibrator is not None:
            self._draw_calibration(prev)
        elif self.recorder.state == State.PROCESSING:
            guides.banner(prev, f"Stitching your new loop...  {int(self.recorder.progress * 100)}%",
                          brand.CV_VIOLET)
        elif recording:
            prev = self.recorder.draw(prev, cfg.mirror_preview)
        elif cfg.show_guides and self.loop_running and loop_frame is not None:
            other = live if self.mode == "loop" else loop_frame
            prev = self._draw_offair_overlay(prev, other, "live camera" if self.mode == "loop" else "loop")
            self._draw_outlines(prev, live, loop_frame)
            self._draw_face_guide(prev)
        elif cfg.show_guides and self.mode == "live" and self.loop_matcher is not None:
            # Before the first cut (a loop just chosen, or BRB just opened): the loop starts from its first frame when
            # you cut, so that's the faded picture to line up with, plus the start-pose outlines.
            if self.player is not None and self.player.first_frame is not None:
                prev = self._draw_offair_overlay(prev, self.lit(self.player.first_frame), "loop (start pose)")
            elif glow_amount(self.sync) >= 0.99:
                guides.put_text(prev, "In start pose - cut is seamless", (12, 34), 0.7, brand.CV_AQUA)
            self._draw_outlines(prev, live, self.player.first_frame if self.player else None)
            self._draw_face_guide(prev)
        if not recording and self.calibrator is None and self.recorder.state != State.PROCESSING and (
                self.loop_running or (self.mode == "live" and self.loop_matcher is not None)):
            self._draw_sync_glow(prev, live)
        if self.cut.waiting:
            guides.banner(prev, f"Hold to sync - cutting when in sync ({int(self.sync * 100)}%)", brand.CV_VIOLET)
        if self.camera_lost:
            guides.banner(prev, "Webcam disconnected - plug it back in, or choose another under Webcam"
                          if (self.camera_unplugged or not self.camera_problem) else
                          "No picture from your webcam - another app may be using it (see below)", brand.CV_DANGER)
        self.badge_preview_rect = None
        if cfg.loop_badge and base is out:
            # Where the meeting will see it, on the same part of you in this (maybe mirrored) preview, reading as
            # the meeting sees it. Faint while LIVE, so you can see and drag it into place before stepping away.
            cx = 1.0 - cfg.badge_x if cfg.mirror_preview else cfg.badge_x
            ph, pw = prev.shape[:2]
            prev = guides.stamp_badge(prev, cx, cfg.badge_y, cfg.badge_size,
                                      opacity=1.0 if self.mode == "loop" else 0.5)
            self.badge_preview_rect = guides.badge_rect(pw, ph, cx, cfg.badge_y, cfg.badge_size)
        self._draw_on_air(prev)
        left = self.lic.remaining() if self.mode == "loop" else None
        if left is not None and left <= 30:
            guides.banner(prev, f"Free LOOP time ends in {format_left(left)} - then back to LIVE", brand.CV_AMBER)
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB)

    # ---------- Rehearse cut: what the meeting would see, in the preview only ----------
    REHEARSE_BEFORE, REHEARSE_AFTER = 1.0, 2.2     # seconds of "now on air", then of "after the cut"

    def start_rehearsal(self) -> None:
        if self.player is None or (not self.loop_running and self.player.first_frame is None):
            self.notify("Record or choose a loop first - then you can rehearse the cut.", 6)
            return
        if self.recording_active or self.calibrator is not None:
            return
        self.rehearse_at = time.monotonic()

    def _rehearsal_frame(self, out: np.ndarray, live: np.ndarray, loop_frame: np.ndarray | None):
        """The preview during a rehearsal: what's on air, then the cut exactly as the meeting would get it
        (same lighting match, watermark and crossfade), then back to normal. Nothing changes on air."""
        t = time.monotonic() - self.rehearse_at
        if t >= self.REHEARSE_BEFORE + self.REHEARSE_AFTER:
            self.rehearse_at = None
            return None
        cfg = self.cfg
        if self.mode == "live":
            nxt = loop_frame if (self.loop_running and loop_frame is not None) else self.lit(self.player.first_frame)
            nxt_mode = "loop"
        else:
            nxt, nxt_mode = live, "live"
        if nxt is None:
            self.rehearse_at = None
            return None

        def as_meeting_sees(frame, mode):
            return self.stamp_badge(frame.copy()) if (mode == "loop" and cfg.loop_badge) else frame
        before = as_meeting_sees(out, self.mode)
        after = as_meeting_sees(nxt, nxt_mode)
        if t < self.REHEARSE_BEFORE:
            frame, label, mode = before, "now on air", self.mode
        else:
            k = (t - self.REHEARSE_BEFORE) * 1000 / cfg.crossfade_ms if cfg.crossfade_ms > 0 else 1.0
            frame = after if k >= 1 else cv2.addWeighted(before, 1 - k, after, k, 0)
            label, mode = "after the cut", nxt_mode
        prev = cv2.flip(frame, 1) if cfg.mirror_preview else frame
        w, h = self.preview_size
        prev = cv2.resize(prev, (max(2, w), max(2, h)), interpolation=cv2.INTER_AREA)
        if not cfg.preview_room:
            prev = self._hide_room(prev, "rehearsal")
        guides.banner(prev, f"REHEARSAL - not on air:  {label}  ({mode.upper()})", brand.CV_VIOLET)
        hh, ww = prev.shape[:2]
        cv2.rectangle(prev, (0, 0), (ww - 1, hh - 1), brand.CV_VIOLET if mode == "loop" else brand.CV_AQUA_DARK,
                      max(4, ww // 160) * 2)
        return prev

    def _draw_on_air(self, prev: np.ndarray) -> None:
        """What Zoom sees right now, impossible to miss: a pulsing ON AIR sign (top right) and a border in the
        mode's colour - green LIVE, apricot LOOP."""
        h, w = prev.shape[:2]
        if self.mode == "loop":
            mode, color = "LOOP", brand.CV_VIOLET
        else:
            mode, color = "LIVE", brand.CV_AQUA_DARK
        pulse = 0.5 + 0.5 * np.sin(time.monotonic() * 2 * np.pi * 0.8)   # ~0.8 Hz, like a studio light
        t = max(4, w // 160)
        border = np.zeros_like(prev)
        cv2.rectangle(border, (0, 0), (w - 1, h - 1), color, t * 2)
        a = 0.55 + 0.35 * pulse
        mask = border.any(axis=2)
        prev[mask] = (prev[mask] * (1 - a) + border[mask] * a).astype(prev.dtype)
        # ON AIR · LOOP
        scale = max(0.7, w / 1250)
        text = f"ON AIR  ·  {mode}"
        tw, th = guides.text_size(text, scale, bold=True)
        right, top = w - 14 - t, 12 + t
        x0 = right - tw - 58
        guides.rounded_rect(prev, (x0, top), (right, top + th + 18), color, (th + 18) // 2, opacity=0.92)
        r = max(6, th // 3)
        cy = top + (th + 18) // 2
        glow = tuple(int(255 * (0.45 + 0.55 * pulse)) for _ in range(3))
        cv2.circle(prev, (x0 + 20, cy), r + 3, (255, 255, 255), 2, cv2.LINE_AA)
        cv2.circle(prev, (x0 + 20, cy), r - 1, glow, -1, cv2.LINE_AA)
        guides.put_text(prev, text, (x0 + 38, top + 9 + th - 4), scale, (255, 255, 255), bold=True, shadow=False)

    def close(self) -> None:
        if getattr(self, "_closed", False):
            return
        self._closed = True
        self.running = False
        self.names.stop()
        t = getattr(self, "_out_thread", None)
        if t is not None and t is not threading.current_thread():
            t.join(timeout=1.0)   # let the meeting's output stop before its camera closes
        self.mic.restore()
        self.mic.flush()
        if getattr(self, "lic", None):
            self.lic.close()
        if getattr(self, "_hk", None):
            self._hk.stop()
        if getattr(self, "_tray", None):
            self._tray.stop()
        self.camera.close()
        self.output.close()
        if self.player:
            self.player.close()


def _ask_yes_no(title: str, text: str) -> bool:
    try:
        import ctypes
        return ctypes.windll.user32.MessageBoxW(None, text, title, 0x04 | 0x20 | 0x40000) == 6  # YES
    except Exception:  # noqa: BLE001 - not on Windows
        return False
