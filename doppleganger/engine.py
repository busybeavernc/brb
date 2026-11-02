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
        if enabled:
            self._connect()

    def _pick_backend(self) -> str:
        choice = self.cfg.output_camera
        if choice in ("doppleganger", "obs"):
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
            else:
                import pyvirtualcam
                self.cam = pyvirtualcam.Camera(self.cfg.width, self.cfg.height, self.cfg.fps,
                                               fmt=pyvirtualcam.PixelFormat.BGR, backend="obs")
            self.backend, self.device, self.error = backend, self.cam.device, ""
            log.info("Virtual camera connected: %s", self.device)
        except Exception as e:  # noqa: BLE001
            self.cam = None
            self.error = _explain_vcam_error(backend, str(e))
            if self.error != self._last_logged:  # log each distinct problem once
                log.warning("Virtual camera (%s) not connected: %s", backend, e)
                self._last_logged = self.error

    def reconnect(self) -> None:
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


class Engine:
    """Everything that happens per video frame. Runs on its own thread (see run()).

    Other threads (the window, tray, hotkeys) never call methods that change state
    directly - they `request()` an action, which runs here between frames.
    """

    def __init__(self, cfg: Config, vcam: bool, config_path: str, progress=None):
        say = progress or (lambda _text, _p=None: None)  # startup progress for the splash screen
        self.cfg = cfg
        self.config_path = config_path
        say("Opening your webcam...", 0.5)
        self._cam_lock = threading.RLock()   # the camera watcher and camera switching take turns
        self._cams: list[str] = [n for _, n in vcamlib.real_cameras()]
        self._cams_at = time.monotonic()
        self.cameras_seq = 0                 # bumps when webcams are plugged in or removed (the window refreshes)
        self.camera_lost = False             # the webcam stopped delivering (unplugged, or taken by another app)
        self._lost_since = 0.0
        idx, self.camera_name = vcamlib.resolve_camera(cfg.camera_name, cfg.camera_index)
        try:
            self.camera = Camera(idx, cfg.width, cfg.height, cfg.fps)
            if (cfg.camera_name, cfg.camera_index) != (self.camera_name, idx):
                cfg.camera_name, cfg.camera_index = self.camera_name, idx
                cfg.save(config_path)
        except RuntimeError as e:   # no webcam plugged in (yet): start anyway; the watcher opens one when it appears
            log.warning("No webcam at startup: %s", e)
            self.camera = NoCamera()
            self.camera_lost = True
            self._lost_since = time.monotonic()
            if not self._cams:
                self.camera_name = cfg.camera_name or ""
        say("Connecting the virtual camera...", 0.6)
        self.output = Output(cfg, vcam)
        self.recorder = Recorder(cfg)
        say("Loading AI person detection...", 0.7)
        self.seg = segment.get()              # AI person segmentation (None -> room calibration fallback)
        say("Loading your loops...", 0.85)
        self.recorder.segmenter = self.seg
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
        self.loop_sil: Silhouette | None = None
        self.loop_matcher: guides.PoseMatcher | None = None
        self.loop_running = False   # becomes True on the first cut to the loop
        self.sync = 0.0             # 0..1: how seamless a cut right now would be
        self.toast = ""
        self.toast_until = 0.0
        self._actions: queue.Queue[str] = queue.Queue()
        self._pending_update: str | None = None
        self._updating = False
        self.view_toggles = 0                     # bumps on the view hotkey: the window switches Normal / Mini Bar
        self.calibrator: Calibrator | None = None
        self._record_after_calibration = False
        self.tips_pending = False        # recording tips are showing; Start begins the recording
        self.ui_attached = False         # a window is open that can show the tips
        self.preview_size = (960, 540)            # set by the window
        self.preview_rgb: np.ndarray | None = None
        self.library_version = 0                  # bumps when loops change, so the window refreshes
        self.running = False
        self.on_quit = None                       # set by the window
        self.ask = _ask_yes_no                    # replaced by the window's own dialogs
        self._light_at = 0.0
        self._tracer: OutlineTracer | None = None
        self._tracer_bg = None
        self._load_background()
        path = self._initial_loop()
        if path:
            self._load_loop(path, announce=False)
        self._start_global_hotkeys()
        # Mac: no tray icon (pystray needs the main thread, which Tk owns); the Dock icon and window do the job.
        self._tray = None if macos.is_mac() else start_tray(self.request, updater.current_version(), self)

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
                self.request("camera_lost")
                return
            if not self.camera_lost:
                return
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

    def _camera_back(self) -> None:
        self.camera_lost = False
        self.cameras_seq += 1
        log.info("Webcam %s is back", self.camera_name)
        self.notify(f"Webcam connected: {self.camera_name}", 5)

    def tray_current_camera(self) -> str:
        return self.camera_name

    def tray_driver_installed(self) -> bool:
        return vcamlib.installed_version() is not None

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
                self.notify(str(e), 6)
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
        where = " - your LOOP stays on air" if self.mode == "loop" else ""
        self.notify(f"Webcam disconnected{where}. Plug it back in, or choose another under Webcam.", 10)

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
        if self.player:
            self.player.close()
        self.player = LoopPlayer(path, (self.cfg.width, self.cfg.height))
        self.loop_path = path
        self.loop_running = False
        self.sync = 0.0
        self.loop_light = library.info(path).lighting
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
            self._load_loop(path)

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
        """Silence while the LOOP is on air (if enabled); your mic is back the moment you're live."""
        if self.mode == "loop" and self.cfg.mute_mic_on_loop:
            self.mic.mute()
        else:
            self.mic.restore()

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
        return (self.recorder.state in (State.COUNTDOWN, State.RECORDING)
                or (self.calibrator is not None and self._record_after_calibration))

    def record(self, confirmed: bool = False) -> None:
        """Record button / R: starts a recording - or cancels the one in progress.
        With a window open, the first press shows the recording tips; Start (or R again) begins."""
        if self.tips_pending and not confirmed:
            confirmed = True  # R pressed again while the tips are showing = start
        if self.recording_active:
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
            self.recorder.start()

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

    def _draw_outlines(self, prev: np.ndarray, live: np.ndarray, other: np.ndarray | None) -> None:
        """Green = you now, apricot = the loop. Overlapping lines = in sync."""
        if not self.cfg.show_outlines:
            return
        if self.seg is not None:
            if self._tracer is None or self._tracer.segmenter is None:
                self._tracer = OutlineTracer(segmenter=self.seg)
        elif self.background is None:
            guides.put_text(prev, "Outlines: calibrate this webcam to see them", (12, prev.shape[0] - 14), 0.45)
            return
        elif self._tracer is None or self._tracer_bg is not self.background:
            self._tracer, self._tracer_bg = OutlineTracer(self.background), self.background
        size = (prev.shape[1], prev.shape[0])
        mirrored = self.cfg.mirror_preview
        if other is not None:
            # the loop as a soft violet shape, you as an aqua line on top: in sync = line on the shape's edge
            outline.draw_shape(prev, self._tracer.contour(other, size, mirrored, "other"), brand.CV_VIOLET)
        live_c = self._tracer.contour(live, size, mirrored, "live")
        outline.draw(prev, live_c, brand.CV_AQUA)
        now = time.monotonic()
        if now - getattr(self, "_outline_log_at", 0.0) > 10.0:  # diagnostics: what the tracer sees
            self._outline_log_at = now
            log.info("Outlines: preview %dx%d, live %s, loop %s, %s", size[0], size[1],
                     "drawn" if live_c is not None else "MISSING",
                     "n/a" if other is None else ("drawn" if self._tracer._cache.get("other", (0, 0, None))[2]
                                                  is not None else "MISSING"),
                     self._tracer.stats)
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
                self._pending_update = path
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
            if a == "toggle":
                self.toggle()
            elif a == "cut_press":
                self.press_cut()
            elif a == "cut_release":
                self.release_cut()
            elif a == "record":
                self.record()
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
        live = None
        first_by = time.monotonic() + 4
        while live is None and self.running and time.monotonic() < first_by:
            live = self.camera.latest()
            time.sleep(0.01)
        if live is None:   # no webcam (yet): a calm placeholder until one is plugged in
            live = placeholder(self.cfg.width, self.cfg.height, "No webcam connected - plug one in")
        self._start_camera_watcher()
        try:
            while self.running:
                fresh = self.camera.latest()
                if fresh is not None:
                    live = fresh
                if self.calibrator is not None:
                    self._update_calibration(live)
                self.recorder.update(live)
                if self.recorder.result_path:
                    self._load_loop(self.recorder.result_path)
                    self.recorder.result_path = None
                if self.recorder.message and self.recorder.state == State.IDLE:
                    self.notify(self.recorder.message)
                    self.recorder.message = ""

                # Once activated, the loop advances every frame whether or not it's on air.
                loop_frame = self.player.next_frame() if (self.loop_running and self.player) else None
                out = self._compose(live, loop_frame)
                self.output.send(out)

                self._license_tick()
                self._update_sync(live, loop_frame, out)
                if self.cut.tick(time.monotonic(), self.sync) == "cut":
                    self.toggle()
                now = time.monotonic()
                if now - self._light_at > 1.0:
                    self.live_light, self._light_at = library.lighting_of(live), now
                self.preview_rgb = self.render_preview(out, live, loop_frame)

                if not self._handle_actions():
                    break
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

    def _compose(self, live: np.ndarray, loop_frame: np.ndarray | None) -> np.ndarray:
        cur = loop_frame if (self.mode == "loop" and loop_frame is not None) else live
        if self.fade_from and self.cfg.crossfade_ms > 0:
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

    @property
    def lighting_warning(self) -> str | None:
        return library.lighting_warning(self.loop_light, self.live_light)

    # ---------- preview (drawn at the window's size, RGB for the window) ----------
    def _draw_offair_overlay(self, prev: np.ndarray, ghost_src: np.ndarray, label: str,
                             text_y: int = 34) -> np.ndarray:
        """Ghost `ghost_src` (the feed you'd cut to) over the preview of what's on air."""
        cfg = self.cfg
        ghost = cv2.flip(ghost_src, 1) if cfg.mirror_preview else ghost_src
        ghost = cv2.resize(ghost, (prev.shape[1], prev.shape[0]), interpolation=cv2.INTER_AREA)
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
        recording = self.recorder.state in (State.COUNTDOWN, State.RECORDING)
        base = live if (recording or self.calibrator is not None) else out
        prev = cv2.flip(base, 1) if cfg.mirror_preview else base
        w, h = self.preview_size
        prev = cv2.resize(prev, (max(2, w), max(2, h)), interpolation=cv2.INTER_AREA)
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
        elif cfg.show_guides and self.mode == "live" and self.loop_matcher is not None:
            # Before the first cut: line up with the loop's start pose (violet shape + your aqua line;
            # the white silhouette is only used while recording).
            glow = glow_amount(self.sync)
            if glow >= 0.99:
                guides.put_text(prev, "In start pose - cut is seamless", (12, 34), 0.7, brand.CV_AQUA)
            self._draw_outlines(prev, live, self.player.first_frame if self.player else None)
        if self.cut.waiting:
            guides.banner(prev, f"Hold to sync - cutting when in sync ({int(self.sync * 100)}%)", brand.CV_VIOLET)
        if self.camera_lost:
            guides.banner(prev, "Webcam disconnected - plug it back in, or choose another under Webcam",
                          brand.CV_DANGER)
        self._draw_on_air(prev)
        left = self.lic.remaining() if self.mode == "loop" else None
        if left is not None and left <= 30:
            guides.banner(prev, f"Free LOOP time ends in {format_left(left)} - then back to LIVE", brand.CV_AMBER)
        return cv2.cvtColor(prev, cv2.COLOR_BGR2RGB)

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
