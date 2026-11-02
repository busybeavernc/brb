"""Doppleganger's windows: main control window, always-on-top mini bar, settings, first-run setup.

Branded Midnight / Violet / Aqua (see brand.py). All windows are excluded from
screen capture (when enabled), so screen-sharing viewers never see them. The
engine runs on its own thread; the window only reads its state and sends it requests.
"""
from __future__ import annotations

import os
import sys
import threading
import tkinter as tk
from tkinter import messagebox, simpledialog, ttk

import cv2
from PIL import Image, ImageTk

from . import brand, library, macos, updater, winutil
from .brand import AMBER, AQUA, BORDER, CLOUD, DANGER, MIDNIGHT, MUTED, SURFACE, SURFACE_HI, VIOLET, VIOLET_DARK
from .cutcontrol import SYNC_THRESHOLD
from .license import WARN_AT, format_left
from .recorder import State

IS_MAC = sys.platform == "darwin"
FONT = "Segoe UI" if os.name == "nt" else ("Helvetica Neue" if IS_MAC else "Inter")
WHEN_STARTS = "when you log in to your Mac" if IS_MAC else "when Windows starts"

# ---------- text size ----------
# Every font is a shared Tk font object, so changing the text size updates all open windows at once.
# Sizes in this file are written for Windows; Tk on a Mac draws points much smaller, so there they're
# scaled to the Mac's standard system text size (13 pt), then by the user's Text size setting.
TEXT_SCALES = (0.85, 1.0, 1.15, 1.3, 1.5, 1.75)
_fonts: dict = {}
_text = {"platform": 1.0, "user": 1.0}


def _platform_factor() -> float:
    if not IS_MAC:
        return 1.0
    try:
        from tkinter import font as tkfont
        system = abs(int(tkfont.nametofont("TkDefaultFont").actual("size"))) or 13
    except Exception:  # noqa: BLE001
        system = 13
    return max(1.0, system / 10)  # our body text is 10 here -> the Mac's system size there


def F(size: int, weight: str = "normal"):
    """Shared font at `size` (Windows points), following the platform and the Text size setting."""
    from tkinter import font as tkfont
    key = (size, weight)
    f = _fonts.get(key)
    if f is None:
        f = tkfont.Font(family=FONT, size=_scaled(size), weight=weight)
        _fonts[key] = f
    return f


def _scaled(size: int) -> int:
    return max(7, round(size * _text["platform"] * _text["user"]))


def px(n: int) -> int:
    """A layout width (wrap length, fixed panel size) that grows with the Text size setting."""
    return round(n * _text["user"])


def set_text_scale(scale: float) -> None:
    _text["user"] = scale
    for (size, _w), f in _fonts.items():
        f.configure(size=_scaled(size))
MODE_BG = {"live": AQUA, "loop": VIOLET, "glitch": AMBER}
MODE_FG = {"live": MIDNIGHT, "loop": CLOUD, "glitch": MIDNIGHT}


def _photo(path: str, width: int | None = None, bg: str = MIDNIGHT) -> ImageTk.PhotoImage:
    """Load a brand image, scaled, flattened onto `bg` (crisp edges on every Tk build)."""
    img = Image.open(path).convert("RGBA")
    if width:
        img = img.resize((width, round(width * img.height / img.width)), Image.LANCZOS)
    flat = Image.new("RGBA", img.size, bg)
    flat.alpha_composite(img)
    return ImageTk.PhotoImage(flat.convert("RGB"))


def apply_theme(root: tk.Misc) -> None:
    """Our own ttk theme in the brand palette (built on 'clam' so every colour is ours)."""
    root.configure(bg=MIDNIGHT)
    _fonts.clear()  # fonts belong to one Tk root; a new root gets new ones
    _text["platform"] = _platform_factor()
    s = ttk.Style(root)
    s.theme_use("clam")
    base = F(10)
    s.configure(".", background=MIDNIGHT, foreground=CLOUD, fieldbackground=SURFACE, bordercolor=BORDER,
                lightcolor=SURFACE, darkcolor=SURFACE, troughcolor=SURFACE, focuscolor=VIOLET,
                selectbackground=VIOLET, selectforeground=CLOUD, font=base, insertcolor=CLOUD)
    s.configure("TFrame", background=MIDNIGHT)
    s.configure("TLabel", background=MIDNIGHT, foreground=CLOUD)
    s.configure("Small.TLabel", font=F(9), foreground=MUTED)
    s.configure("Head.TLabel", font=F(11, "bold"))
    s.configure("Title.TLabel", font=F(17, "bold"))
    s.configure("Tag.TLabel", font=F(10), foreground=MUTED)
    s.configure("TButton", background=SURFACE, foreground=CLOUD, bordercolor=BORDER, padding=(10, 6),
                lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=SURFACE)
    s.map("TButton", background=[("pressed", BORDER), ("active", SURFACE_HI), ("disabled", MIDNIGHT)],
          foreground=[("disabled", MUTED)])
    s.configure("Tool.TButton", background=SURFACE, foreground=CLOUD, bordercolor=SURFACE, padding=(10, 4),
                lightcolor=SURFACE, darkcolor=SURFACE, focuscolor=SURFACE)
    s.map("Tool.TButton", background=[("pressed", BORDER), ("active", SURFACE_HI)])
    s.configure("Tool.TMenubutton", background=SURFACE, foreground=CLOUD, bordercolor=SURFACE, padding=(10, 4),
                lightcolor=SURFACE, darkcolor=SURFACE, arrowcolor=CLOUD, relief="flat")
    s.map("Tool.TMenubutton", background=[("pressed", BORDER), ("active", SURFACE_HI)])
    s.configure("Accent.TButton", background=VIOLET, foreground=CLOUD, bordercolor=VIOLET,
                lightcolor=VIOLET, darkcolor=VIOLET, focuscolor=VIOLET)
    s.map("Accent.TButton", background=[("pressed", VIOLET_DARK), ("active", "#8B5CF6")])
    s.configure("TCheckbutton", background=MIDNIGHT, foreground=CLOUD, indicatorbackground=SURFACE,
                indicatorforeground=CLOUD, indicatorcolor=SURFACE, focuscolor=MIDNIGHT)
    s.map("TCheckbutton", indicatorcolor=[("selected", VIOLET)], background=[("active", MIDNIGHT)])
    s.configure("TCombobox", fieldbackground=SURFACE, background=SURFACE, foreground=CLOUD, arrowcolor=CLOUD,
                bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE, padding=(8, 5))
    s.map("TCombobox", fieldbackground=[("readonly", SURFACE)], foreground=[("readonly", CLOUD)],
          selectbackground=[("readonly", SURFACE)], selectforeground=[("readonly", CLOUD)])
    s.configure("Vertical.TScrollbar", background=SURFACE_HI, troughcolor=MIDNIGHT, bordercolor=MIDNIGHT,
                arrowcolor=CLOUD, lightcolor=SURFACE_HI, darkcolor=SURFACE_HI, gripcount=0)
    s.map("Vertical.TScrollbar", background=[("active", VIOLET), ("pressed", VIOLET)])
    for st in ("TEntry", "TSpinbox"):
        s.configure(st, fieldbackground=SURFACE, background=SURFACE, foreground=CLOUD, insertcolor=CLOUD,
                    bordercolor=BORDER, lightcolor=SURFACE, darkcolor=SURFACE, arrowcolor=CLOUD, padding=(6, 4))
        s.map(st, fieldbackground=[("disabled", MIDNIGHT)], foreground=[("disabled", MUTED)])
    root.option_add("*TCombobox*Listbox.background", SURFACE)
    root.option_add("*TCombobox*Listbox.foreground", CLOUD)
    root.option_add("*TCombobox*Listbox.selectBackground", VIOLET)
    root.option_add("*TCombobox*Listbox.font", base)
    s.configure("Sync.Horizontal.TProgressbar", troughcolor=SURFACE, background=VIOLET, bordercolor=SURFACE,
                lightcolor=VIOLET, darkcolor=VIOLET, thickness=8)
    s.configure("InSync.Horizontal.TProgressbar", troughcolor=SURFACE, background=AQUA, bordercolor=SURFACE,
                lightcolor=AQUA, darkcolor=AQUA, thickness=8)
    s.configure("Treeview", background=SURFACE, fieldbackground=SURFACE, foreground=CLOUD, bordercolor=BORDER,
                lightcolor=SURFACE, darkcolor=SURFACE, rowheight=62)
    s.map("Treeview", background=[("selected", SURFACE_HI)], foreground=[("selected", CLOUD)])
    s.configure("TScale", background=VIOLET, troughcolor=SURFACE, bordercolor=SURFACE,
                lightcolor=VIOLET, darkcolor=VIOLET)
    s.configure("TSeparator", background=BORDER)
    s.configure("TLabelframe", background=MIDNIGHT, bordercolor=BORDER, lightcolor=MIDNIGHT, darkcolor=MIDNIGHT)
    s.configure("TLabelframe.Label", background=MIDNIGHT, foreground=MUTED, font=F(9, "bold"))
    s.configure("Vertical.TScrollbar", background=SURFACE, troughcolor=MIDNIGHT, bordercolor=MIDNIGHT,
                arrowcolor=MUTED, lightcolor=SURFACE, darkcolor=SURFACE)


class HoldButton(tk.Label):
    """Big cut button: press/release go to the engine (tap = cut now, hold = cut when in sync).
    It wears the colour of where you're cutting TO: Violet = to the loop, Aqua = to live."""

    def __init__(self, master, engine, font_size=15, **kw):
        super().__init__(master, text="CUT", fg=CLOUD, bg=VIOLET, cursor="hand2",
                         font=F(font_size, "bold"), padx=10, pady=12, **kw)
        self.e = engine
        self.bind("<ButtonPress-1>", lambda _e: self.e.request("cut_press"))
        self.bind("<ButtonRelease-1>", lambda _e: self.e.request("cut_release"))

    def refresh(self, short: bool = False) -> None:
        e = self.e
        if e.recorder.state == State.PROCESSING:
            pct = int(e.recorder.progress * 100)
            text, bg, fg = (f"STITCHING {pct}%" if short else f"STITCHING NEW LOOP...  {pct}%"), SURFACE, MUTED
        elif e.cut.waiting:
            text = f"SYNC {int(e.sync * 100)}%" if short else f"WAITING FOR SYNC  {int(e.sync * 100)}%"
            bg, fg = SURFACE_HI, AQUA
        elif e.mode == "loop":
            text, bg, fg = "CUT TO LIVE", AQUA, MIDNIGHT
        else:
            ready = e.player is not None
            text, bg, fg = "CUT TO LOOP", (VIOLET if ready else SURFACE), (CLOUD if ready else MUTED)
        if self.cget("text") != text:
            self.configure(text=text)
        if self.cget("bg") != bg or self.cget("fg") != fg:
            self.configure(bg=bg, fg=fg)


class MainWindow:
    def __init__(self, engine, minimized: bool = False):
        self.e = engine
        self.cfg = engine.cfg
        self._start_minimized = minimized
        self.root = tk.Tk()
        self.root.title("Doppleganger")
        self.root.geometry("1280x760")
        self.root.minsize(980, 620)
        try:
            self.root.iconbitmap(default=brand.asset("icon.ico"))
        except tk.TclError:
            pass
        self._icon = ImageTk.PhotoImage(Image.open(brand.asset("icon.png")).resize((64, 64), Image.LANCZOS))
        self.root.iconphoto(True, self._icon)
        apply_theme(self.root)
        set_text_scale(self.cfg.text_scale)
        self._photo = None
        self._last_preview_id = None
        self._lib_version = -1
        self._thumbs: dict[str, ImageTk.PhotoImage] = {}
        self._pending_release = None
        self._engine_thread: threading.Thread | None = None
        self.minibar: MiniBar | None = None
        self.wizard: Wizard | None = None
        self.settings: SettingsWindow | None = None
        self.busy: BusyDialog | None = None
        self.tips: RecordTipsDialog | None = None
        self.license_dlg: LicenseDialog | None = None
        self.eula_dlg: EulaDialog | None = None
        engine.ui_attached = True  # Record shows the tips first
        # Mac: is OBS Studio + its virtual camera installed? (checked in the background, every few seconds)
        self.obs = {"obs": True, "camera": "unknown", "ready": True, "message": ""}
        if IS_MAC:
            self._watch_obs()
        self._build()
        self._no_button_focus(self.root)
        self._bind_keys()
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---------- layout ----------
    def _build(self) -> None:
        r = self.root
        r.columnconfigure(0, weight=1)
        r.rowconfigure(1, weight=1)

        # --- toolbar: Settings · Setup Guide · View (Normal / Mini Bar) ---
        bar = tk.Frame(r, bg=SURFACE, padx=10, pady=4)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew")
        for text, cmd in (("Settings", self.open_settings), ("Setup Guide", self.open_wizard)):
            ttk.Button(bar, text=text, style="Tool.TButton", command=cmd).pack(side="left", padx=(0, 4))
        view = ttk.Menubutton(bar, text="View", style="Tool.TMenubutton", width=6)
        self.view_menu = tk.Menu(view, tearoff=0, bg=SURFACE, fg=CLOUD, activebackground=VIOLET,
                                 activeforeground=CLOUD, font=F(10))
        self.v_view = tk.StringVar(value="normal")
        self.view_menu.add_radiobutton(label="Normal", value="normal", variable=self.v_view, command=self.show_main)
        self.view_menu.add_radiobutton(label="Mini Bar", value="mini", variable=self.v_view, command=self.show_minibar)
        self.view_menu.add_separator()
        self.view_menu.add_command(label="Larger text", command=lambda: self.change_text_size(1),
                                   accelerator=("Cmd" if IS_MAC else "Ctrl") + "++")
        self.view_menu.add_command(label="Smaller text", command=lambda: self.change_text_size(-1),
                                   accelerator=("Cmd" if IS_MAC else "Ctrl") + "+-")
        view["menu"] = self.view_menu
        view.pack(side="left", padx=(0, 4))
        tk.Label(bar, text=f"v{updater.current_version()}", bg=SURFACE, fg=MUTED, font=F(9)).pack(side="right")

        self.preview = tk.Label(r, bg=MIDNIGHT, bd=0, highlightthickness=0)
        self.preview.grid(row=1, column=0, sticky="nsew", padx=(14, 0), pady=14)
        self.preview.bind("<Configure>", self._on_preview_resize)

        # the side panel scrolls when the window is too short for it (a scroll bar appears only then)
        holder = tk.Frame(r, bg=MIDNIGHT)
        holder.grid(row=1, column=1, sticky="ns")
        holder.rowconfigure(0, weight=1)
        self._side_canvas = tk.Canvas(holder, bg=MIDNIGHT, highlightthickness=0, bd=0, width=px(352))
        self._side_canvas.grid(row=0, column=0, sticky="ns")
        self._side_scroll = ttk.Scrollbar(holder, orient="vertical", command=self._side_canvas.yview)
        self._side_canvas.configure(yscrollcommand=self._side_scroll.set)
        side = ttk.Frame(self._side_canvas, padding=(16, 12, 16, 8))
        self._side_win = self._side_canvas.create_window(0, 0, window=side, anchor="nw")
        side.bind("<Configure>", lambda _e: self._fit_side())
        self._side_canvas.bind("<Configure>", lambda _e: self._fit_side())
        for w in (self._side_canvas, side):
            w.bind("<Enter>", lambda _e: self._wheel(True))
            w.bind("<Leave>", lambda _e: self._wheel(False))
        side.columnconfigure(0, weight=1)
        self._side = side

        self._logo = _photo(brand.asset("logo_dark.png"), 250)
        tk.Label(side, image=self._logo, bg=MIDNIGHT, bd=0).grid(sticky="w", pady=(0, 12))

        # --- status ---
        top = ttk.Frame(side)
        top.grid(sticky="ew")
        self.state_lbl = tk.Label(top, text="LIVE", fg=MIDNIGHT, bg=AQUA, font=F(15, "bold"), width=7, pady=3)
        self.state_lbl.pack(side="left")
        self.glitch_lbl = tk.Label(top, text="GLITCH", fg=MIDNIGHT, bg=AMBER, font=F(10, "bold"),
                                   padx=8, pady=6)
        self.out_lbl = ttk.Label(side, text="", style="Small.TLabel", wraplength=px(300), justify="left")
        self.out_lbl.grid(sticky="w", pady=(6, 0))
        # license: free LOOP time left / countdown, and Get License Key until a key is applied
        licrow = ttk.Frame(side)
        licrow.grid(sticky="ew", pady=(6, 0))
        self.lic_lbl = tk.Label(licrow, text="", bg=MIDNIGHT, fg=MUTED, font=F(9), anchor="w", justify="left")
        self.lic_lbl.pack(side="left")
        self.lic_btn = ttk.Button(licrow, text="Get License Key", style="Accent.TButton", command=self.open_license)
        # Mac only, while OBS or its camera is missing (beside the LIVE badge, so the panel doesn't grow)
        self.obs_btn = ttk.Button(top, text="Get OBS Studio (free)", style="Accent.TButton",
                                  command=self.open_obs_help)

        syncrow = ttk.Frame(side)
        syncrow.grid(sticky="ew", pady=(10, 0))
        syncrow.columnconfigure(1, weight=1)
        ttk.Label(syncrow, text="Sync", style="Small.TLabel").grid(row=0, column=0, padx=(0, 8))
        self.sync_bar = ttk.Progressbar(syncrow, maximum=100, style="Sync.Horizontal.TProgressbar")
        self.sync_bar.grid(row=0, column=1, sticky="ew")
        self.sync_pct = ttk.Label(syncrow, text="0%", width=5, anchor="e", style="Small.TLabel")
        self.sync_pct.grid(row=0, column=2)
        self.v_outlines = tk.BooleanVar(value=self.cfg.show_outlines)
        ttk.Checkbutton(side, text="Outlines: you (aqua) · loop (violet)   O", variable=self.v_outlines,
                        command=lambda: self.e.request("outlines")).grid(sticky="w", pady=(6, 0))

        # --- cut ---
        self.cut_btn = HoldButton(side, self.e)
        self.cut_btn.grid(sticky="ew", pady=(12, 3))
        hk = ("fn+" if IS_MAC else "") + self.cfg.hotkey.strip("<>").upper()
        ttk.Label(side, text=f"Tap: cut now  ·  Hold: cut when in sync  ({hk} / Space)",
                  style="Small.TLabel").grid(sticky="w")

        row = ttk.Frame(side)
        row.grid(sticky="ew", pady=(10, 0))
        for i in range(3):
            row.columnconfigure(i, weight=1)
        gk = self.cfg.glitch_hotkey.strip("<>").upper()
        ttk.Button(row, text=f"Glitch ({gk})", command=lambda: self.e.request("glitch")).grid(
            row=0, column=0, sticky="ew", padx=(0, 4))
        self.rec_btn = ttk.Button(row, text="Record loop (R)", command=lambda: self.e.request("record"))
        self.rec_btn.grid(row=0, column=1, sticky="ew", padx=4)
        if self.e.seg is None:  # AI segmentation unavailable: calibration is needed, keep it handy
            ttk.Button(row, text="Calibrate", command=lambda: self.e.request("calibrate")).grid(
                row=0, column=2, sticky="ew", padx=(4, 0))
        else:
            row.columnconfigure(2, weight=0)

        ttk.Separator(side).grid(sticky="ew", pady=12)

        # --- webcam ---
        ttk.Label(side, text="Webcam", style="Head.TLabel").grid(sticky="w")
        self.cam_var = tk.StringVar(value=self.e.camera_name)
        self.cam_box = ttk.Combobox(side, textvariable=self.cam_var, state="readonly",
                                    postcommand=self._refresh_cameras)
        self.cam_box.grid(sticky="ew", pady=(4, 0))
        self.cam_box.bind("<<ComboboxSelected>>", lambda _e: self.e.request(f"camera:{self.cam_var.get()}"))

        # --- loops ---
        lh = ttk.Frame(side)
        lh.grid(sticky="ew", pady=(12, 4))
        ttk.Label(lh, text="Loops", style="Head.TLabel").pack(side="left")
        self.light_lbl = ttk.Label(lh, text="", style="Small.TLabel", foreground=AMBER)
        self.light_lbl.pack(side="right")
        self.tree = ttk.Treeview(side, show="tree", height=3, selectmode="browse")
        self.tree.column("#0", width=300)
        self.tree.grid(sticky="ew")
        self.tree.bind("<Double-1>", lambda _e: self._use_selected())
        lb = ttk.Frame(side)
        lb.grid(sticky="ew", pady=(4, 0))
        for i in range(3):
            lb.columnconfigure(i, weight=1)
        ttk.Button(lb, text="Use", command=self._use_selected).grid(row=0, column=0, sticky="ew", padx=(0, 4))
        ttk.Button(lb, text="Rename", command=self._rename_selected).grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(lb, text="Delete", command=self._delete_selected).grid(row=0, column=2, sticky="ew", padx=(4, 0))

        ttk.Label(side, text=brand.TAGLINE, style="Small.TLabel").grid(sticky="e", pady=(12, 0))

        self.status = tk.Label(r, text="", anchor="w", padx=14, pady=5, bg=SURFACE, fg=CLOUD, font=F(9))
        self.status.grid(row=2, column=0, columnspan=2, sticky="ew")

    # ---------- side panel scrolling ----------
    def _fit_side(self) -> None:
        c, side = self._side_canvas, self._side
        need_w = side.winfo_reqwidth()
        if int(c.cget("width")) != need_w:
            c.configure(width=need_w)
        c.configure(scrollregion=(0, 0, need_w, side.winfo_reqheight()))
        fits = side.winfo_reqheight() <= c.winfo_height() + 1
        if fits and self._side_scroll.winfo_ismapped():
            self._side_scroll.grid_remove()
            c.yview_moveto(0)
        elif not fits and not self._side_scroll.winfo_ismapped():
            self._side_scroll.grid(row=0, column=1, sticky="ns")

    def _wheel(self, on: bool) -> None:
        if on:
            self.root.bind_all("<MouseWheel>", self._on_wheel)
            self.root.bind_all("<Button-4>", lambda _e: self._side_canvas.yview_scroll(-2, "units"))
            self.root.bind_all("<Button-5>", lambda _e: self._side_canvas.yview_scroll(2, "units"))
        else:
            for seq in ("<MouseWheel>", "<Button-4>", "<Button-5>"):
                self.root.unbind_all(seq)

    def _on_wheel(self, ev) -> None:
        if self._side_scroll.winfo_ismapped():
            step = -int(ev.delta / 120) if abs(ev.delta) >= 120 else (-1 if ev.delta > 0 else 1)
            self._side_canvas.yview_scroll(step * 2, "units")

    @staticmethod
    def _no_button_focus(widget) -> None:
        """Buttons don't keep keyboard focus after a click, so keys always go to the app's shortcuts."""
        for w in widget.winfo_children():
            if isinstance(w, (ttk.Button, ttk.Checkbutton, tk.Button, tk.Checkbutton, ttk.Scale)):
                w.configure(takefocus=0)
            MainWindow._no_button_focus(w)

    # ---------- keys (hold-capable Space) ----------
    def _bind_keys(self) -> None:
        mod = "Command" if IS_MAC else "Control"
        for seq, step in (("plus", 1), ("equal", 1), ("KP_Add", 1), ("minus", -1), ("KP_Subtract", -1), ("0", 0)):
            self.root.bind_all(f"<{mod}-{seq}>", lambda _e, st=step: self.change_text_size(st))
        r = self.root
        # Space means CUT everywhere. Tk buttons/checkboxes normally also "press" themselves on
        # Space when focused (e.g. Record right after you clicked it) - switch that off.
        for cls in ("TButton", "TCheckbutton", "TRadiobutton", "Button", "Checkbutton", "Radiobutton",
                    "TCombobox", "Treeview", "TScale"):
            r.bind_class(cls, "<KeyPress-space>", lambda _e: None)
            r.bind_class(cls, "<KeyRelease-space>", lambda _e: None)
        r.bind_all("<KeyPress-space>", self._space_down)
        r.bind_all("<KeyRelease-space>", self._space_up)
        keys = {"r": "record", "b": "glitch", "c": "calibrate", "e": "end_recording", "u": "update", "k": "camera:"}
        for k, action in keys.items():
            for kk in (k, k.upper()):
                r.bind_all(f"<KeyPress-{kk}>", lambda _e, a=action: self._key_action(a))
        r.bind_all("<Escape>", lambda _e: self.e.request("cancel"))
        for kk in ("o", "O"):
            r.bind_all(f"<KeyPress-{kk}>", lambda _e: self._key_action("outlines"))
        for kk in ("m", "M"):
            r.bind_all(f"<KeyPress-{kk}>", lambda _e: None if self._typing() else self.show_minibar())
        for kk in ("g", "G"):
            r.bind_all(f"<KeyPress-{kk}>", lambda _e: None if self._typing() else self.toggle_guides())

    def _typing(self) -> bool:
        w = self.root.focus_get()
        return isinstance(w, (tk.Entry, ttk.Entry)) and not isinstance(w, ttk.Combobox)

    def _key_action(self, action: str) -> None:
        if not self._typing():
            self.e.request(action)

    def _space_down(self, _e=None):
        if self._typing():
            return None
        if self._pending_release is not None:  # auto-repeat: that "release" was fake
            self.root.after_cancel(self._pending_release)
            self._pending_release = None
            return "break"
        self.e.request("cut_press")
        return "break"

    def _space_up(self, _e=None):
        if self._typing():
            return None
        # Wait a moment: on some systems key auto-repeat sends release+press pairs.
        self._pending_release = self.root.after(40, self._space_released)
        return "break"

    def _space_released(self):
        self._pending_release = None
        self.e.request("cut_release")

    # ---------- actions ----------
    def toggle_guides(self):
        self.cfg.show_guides = not self.cfg.show_guides
        self.cfg.save(self.e.config_path)

    def apply_capture_setting(self) -> None:
        hide = self.cfg.hide_from_screen_share
        for w in (self.root, self.minibar and self.minibar.win, self.wizard and self.wizard.win,
                  self.settings and self.settings.win, self.license_dlg and self.license_dlg.alive and self.license_dlg.win):
            if w is not None:
                try:
                    winutil.hide_from_capture(w, hide)
                except Exception:  # noqa: BLE001
                    pass

    def _refresh_cameras(self) -> None:
        self.cam_box["values"] = self.e.tray_cameras()

    def _selected_loop(self) -> str | None:
        sel = self.tree.selection()
        return sel[0] if sel else None

    def _use_selected(self) -> None:
        p = self._selected_loop()
        if p:
            self.e.request(f"use_loop:{p}")

    def _rename_selected(self) -> None:
        p = self._selected_loop()
        if not p:
            return
        name = simpledialog.askstring("Rename loop", "Name:", initialvalue=library.info(p).name, parent=self.root)
        if name is not None:
            library.rename(p, name)
            self.e.request("library_changed")

    def _delete_selected(self) -> None:
        p = self._selected_loop()
        if p and messagebox.askyesno("Delete loop", f"Delete \"{library.info(p).name}\"? This can't be undone.",
                                     parent=self.root):
            self.e.request(f"delete_loop:{p}")

    def _refresh_library(self) -> None:
        self.tree.delete(*self.tree.get_children())
        for li in library.list_loops(self.cfg.loops_dir):
            if li.path not in self._thumbs:
                th = library.thumbnail(li.path, 96)
                if th is not None:
                    self._thumbs[li.path] = ImageTk.PhotoImage(Image.fromarray(cv2.cvtColor(th, cv2.COLOR_BGR2RGB)))
            active = li.path == self.e.loop_path
            warn = library.lighting_warning(li.lighting, self.e.live_light)
            details = [f"{int(li.seconds)} s"] + (["in use"] if active else [])
            if warn:
                details.append(f"⚠ {library.lighting_short(warn)}")
            label = f"  {li.name}\n  " + "  ·  ".join(details)
            self.tree.insert("", "end", iid=li.path, text=label, image=self._thumbs.get(li.path, ""))
            if active:
                self.tree.selection_set(li.path)

    # ---------- refresh loop ----------
    def _on_preview_resize(self, ev) -> None:
        w, h = ev.width, ev.height
        pw = min(w, int(h * 16 / 9))
        self.e.preview_size = (max(64, pw - pw % 2), max(36, int(pw * 9 / 16) // 2 * 2))

    def tick(self) -> None:
        e = self.e
        if self._engine_thread is not None and not self._engine_thread.is_alive():
            self.root.destroy()
            return
        rgb = e.preview_rgb
        if rgb is not None and id(rgb) != self._last_preview_id:
            self._last_preview_id = id(rgb)
            if self.root.state() != "withdrawn":
                self._photo = ImageTk.PhotoImage(Image.fromarray(rgb))
                self.preview.configure(image=self._photo)
            if self.minibar is not None and self.minibar.visible:
                self.minibar.refresh(rgb)
        self._refresh_status()
        self._refresh_busy()
        self._refresh_tips()
        if self.wizard is not None:
            self.wizard.refresh()
        if self.settings is not None:
            self.settings.refresh()
        self.root.after(33, self.tick)

    def _refresh_status(self) -> None:
        e = self.e
        mode = e.mode
        if e.recorder.state == State.RECORDING:   # recording: a red REC badge with the time, hard to miss
            el, tot = int(e.recorder.elapsed), int(e.cfg.loop_seconds)
            self.state_lbl.configure(text=f"● REC {el // 60}:{el % 60:02d} / {tot // 60}:{tot % 60:02d}",
                                     bg=DANGER, fg=MIDNIGHT, width=0)
        elif e.recorder.state == State.COUNTDOWN:
            self.state_lbl.configure(text="● REC starting...", bg=DANGER, fg=MIDNIGHT, width=0)
        else:
            self.state_lbl.configure(text=mode.upper(), bg=MODE_BG[mode], fg=MODE_FG[mode], width=7)
        if e.glitch.active and not self.glitch_lbl.winfo_ismapped():
            self.glitch_lbl.pack(side="left", padx=8)
        elif not e.glitch.active and self.glitch_lbl.winfo_ismapped():
            self.glitch_lbl.pack_forget()
        pct = int(e.sync * 100)
        insync = e.sync >= SYNC_THRESHOLD
        self.sync_bar.configure(value=pct, style="InSync.Horizontal.TProgressbar" if insync
                                else "Sync.Horizontal.TProgressbar")
        self.sync_pct.configure(text=f"{pct}%", foreground=AQUA if insync else MUTED)
        self.cut_btn.refresh()
        o = e.output
        if o.connected:
            name = "HappyHacks Virtual Camera" if o.backend == "doppleganger" else "OBS Virtual Camera"
            self.out_lbl.configure(text=f"● Sending to {name}" + ("  ·  in use" if o.viewer_connected else ""),
                                   foreground=AQUA)
        else:
            self.out_lbl.configure(text=f"● Not sending to your meeting: {o.error}", foreground=DANGER)
        if IS_MAC:
            need = not self.obs["ready"] and not o.connected
            if need:
                self.obs_btn.configure(text="Get OBS Studio (free)" if not self.obs["obs"] else "Finish OBS setup")
                if not self.obs_btn.winfo_ismapped():
                    self.obs_btn.pack(side="right")
            elif self.obs_btn.winfo_ismapped():
                self.obs_btn.pack_forget()
        rec = e.recorder.state
        self.rec_btn.configure(text="Cancel recording (R)" if e.recording_active else
                               ("Stitching..." if rec == State.PROCESSING else "Record loop (R)"))
        if self.v_outlines.get() != self.cfg.show_outlines:
            self.v_outlines.set(self.cfg.show_outlines)
        self._refresh_license()
        warn = e.lighting_warning
        self.light_lbl.configure(text=f"⚠ {warn}" if warn else "")
        if e.library_version != self._lib_version:
            self._lib_version = e.library_version
            self._refresh_library()
        if self.cam_var.get() != e.camera_name:
            self.cam_var.set(e.camera_name)
        msg = e.message
        if e.mic.is_muted:
            msg = (msg + "   ·   " if msg else "") + "Microphone muted while the LOOP is on air"
        if IS_MAC and getattr(e, "_hk", None) is None and not msg:
            msg = "Hotkeys are off: allow Input Monitoring (Setup guide > Allow Doppleganger on your Mac)"
        self.status.configure(text=msg)

    def _refresh_busy(self) -> None:
        """While a new loop is being stitched: a small modal over the window, so nothing gets cut meanwhile."""
        busy = self.e.recorder.state == State.PROCESSING
        if busy and self.busy is None:
            self.busy = BusyDialog(self.minibar.win if (self.minibar and self.minibar.visible) else self.root)
        if self.busy is not None:
            if busy:
                self.busy.update(self.e.recorder.progress)
            else:
                self.busy.close()
                self.busy = None

    def _watch_obs(self) -> None:
        def loop():
            import time as _time
            while True:
                try:
                    self.obs = macos.obs_status()
                except Exception:  # noqa: BLE001
                    pass
                _time.sleep(3.0)
        threading.Thread(target=loop, daemon=True, name="obs-check").start()

    def open_obs_help(self) -> None:
        """Mac: OBS missing -> its download page; installed but camera not set up -> the setup steps."""
        if not self.obs["obs"]:
            macos.open_obs_download()
        else:
            self.open_wizard(page="camera")

    def _refresh_license(self) -> None:
        lic = self.e.lic
        left = lic.remaining()
        on_air = self.e.mode == "loop"
        if left is not None and left <= WARN_AT and (on_air or left <= 0.5):
            text = (f"⏱ {format_left(left)} of free LOOP time left" if left > 0.5 else
                    "Free LOOP time used up - get a license key for unlimited use")
            self.lic_lbl.configure(text=text, fg=DANGER if left <= 30 else AMBER, font=F(10, "bold"))
        else:
            self.lic_lbl.configure(text=lic.summary(), fg=AQUA if lic.unlimited else MUTED, font=F(9))
        if lic.unlimited and self.lic_btn.winfo_ismapped():
            self.lic_btn.pack_forget()
        elif not lic.unlimited and lic.known and not self.lic_btn.winfo_ismapped():
            self.lic_btn.pack(side="right")

    def open_license(self) -> None:
        if self.license_dlg is None or not self.license_dlg.alive:
            self.license_dlg = LicenseDialog(self)
        self.license_dlg.win.lift()

    def change_text_size(self, step: int) -> None:
        """step +1 / -1 = one size larger / smaller, 0 = back to normal."""
        cur = min(TEXT_SCALES, key=lambda v: abs(v - self.cfg.text_scale))
        i = TEXT_SCALES.index(cur)
        new = 1.0 if step == 0 else TEXT_SCALES[max(0, min(len(TEXT_SCALES) - 1, i + step))]
        if new != self.cfg.text_scale:
            self.cfg.text_scale = new
            self.cfg.save(self.e.config_path)
            set_text_scale(new)
        self.e.notify(f"Text size {int(round(new * 100))}%", 2)
        if self.settings is not None and self.settings.alive:
            self.settings.refresh()

    def _refresh_tips(self) -> None:
        """'How to record a good loop' before each recording (until turned off)."""
        want = self.e.tips_pending
        if want and self.tips is None:
            parent = self.minibar.win if (self.minibar and self.minibar.visible) else self.root
            if parent is self.root and self.root.state() == "withdrawn":
                self.show_main()
            self.tips = RecordTipsDialog(self, parent)
        elif not want and self.tips is not None:
            self.tips.close()
            self.tips = None

    # ---------- other windows ----------
    def show_minibar(self) -> None:
        if self.minibar is None:
            self.minibar = MiniBar(self)
        self.minibar.show()
        self.root.withdraw()
        self.v_view.set("mini")
        self.cfg.mini_bar = True
        self.cfg.save(self.e.config_path)

    def show_main(self) -> None:
        self.v_view.set("normal")
        if self.minibar is not None:
            self.minibar.hide()
        self.root.deiconify()
        self.root.lift()
        self.cfg.mini_bar = False
        self.cfg.save(self.e.config_path)
        self.root.after(150, self.apply_capture_setting)

    def open_wizard(self, page: str | None = None) -> None:
        if self.wizard is None or not self.wizard.alive:
            self.wizard = Wizard(self)
        if page and page in self.wizard.STEPS:
            self.wizard.step = self.wizard.STEPS.index(page)
            self.wizard.render()
        self.wizard.win.lift()

    def open_settings(self) -> None:
        if self.settings is None or not self.settings.alive:
            self.settings = SettingsWindow(self)
        self.settings.win.lift()

    # ---------- lifecycle ----------
    def on_close(self, force: bool = False) -> None:
        if not force and self.e.mode == "loop" and not messagebox.askyesno(
                "Quit Doppleganger?", "The LOOP is on air. Quitting stops the picture your meeting sees.\n\nQuit?",
                parent=self.root):
            return
        self.e.stop()

    def run(self) -> None:
        if IS_MAC:
            try:
                self.root.createcommand("tk::mac::Quit", self.on_close)          # Cmd+Q / Dock > Quit
                self.root.createcommand("tk::mac::ReopenApplication", self.show_main)  # click the Dock icon
                self.root.createcommand("tk::mac::ShowPreferences", self.open_settings)  # Cmd+,
            except tk.TclError:
                pass
        self._engine_thread = threading.Thread(target=self.e.run, daemon=True, name="engine")
        self._engine_thread.start()
        self.root.after(200, self.apply_capture_setting)
        self.root.after(33, self.tick)
        if self._start_minimized:
            self.root.after(300, self.root.iconify)  # started with Windows: stay out of the way
        elif self.cfg.mini_bar:
            self.root.after(300, self.show_minibar)
        if self.cfg.eula_accepted != EULA_VERSION:
            if self._start_minimized:
                self.root.after(350, self.root.deiconify)   # the agreement needs the window
            self.root.after(500, lambda: setattr(self, "eula_dlg", EulaDialog(self)))
        else:
            self.root.after(0, self.after_eula)
        self.root.mainloop()
        self.e.stop()
        self._engine_thread.join(timeout=5)

    def after_eula(self) -> None:
        """What happens once the license agreement is accepted: the setup guide or the Mac permissions page."""
        if not self.cfg.setup_done:
            camera_ok = macos.obs_status()["ready"] if IS_MAC else self.e.tray_driver_installed()
            if self.e.player is not None and camera_ok:
                self.cfg.setup_done = True  # already set up with an earlier version
                self.cfg.save(self.e.config_path)
            else:
                self.root.after(600, self.open_wizard)
        elif IS_MAC and not self.cfg.mac_permissions_seen:
            # set up with an earlier version: walk through the Mac permissions once
            self.root.after(800, lambda: self.open_wizard(page="permissions"))


RECORD_TIPS = (
    ("Sit the way you will in the meeting",
     "Same chair, distance and framing. Look at the screen, not the camera lens."),
    ("Hold still for the first 10 seconds",
     "This pose is where the loop starts and ends. The countdown tells you when you're free to move."),
    ("Keep movement subtle and natural",
     "Blink, breathe, nod slightly, glance at your screen now and then. A little movement looks alive; "
     "a frozen face looks fake."),
    ("Avoid anything memorable",
     "No big laughs, waves, face-touching, drinking, looking away or adjusting hair or glasses. "
     "The loop repeats, so a distinctive moment gets noticed the second time."),
    ("Come back to the start for the last 10 seconds",
     "You'll be prompted. Match the outline, same expression, and hold still until it finishes."),
    ("Keep the room the same",
     "Steady lighting, nobody walking behind you. Ideally record right before the meeting."),
)


def center_on(win: tk.Misc, parent: tk.Misc | None, top_third: bool = False) -> None:
    """Put a dialog in the middle of the app window (or the screen), fully on screen."""
    win.update_idletasks()
    w, h = win.winfo_reqwidth(), win.winfo_reqheight()
    sw, sh = win.winfo_screenwidth(), win.winfo_screenheight()
    try:
        visible = parent is not None and parent.winfo_viewable()
    except tk.TclError:
        visible = False
    if visible:
        px_, py_ = parent.winfo_rootx(), parent.winfo_rooty()
        pw, ph = max(parent.winfo_width(), 1), max(parent.winfo_height(), 1)
    else:
        px_, py_, pw, ph = 0, 0, sw, sh
    x = px_ + (pw - w) // 2
    y = py_ + ((ph - h) // 4 if top_third else (ph - h) // 2)
    x = min(max(0, x), max(0, sw - w))
    y = min(max(0, y), max(0, sh - h - 48))   # keep clear of the taskbar
    win.geometry(f"+{x}+{y}")


class ScrollArea(tk.Frame):
    """A frame whose content scrolls when it's taller than max_height (a scroll bar appears only then)."""

    def __init__(self, parent, max_height: int):
        super().__init__(parent, bg=MIDNIGHT)
        self.max_height = max_height
        self.canvas = tk.Canvas(self, bg=MIDNIGHT, highlightthickness=0, bd=0)
        self.canvas.pack(side="left", fill="both", expand=True)
        self.bar = ttk.Scrollbar(self, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.bar.set)
        self.inner = ttk.Frame(self.canvas)
        self.canvas.create_window(0, 0, window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", lambda _e: self._fit())
        for w in (self.canvas, self.inner):
            w.bind("<Enter>", lambda _e: self.bind_all("<MouseWheel>", self._wheel))
            w.bind("<Leave>", lambda _e: self.unbind_all("<MouseWheel>"))

    def _fit(self):
        w, h = self.inner.winfo_reqwidth(), self.inner.winfo_reqheight()
        self.canvas.configure(width=w, height=min(h, self.max_height), scrollregion=(0, 0, w, h))
        if h > self.max_height and not self.bar.winfo_ismapped():
            self.bar.pack(side="right", fill="y")
        elif h <= self.max_height and self.bar.winfo_ismapped():
            self.bar.pack_forget()

    def _wheel(self, ev):
        if self.bar.winfo_ismapped():
            self.canvas.yview_scroll(-int(ev.delta / 120) * 2 if abs(ev.delta) >= 120 else (-2 if ev.delta > 0 else 2), "units")


EULA_VERSION = "1"   # bump when assets/EULA.md changes in a way people must accept again
EULA_URL = "https://doppleganger.happyhacks.io/eula"


def eula_text() -> str:
    """The bundled EULA (Markdown) as readable plain text."""
    import re as _re
    try:
        with open(brand.asset("EULA.md"), encoding="utf-8") as fh:
            md = fh.read()
    except OSError:
        return f"The license agreement is at {EULA_URL}"
    md = _re.sub(r"^#+\s*", "", md, flags=_re.M)          # headings
    md = md.replace("**", "")
    return md.strip()


class EulaDialog:
    """First run (and whenever the EULA changes): read and accept the license agreement, or quit."""

    def __init__(self, main: "MainWindow"):
        self.main = main
        self.win = tk.Toplevel(main.root)
        self.win.title("Doppleganger license agreement")
        self.win.configure(bg=MIDNIGHT)
        self.win.transient(main.root)
        self.win.protocol("WM_DELETE_WINDOW", self.decline)
        f = ttk.Frame(self.win, padding=(24, 18))
        f.pack(fill="both", expand=True)
        ttk.Label(f, text="Before you start", style="Title.TLabel").pack(anchor="w")
        ttk.Label(f, text="Doppleganger is for short, courteous breaks from video meetings - a restroom break, a coffee "
                          "refill - without distracting anyone. It's not for faking attendance or getting around "
                          "attendance checks, identity checks, exams or meetings that require your live presence. "
                          "Please read and accept the license agreement:",
                  wraplength=px(560), justify="left").pack(anchor="w", pady=(6, 10))
        box = ttk.Frame(f)
        box.pack(fill="both", expand=True)
        txt = tk.Text(box, wrap="word", width=78, height=18, bg=SURFACE, fg=CLOUD, relief="flat", padx=12, pady=10,
                      font=F(9), highlightthickness=0, insertbackground=CLOUD)
        sb = ttk.Scrollbar(box, orient="vertical", command=txt.yview)
        txt.configure(yscrollcommand=sb.set)
        txt.insert("1.0", eula_text())
        txt.configure(state="disabled")
        txt.pack(side="left", fill="both", expand=True)
        sb.pack(side="right", fill="y")
        row = ttk.Frame(f)
        row.pack(fill="x", pady=(12, 0))
        ttk.Button(row, text="Read it online", command=lambda: __import__("webbrowser").open(EULA_URL)).pack(side="left")
        ttk.Button(row, text="I Agree", style="Accent.TButton", command=self.accept).pack(side="right")
        ttk.Button(row, text="Decline", command=self.decline).pack(side="right", padx=8)
        center_on(self.win, main.root)
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        self.win.after(150, main.apply_capture_setting)

    def accept(self):
        self.main.cfg.eula_accepted = EULA_VERSION
        self.main.cfg.save(self.main.e.config_path)
        self.win.grab_release()
        self.win.destroy()
        self.main.eula_dlg = None
        self.main.after_eula()

    def decline(self):
        self.win.grab_release()
        self.win.destroy()
        self.main.e.stop()   # Doppleganger can't be used without accepting


class RecordTipsDialog:
    """Shown when Record is pressed: what to expect, so the first loop is a good one."""

    def __init__(self, main: "MainWindow", parent: tk.Misc):
        self.main = main
        self.e = main.e
        self.win = tk.Toplevel(parent)
        self.win.title("Before you record")
        self.win.resizable(False, False)
        self.win.configure(bg=MIDNIGHT)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", self.dismiss)
        f = ttk.Frame(self.win, padding=(26, 20))
        f.pack()
        ttk.Label(f, text="Before you record", style="Title.TLabel").pack(anchor="w")
        secs = int(round(main.cfg.loop_seconds))
        intro = (f"You'll record about {secs // 60}:{secs % 60:02d} of yourself. It plays on repeat, "
                 "so the goal is a calm, ordinary stretch of you paying attention.")
        ttk.Label(f, text=intro, style="Small.TLabel", wraplength=px(440), justify="left").pack(anchor="w", pady=(2, 12))
        hold = int(round(main.cfg.hold_still_seconds))
        for i, (head, body) in enumerate(RECORD_TIPS, 1):
            span = f"{hold} second" + ("" if hold == 1 else "s")
            head, body = head.replace("10 seconds", span), body.replace("10 seconds", span)
            row = ttk.Frame(f)
            row.pack(fill="x", pady=4)
            tk.Label(row, text=str(i), width=2, bg=VIOLET, fg=CLOUD, font=F(10, "bold")).pack(
                side="left", anchor="n", padx=(0, 10), ipady=1)
            col = ttk.Frame(row)
            col.pack(side="left", fill="x")
            ttk.Label(col, text=head, style="Head.TLabel").pack(anchor="w")
            ttk.Label(col, text=body, style="Small.TLabel", wraplength=px(410), justify="left").pack(anchor="w")
        ttk.Label(f, text="Changed your mind mid-way? Press R or Esc to cancel; nothing is saved.",
                  style="Small.TLabel").pack(anchor="w", pady=(10, 12))
        bottom = ttk.Frame(f)
        bottom.pack(fill="x")
        self.v_again = tk.BooleanVar(value=False)
        ttk.Checkbutton(bottom, text="Don't show this again", variable=self.v_again).pack(side="left")
        ttk.Button(bottom, text="Start recording  (R)", style="Accent.TButton",
                   command=self.start).pack(side="right")
        ttk.Button(bottom, text="Not now", command=self.dismiss).pack(side="right", padx=8)
        for key in ("<Return>", "<KeyPress-r>", "<KeyPress-R>"):
            self.win.bind(key, lambda _e: self.start())
        self.win.bind("<Escape>", lambda _e: self.dismiss())
        center_on(self.win, main.root, top_third=True)   # the app window, or the screen if only the mini bar is up
        try:
            self.win.grab_set()
        except tk.TclError:
            pass
        try:
            if main.cfg.hide_from_screen_share:
                winutil.hide_from_capture(self.win, True)
        except Exception:  # noqa: BLE001
            pass
        self.win.focus_force()

    def _remember(self) -> None:
        if self.v_again.get():
            self.main.cfg.show_record_tips = False
            self.main.cfg.save(self.e.config_path)

    def start(self) -> None:
        self._remember()
        self.e.request("record_now")

    def dismiss(self) -> None:
        self._remember()
        self.e.request("tips_closed")

    def close(self) -> None:
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()


class LicenseDialog:
    """Get License Key: what the free time is, where to get a key, and a box to paste it in."""

    def __init__(self, main: "MainWindow"):
        self.main, self.e = main, main.e
        self.lic = main.e.lic
        self.alive = True
        self.win = tk.Toplevel(main.root)
        self.win.title("Doppleganger license")
        self.win.resizable(False, False)
        self.win.configure(bg=MIDNIGHT)
        self.win.transient(main.root)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        f = ttk.Frame(self.win, padding=(26, 20))
        f.pack()
        ttk.Label(f, text="Doppleganger license", style="Title.TLabel").pack(anchor="w")
        # Prices and offers come from doppleganger.happyhacks.io each time this opens (never stale in the app)
        self.pitch_lbl = ttk.Label(f, text="Checking the latest prices and offers...", wraplength=px(440),
                                   justify="left")
        self.pitch_lbl.pack(anchor="w", pady=(6, 4))
        self.offer_lbl = ttk.Label(f, text="", wraplength=px(440), justify="left", foreground=AQUA,
                                   font=F(10, "bold"))
        self.offer_lbl.pack(anchor="w")
        self.now_lbl = ttk.Label(f, text="", style="Small.TLabel")
        self.now_lbl.pack(anchor="w", pady=(4, 12))
        self.get_btn = ttk.Button(f, text="Get Unlimited Access Now", style="Accent.TButton", command=self._open_site)
        self.get_btn.pack(anchor="w")
        self._seq = self.lic.status_seq
        self.lic.refresh()
        ttk.Label(f, text="Already have a key? Paste it here:", style="Head.TLabel").pack(anchor="w", pady=(18, 4))
        row = ttk.Frame(f)
        row.pack(fill="x")
        self.var = tk.StringVar(value=self.lic.key)
        ent = ttk.Entry(row, textvariable=self.var, width=30, font=F(11))
        ent.pack(side="left")
        ent.bind("<Return>", lambda _e: self._apply())
        ttk.Button(row, text="Apply", command=self._apply).pack(side="left", padx=8)
        self.result = ttk.Label(f, text="", wraplength=px(440), justify="left")
        self.result.pack(anchor="w", pady=(10, 0))
        bottom = ttk.Frame(f)
        bottom.pack(fill="x", pady=(16, 0))
        self.remove_btn = ttk.Button(bottom, text="Remove the key from this computer", command=self._remove)
        ttk.Button(bottom, text="Close", command=self.close).pack(side="right")
        self._applied = False
        self._tick()
        center_on(self.win, main.root, top_third=True)
        main.root.after(150, main.apply_capture_setting)
        ent.focus_set()

    def _open_site(self):
        import webbrowser
        webbrowser.open(self.lic.license_url)

    def _apply(self):
        key = self.var.get().strip()
        if not key:
            return
        self._applied = True
        self.result.configure(text="Checking the key...", foreground=MUTED)
        self.lic.apply_key(key)

    def _remove(self):
        if messagebox.askyesno("Remove license key?", "Remove the license key from this computer?\n\n"
                               "Doppleganger goes back to free LOOP time. The key stays assigned to this computer; "
                               "to move it to another one, contact support.", parent=self.win):
            self.var.set("")
            self.lic.remove_key()

    def _tick(self):
        if not self.alive:
            return
        lic = self.lic
        self.now_lbl.configure(text="Now: " + lic.summary())
        fresh = lic.status_seq != self._seq
        if fresh:
            self.pitch_lbl.configure(text=lic.pitch or "Unlimited Access: unlimited LOOP time, cancel any time.")
            self.offer_lbl.configure(text=lic.offer_text if lic.offer and not lic.unlimited else "")
        elif not lic.online and lic.message and self.pitch_lbl.cget("text").startswith("Checking"):
            self.pitch_lbl.configure(text="Couldn't reach doppleganger.happyhacks.io for the latest prices. "
                                          "Check your internet connection.")
        if self._applied:
            if lic.unlimited:
                self.result.configure(text="✓ License applied. Unlimited LOOP time on this computer.", foreground=AQUA)
            elif lic.key_problem:
                self.result.configure(text=lic.key_problem, foreground=DANGER)
            elif not lic.online and lic.message:
                self.result.configure(text=lic.message, foreground=DANGER)
        if lic.key and not self.remove_btn.winfo_ismapped():
            self.remove_btn.pack(side="left")
        elif not lic.key and self.remove_btn.winfo_ismapped():
            self.remove_btn.pack_forget()
        self.win.after(300, self._tick)

    def close(self):
        self.alive = False
        self.win.destroy()


class BusyDialog:
    """Modal 'Stitching your new loop...' with progress. Blocks the window until the loop is ready."""

    def __init__(self, parent: tk.Misc):
        self.win = tk.Toplevel(parent)
        self.win.title("Doppleganger")
        self.win.resizable(False, False)
        self.win.configure(bg=MIDNIGHT)
        self.win.transient(parent)
        self.win.protocol("WM_DELETE_WINDOW", lambda: None)  # can't be dismissed; closes by itself
        f = ttk.Frame(self.win, padding=(26, 20))
        f.pack()
        ttk.Label(f, text="Stitching your new loop...", style="Head.TLabel").pack(anchor="w")
        ttk.Label(f, text="Matching the end to the start so it repeats seamlessly. Cutting is paused "
                          "for a moment.", style="Small.TLabel", wraplength=px(320), justify="left").pack(
            anchor="w", pady=(4, 12))
        self.bar = ttk.Progressbar(f, maximum=100, length=320, style="InSync.Horizontal.TProgressbar")
        self.bar.pack()
        center_on(self.win, parent, top_third=True)
        try:
            self.win.grab_set()  # modal: clicks on the main window wait until the loop is ready
        except tk.TclError:
            pass
        try:
            winutil.hide_from_capture(self.win, True)
        except Exception:  # noqa: BLE001
            pass

    def update(self, progress: float) -> None:
        self.bar["value"] = int(progress * 100)

    def close(self) -> None:
        try:
            self.win.grab_release()
        except tk.TclError:
            pass
        self.win.destroy()


class SettingsWindow:
    def __init__(self, main: MainWindow):
        self.main = main
        self.e = main.e
        self.cfg = main.cfg
        self.alive = True
        self.win = tk.Toplevel(main.root)
        self.win.title("Doppleganger settings")
        self.win.resizable(False, False)
        self.win.transient(main.root)
        self.win.configure(bg=MIDNIGHT)
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        area = ScrollArea(self.win, max_height=self.win.winfo_screenheight() - 140)
        area.pack(fill="both", expand=True)
        f = ttk.Frame(area.inner, padding=20)
        f.pack(fill="both")
        ttk.Label(f, text="Settings", style="Title.TLabel").grid(sticky="w", pady=(0, 10))

        ttk.Label(f, text="Text size", style="Head.TLabel").grid(sticky="w", pady=(4, 2))
        ts = ttk.Frame(f)
        ts.grid(sticky="ew")
        ttk.Button(ts, text="A−", width=4, command=lambda: main.change_text_size(-1)).pack(side="left")
        self.text_pct = ttk.Label(ts, text="", width=6, anchor="center")
        self.text_pct.pack(side="left", padx=6)
        ttk.Button(ts, text="A+", width=4, command=lambda: main.change_text_size(1)).pack(side="left")
        ttk.Button(ts, text="Reset", command=lambda: main.change_text_size(0)).pack(side="left", padx=(8, 0))
        key = "Cmd" if IS_MAC else "Ctrl"
        ttk.Label(ts, text=f"or {key} +  /  {key} −", style="Small.TLabel").pack(side="left", padx=(12, 0))

        ttk.Label(f, text="License", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        lr = ttk.Frame(f)
        lr.grid(sticky="ew")
        self.lic_lbl = ttk.Label(lr, text="", style="Small.TLabel")
        self.lic_lbl.pack(side="left")
        ttk.Button(lr, text="License key...", command=main.open_license).pack(side="right")
        ttk.Button(lr, text="EULA", command=lambda: __import__("webbrowser").open(EULA_URL)).pack(side="right", padx=6)

        ttk.Label(f, text="Privacy", style="Head.TLabel").grid(sticky="w", pady=(4, 2))
        self.v_mute = tk.BooleanVar(value=self.cfg.mute_mic_on_loop)
        self.v_hide = tk.BooleanVar(value=self.cfg.hide_from_screen_share)
        ttk.Checkbutton(f, text="Mute my microphone while the LOOP is on air", variable=self.v_mute,
                        command=self._changed).grid(sticky="w")
        ttk.Checkbutton(f, text="Hide Doppleganger's windows from screen sharing", variable=self.v_hide,
                        command=self._changed).grid(sticky="w")

        ttk.Label(f, text="Cuts & preview", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        self.v_fade = tk.BooleanVar(value=self.cfg.crossfade_ms > 0)
        self.v_guides = tk.BooleanVar(value=self.cfg.show_guides)
        ttk.Checkbutton(f, text="Soft 0.3 s crossfade on cuts (off = hard cut)", variable=self.v_fade,
                        command=self._changed).grid(sticky="w")
        ttk.Checkbutton(f, text="Show alignment guides in the preview (G)", variable=self.v_guides,
                        command=self._changed).grid(sticky="w")
        self.v_outlines = tk.BooleanVar(value=self.cfg.show_outlines)
        ttk.Checkbutton(f, text="Show live (aqua) and loop (violet) outlines (O)", variable=self.v_outlines,
                        command=self._changed).grid(sticky="w")
        self.v_tips = tk.BooleanVar(value=self.cfg.show_record_tips)
        ttk.Checkbutton(f, text="Show recording tips before each recording", variable=self.v_tips,
                        command=self._changed).grid(sticky="w")
        op = ttk.Frame(f)
        op.grid(sticky="ew", pady=(6, 0))
        ttk.Label(op, text="Overlay strength").pack(side="left")
        self.v_opacity = tk.DoubleVar(value=self.cfg.overlay_opacity)
        ttk.Scale(op, from_=0.15, to=0.6, variable=self.v_opacity, length=180,
                  command=lambda _v: self._changed()).pack(side="left", padx=10)

        ttk.Label(f, text="Recording", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        rl = ttk.Frame(f)
        rl.grid(sticky="ew")
        ttk.Label(rl, text="Loop length").pack(side="left")
        self.v_len = tk.StringVar(value=str(int(self.cfg.loop_seconds)))
        self.len_box = ttk.Spinbox(rl, from_=60, to=600, increment=10, width=6, textvariable=self.v_len,
                                   command=self._len_changed, font=F(10))
        self.len_box.pack(side="left", padx=8)
        self.len_box.bind("<FocusOut>", lambda _e: self._len_changed())
        self.len_box.bind("<Return>", lambda _e: self._len_changed())
        self.len_note = ttk.Label(rl, text="seconds (60-600, default 90)", style="Small.TLabel")
        self.len_note.pack(side="left")

        ttk.Label(f, text="Hotkeys (work while your meeting app is in front)", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        hk, gk = self.cfg.hotkey.strip("<>").upper(), self.cfg.glitch_hotkey.strip("<>").upper()
        if IS_MAC:
            hk, gk = f"fn+{hk}", f"fn+{gk}"
        ttk.Label(f, text=f"{hk}: tap = cut now, hold = cut when in sync     {gk}: glitch effect",
                  style="Small.TLabel").grid(sticky="w")
        if IS_MAC:
            mac_hk = ttk.Frame(f)
            mac_hk.grid(sticky="ew")
            ttk.Label(mac_hk, text="Mac: needs Input Monitoring permission for Doppleganger.",
                      style="Small.TLabel").pack(side="left")
            ttk.Button(mac_hk, text="Open settings", command=lambda: (
                macos.request_input_monitoring(), macos.open_input_monitoring_settings())).pack(side="right")

        ttk.Label(f, text="Finding you in the picture", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        cal = ttk.Frame(f)
        cal.grid(sticky="ew")
        how = ("AI person detection - no setup needed. Calibrating the room is an optional fallback."
               if self.e.seg is not None else "Room calibration (AI detection isn't available on this PC).")
        ttk.Label(cal, text=how, style="Small.TLabel", wraplength=px(330), justify="left").pack(side="left")
        ttk.Button(cal, text="Calibrate room", command=lambda: self.e.request("calibrate")).pack(side="right")

        ttk.Label(f, text="OBS Virtual Camera" if IS_MAC else "HappyHacks Virtual Camera",
                  style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        self.v_startup = tk.BooleanVar(value=winutil.starts_with_windows())
        self.startup_chk = ttk.Checkbutton(
            f, text=f"Start Doppleganger (minimized) {WHEN_STARTS} - so your meeting app always gets a picture",
            variable=self.v_startup, command=self._startup_changed)
        self.startup_chk.grid(sticky="w")
        if winutil.startup_command() is None:
            self.startup_chk.state(["disabled"])
        cam = ttk.Frame(f)
        cam.grid(sticky="ew")
        self.drv_lbl = ttk.Label(cam, text="", style="Small.TLabel", wraplength=px(330), justify="left")
        self.drv_lbl.pack(side="left")
        if IS_MAC:  # the OBS Virtual Camera instead of our own driver
            self.drv_btn = ttk.Button(cam, text="Get OBS Studio", command=main.open_obs_help)
        else:
            self.drv_btn = ttk.Button(cam, text="Install", command=self._driver)
        self.drv_btn.pack(side="right")

        ttk.Label(f, text="Updates", style="Head.TLabel").grid(sticky="w", pady=(12, 2))
        up = ttk.Frame(f)
        up.grid(sticky="ew")
        ttk.Label(up, text=f"Version {updater.current_version()} · checks automatically when opened",
                  style="Small.TLabel").pack(side="left")
        ttk.Button(up, text="Check now", command=lambda: self.e.request("update")).pack(side="right")
        ttk.Button(f, text="Done", style="Accent.TButton", command=self.close).grid(sticky="e", pady=(16, 0))
        self.refresh()
        MainWindow._no_button_focus(self.win)
        center_on(self.win, main.root)
        self.win.after(150, main.apply_capture_setting)

    def _changed(self) -> None:
        c = self.cfg
        mute_changed = c.mute_mic_on_loop != self.v_mute.get()
        hide_changed = c.hide_from_screen_share != self.v_hide.get()
        c.mute_mic_on_loop = self.v_mute.get()
        c.hide_from_screen_share = self.v_hide.get()
        c.show_guides = self.v_guides.get()
        c.show_outlines = self.v_outlines.get()
        c.show_record_tips = self.v_tips.get()
        c.crossfade_ms = 300 if self.v_fade.get() else 0
        c.overlay_opacity = round(float(self.v_opacity.get()), 2)
        c.save(self.e.config_path)
        if mute_changed:
            self.e.request("mic_setting")
        if hide_changed:
            self.main.apply_capture_setting()

    def _len_changed(self) -> None:
        try:
            v = int(float(self.v_len.get()))
        except ValueError:
            v = int(self.cfg.loop_seconds)
        v = max(60, min(600, v))
        self.v_len.set(str(v))
        if self.e.recording_active:   # takes effect on the next recording
            return
        if v != int(self.cfg.loop_seconds):
            self.cfg.loop_seconds = float(v)
            self.cfg.save(self.e.config_path)

    def _startup_changed(self) -> None:
        try:
            winutil.set_start_with_windows(self.v_startup.get())
        except OSError as ex:
            messagebox.showerror("Doppleganger", f"Couldn't change the startup setting: {ex}", parent=self.win)
            self.v_startup.set(winutil.starts_with_windows())

    def _driver(self) -> None:
        self.e.request("uninstall_driver" if self.e.tray_driver_installed() else "install_driver")

    def refresh(self) -> None:
        if not self.alive:
            return
        self.text_pct.configure(text=f"{int(round(self.cfg.text_scale * 100))}%")
        busy = self.e.recording_active
        self.len_box.state(["disabled"] if busy else ["!disabled"])
        self.len_note.configure(text="seconds - can't change while recording" if busy else "seconds (60-600, default 90)")
        lic = self.e.lic
        self.lic_lbl.configure(text=lic.summary() + (f"  ·  key {lic.key[-4:]}" if lic.key and lic.unlimited else ""))
        self.v_guides.set(self.cfg.show_guides)
        self.v_outlines.set(self.cfg.show_outlines)
        if IS_MAC:
            st = self.main.obs
            self.drv_lbl.configure(text=("OBS Virtual Camera ready ✓ - choose it in your meeting app" if st["ready"]
                                         else st["message"]))
            self.drv_btn.configure(text="Get OBS Studio" if not st["obs"] else "Setup steps",
                                   state="disabled" if st["ready"] else "normal")
            return
        installed = self.e.tray_driver_installed()
        self.drv_lbl.configure(text="Installed ✓ - choose \"HappyHacks Virtual Camera\" in your meeting app" if installed
                               else "Not installed")
        self.drv_btn.configure(text="Remove" if installed else "Install",
                               state="normal" if (installed or self.e.tray_driver_available()) else "disabled")

    def close(self) -> None:
        self.alive = False
        self.win.destroy()
        self.main.settings = None


class MiniBar:
    """Compact always-on-top control strip to park beside Zoom."""

    TW, TH = 192, 108

    def __init__(self, main: MainWindow):
        self.main = main
        self.e = main.e
        self.win = tk.Toplevel(main.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=MIDNIGHT, highlightthickness=1, highlightbackground=BORDER)
        self.visible = False
        self._blank = ImageTk.PhotoImage(Image.new("RGB", (self.TW, self.TH), MIDNIGHT))
        self._photo = None
        f = tk.Frame(self.win, bg=MIDNIGHT, padx=8, pady=8)
        f.pack()
        self._mark = _photo(brand.asset("icon.png"), 22)
        grip = tk.Label(f, image=self._mark, bg=MIDNIGHT, cursor="fleur")
        grip.grid(row=0, column=0, sticky="n", padx=(0, 8))
        self.thumb = tk.Label(f, image=self._blank, bg=MIDNIGHT, bd=0)
        self.thumb.grid(row=0, column=1)
        right = tk.Frame(f, bg=MIDNIGHT)
        right.grid(row=0, column=2, sticky="ns", padx=(10, 0))
        self.state_lbl = tk.Label(right, text="LIVE", fg=MIDNIGHT, bg=AQUA, font=F(11, "bold"))
        self.state_lbl.pack(fill="x")
        self.sync = ttk.Progressbar(right, maximum=100, length=150, style="Sync.Horizontal.TProgressbar")
        self.sync.pack(fill="x", pady=5)
        self.cut = HoldButton(right, self.e, font_size=11)
        self.cut.configure(pady=6)
        self.cut.pack(fill="x")
        row = tk.Frame(right, bg=MIDNIGHT)
        row.pack(fill="x", pady=(5, 0))
        ttk.Button(row, text="Glitch", width=7, command=lambda: self.e.request("glitch")).pack(side="left")
        ttk.Button(row, text="Full view", width=9, command=main.show_main).pack(side="right")
        for w in (grip, self.thumb):
            w.bind("<ButtonPress-1>", self._drag_start)
            w.bind("<B1-Motion>", self._drag)
            w.bind("<ButtonRelease-1>", self._drag_end)
        self.win.bind("<KeyPress-space>", main._space_down)
        self.win.bind("<KeyRelease-space>", main._space_up)
        self._place()
        MainWindow._no_button_focus(self.win)

    def _place(self) -> None:
        pos = self.main.cfg.mini_bar_pos
        if not pos:
            self.win.update_idletasks()
            x = self.win.winfo_screenwidth() - self.win.winfo_reqwidth() - 24
            pos = f"+{max(0, x)}+60"
        self.win.geometry(pos)

    def _drag_start(self, ev):
        self._dx, self._dy = ev.x_root - self.win.winfo_x(), ev.y_root - self.win.winfo_y()

    def _drag(self, ev):
        self.win.geometry(f"+{ev.x_root - self._dx}+{ev.y_root - self._dy}")

    def _drag_end(self, _ev):
        self.main.cfg.mini_bar_pos = f"+{self.win.winfo_x()}+{self.win.winfo_y()}"
        self.main.cfg.save(self.e.config_path)

    def show(self) -> None:
        self.win.deiconify()
        self.visible = True
        self.win.after(150, self.main.apply_capture_setting)

    def hide(self) -> None:
        self.win.withdraw()
        self.visible = False

    def refresh(self, rgb) -> None:
        e = self.e
        mode = "glitch" if e.glitch.active else e.mode
        left = e.lic.remaining() if e.mode == "loop" else None
        label = mode.upper() + (f"  ·  {format_left(left)}" if left is not None and left <= WARN_AT else "")
        self.state_lbl.configure(text=label, bg=AMBER if left is not None and left <= WARN_AT else MODE_BG[mode],
                                 fg=MIDNIGHT if left is not None and left <= WARN_AT else MODE_FG[mode])
        insync = e.sync >= SYNC_THRESHOLD
        self.sync.configure(value=int(e.sync * 100),
                            style="InSync.Horizontal.TProgressbar" if insync else "Sync.Horizontal.TProgressbar")
        self.cut.refresh(short=True)
        if rgb is not None:
            small = cv2.resize(rgb, (self.TW, self.TH), interpolation=cv2.INTER_AREA)
            self._photo = ImageTk.PhotoImage(Image.fromarray(small))
            self.thumb.configure(image=self._photo)


class Wizard:
    """First-run setup: camera -> webcam -> calibrate -> first loop -> Zoom."""

    STEPS = ("welcome", "camera", "webcam", "calibrate", "record", "zoom")

    def __init__(self, main: MainWindow):
        self.main = main
        self.e = main.e
        if self.e.seg is not None:  # AI detection finds you without calibrating the room
            self.STEPS = tuple(s for s in self.STEPS if s != "calibrate")
        if IS_MAC:  # macOS privacy permissions get their own page, right after the OBS camera
            self.STEPS = ("welcome", "camera", "permissions") + tuple(
                s for s in self.STEPS if s not in ("welcome", "camera"))
        self._perm_checked = 0.0
        self.win = tk.Toplevel(main.root)
        self.win.title("Doppleganger setup")
        self.win.resizable(False, False)
        self.win.transient(main.root)
        self.win.configure(bg=MIDNIGHT)
        self.alive = True
        self.win.protocol("WM_DELETE_WINDOW", self.close)
        self.step = 0
        self._logo = _photo(brand.asset("logo_dark.png"), 330)
        self.body = ttk.Frame(self.win, padding=24, width=px(480), height=px(480 if IS_MAC else 350))
        self.body.pack(fill="both", expand=True)
        self.body.pack_propagate(False)
        nav = ttk.Frame(self.win, padding=(24, 0, 24, 18))
        nav.pack(fill="x")
        self.dots = tk.Label(nav, bg=MIDNIGHT, fg=MUTED, font=F(10))
        self.dots.pack(side="left")
        self.next_btn = ttk.Button(nav, text="Next", style="Accent.TButton", command=self.next)
        self.next_btn.pack(side="right")
        self.back_btn = ttk.Button(nav, text="Back", command=self.back)
        self.back_btn.pack(side="right", padx=6)
        self.skip_btn = ttk.Button(nav, text="Skip setup", command=self.finish)
        self.skip_btn.pack(side="right", padx=6)
        self.status = None
        self.render()
        main.root.update_idletasks()
        center_on(self.win, main.root)
        self.win.after(150, main.apply_capture_setting)

    # --- navigation ---
    def next(self):
        if self.step >= len(self.STEPS) - 1:
            self.finish()
            return
        self.step += 1
        self.render()

    def back(self):
        if self.step > 0:
            self.step -= 1
            self.render()

    def finish(self):
        self.main.cfg.setup_done = True
        self.main.cfg.save(self.e.config_path)
        self.close()

    def close(self):
        self.alive = False
        self.win.destroy()
        self.main.wizard = None

    # --- pages ---
    def _clear(self):
        for w in self.body.winfo_children():
            w.destroy()
        self.status = None

    def _title(self, text):
        ttk.Label(self.body, text=text, style="Title.TLabel").pack(anchor="w", pady=(0, 12))

    def _para(self, text):
        ttk.Label(self.body, text=text, wraplength=px(430), justify="left").pack(anchor="w", pady=(0, 9))

    def _button(self, text, action):
        ttk.Button(self.body, text=text, style="Accent.TButton", command=lambda: self.e.request(action)).pack(
            anchor="w", pady=(4, 0))

    def _status(self):
        self.status = ttk.Label(self.body, text="", wraplength=px(430), justify="left")
        self.status.pack(anchor="w", pady=(12, 0))

    def render(self):
        self._clear()
        getattr(self, f"_page_{self.STEPS[self.step]}")()
        MainWindow._no_button_focus(self.win)
        self.dots.configure(text="  ".join("●" if i == self.step else "○" for i in range(len(self.STEPS))))
        self.back_btn.configure(state="normal" if self.step else "disabled")
        last = self.step == len(self.STEPS) - 1
        self.next_btn.configure(text="Finish" if last else "Next")
        if last:
            self.skip_btn.pack_forget()
        self.refresh()

    def _page_welcome(self):
        tk.Label(self.body, image=self._logo, bg=MIDNIGHT).pack(anchor="w", pady=(4, 18))
        self._para("Doppleganger records a short, seamless loop of you, then lets you switch between "
                   "that loop and your live webcam during a meeting, so you can step away for a moment "
                   "(a restroom break, a coffee refill) without your camera going dark and distracting everyone.")
        self._para("This guide takes about 2 minutes: add the camera, pick your webcam, "
                   "and record your first loop.")

    def _page_camera(self):
        if IS_MAC:
            return self._page_camera_mac()
        self._title("1  Add the HappyHacks Virtual Camera")
        self._para("This adds a camera named \"HappyHacks Virtual Camera\" that meeting apps and browsers can use. "
                   "Windows asks for permission once.")
        self._button("Install camera", "install_driver")
        self._status()

    def _page_camera_mac(self):
        self._title("1  Set up the OBS camera")
        self._para("On a Mac, Doppleganger sends your picture to your meeting app through the free OBS Virtual Camera. "
                   "OBS only has to be installed and set up once - it doesn't need to be open during meetings.")
        steps = ("Install OBS Studio 30 or newer.",
                 "Open OBS and click \"Start Virtual Camera\" (bottom right). When macOS asks, allow the "
                 "camera extension in System Settings.",
                 "Click \"Stop Virtual Camera\" and quit OBS. Then choose \"OBS Virtual Camera\" in your meeting app.")
        for i, t in enumerate(steps, 1):
            ttk.Label(self.body, text=f"{i}.  {t}", wraplength=px(430), justify="left", style="Small.TLabel").pack(
                anchor="w", pady=(0, 4))
        row = ttk.Frame(self.body)
        row.pack(anchor="w", pady=(6, 0))
        ttk.Button(row, text="Download OBS Studio", style="Accent.TButton",
                   command=macos.open_obs_download).pack(side="left")
        ttk.Button(row, text="Open OBS", command=macos.open_obs).pack(side="left", padx=6)
        ttk.Button(row, text="Allow in Settings",
                   command=macos.open_camera_extension_settings).pack(side="left")
        self._status()

    def _page_permissions(self):
        self._title(f"{self.STEPS.index('permissions')}  Allow Doppleganger on your Mac")
        ttk.Label(self.body, text="macOS asks before an app may use your camera or notice your hotkeys. "
                                  "Turn on Doppleganger in each list below - you only do this once.",
                  wraplength=px(430), justify="left").pack(anchor="w", pady=(0, 10))
        self.perm_rows = {}
        for key, name, why, how, action in (
                ("camera", "Camera", "Shows you in the preview and records your loop.",
                 "Turn on the switch next to Doppleganger.", macos.open_camera_privacy_settings),
                ("hotkeys", "Input Monitoring",
                 "Lets fn+F9 (cut) and fn+F10 (glitch) work while your meeting app is in front.",
                 "Turn on Doppleganger (not listed? click +, then Applications > Doppleganger). "
                 "When macOS offers \"Quit & Reopen\", click it.",
                 lambda: (macos.request_input_monitoring(), macos.open_input_monitoring_settings()))):
            row = ttk.Frame(self.body)
            row.pack(fill="x", pady=(0, 10))
            state = tk.Label(row, text="…", width=2, bg=MIDNIGHT, fg=MUTED, font=F(14, "bold"))
            state.pack(side="left", anchor="n")
            col = ttk.Frame(row)
            col.pack(side="left", fill="x", expand=True, padx=(6, 8))
            ttk.Label(col, text=name, style="Head.TLabel").pack(anchor="w")
            ttk.Label(col, text=why, style="Small.TLabel", wraplength=px(280), justify="left").pack(anchor="w")
            hint = ttk.Label(col, text=how, style="Small.TLabel", wraplength=px(280), justify="left")
            hint.pack(anchor="w")
            btn = ttk.Button(row, text="Open Settings", command=action)
            btn.pack(side="right", anchor="n")
            self.perm_rows[key] = (state, hint, btn)
        self.reopen_btn = ttk.Button(self.body, text="Reopen Doppleganger", style="Accent.TButton",
                                     command=self._reopen)
        ttk.Label(self.body, text="After an update, macOS may ask again (Doppleganger isn't signed by Apple yet). "
                                  "If hotkeys stop working, switch Input Monitoring off and on for Doppleganger.",
                  style="Small.TLabel", wraplength=px(430), justify="left").pack(anchor="w", side="bottom")
        self._status()
        self._perm_checked = 0.0
        self.main.cfg.mac_permissions_seen = True
        self.main.cfg.save(self.e.config_path)

    def _reopen(self):
        self.main.cfg.save(self.e.config_path)
        macos.relaunch()
        self.main.on_close(force=True)

    def _refresh_permissions(self):
        import time as _time
        now = _time.monotonic()
        if now - self._perm_checked < 1.0:  # asking macOS is cheap, but not every 33 ms
            return
        self._perm_checked = now
        cam = macos.camera_permission()
        mon = macos.has_input_monitoring()
        hotkeys_running = getattr(self.e, "_hk", None) is not None
        marks = {"camera": cam == "allowed", "hotkeys": bool(mon) and hotkeys_running}
        for key, ok in marks.items():
            state, hint, btn = self.perm_rows[key]
            state.configure(text="✓" if ok else "!", fg=AQUA if ok else AMBER)
            if ok:
                hint.pack_forget()
        need_reopen = bool(mon) and not hotkeys_running
        if need_reopen and not self.reopen_btn.winfo_ismapped():
            self.reopen_btn.pack(anchor="w", pady=(2, 0))
        if all(marks.values()):
            txt, col = "✓ All set.", AQUA
        elif need_reopen:
            txt, col = "Input Monitoring is on - reopen Doppleganger so the hotkeys start working.", AMBER
        elif cam == "denied":
            txt, col = "Camera is off for Doppleganger - turn it on, then reopen the app.", AMBER
        else:
            txt, col = "Waiting for the permissions above...", MUTED
        self.status.configure(text=txt, foreground=col)

    def _page_webcam(self):
        self._title(f"{self.STEPS.index('webcam')}  Choose your webcam")
        self._para("Pick the webcam you want people to see. You should see yourself in the main window.")
        var = tk.StringVar(value=self.e.camera_name)
        box = ttk.Combobox(self.body, textvariable=var, state="readonly", values=self.e.tray_cameras(), width=42)
        box.pack(anchor="w")
        box.bind("<<ComboboxSelected>>", lambda _e: self.e.request(f"camera:{var.get()}"))

    def _page_calibrate(self):
        self._title(f"{self.STEPS.index('calibrate')}  Calibrate your silhouette")
        self._para("For about 10 seconds, keep facing the camera and lean or step slowly to the side the "
                   "preview asks for. Doppleganger learns what your room looks like without you, so it can "
                   "outline you precisely.")
        self._button("Start calibration", "calibrate")
        self._status()

    def _page_record(self):
        self._title(f"{self.STEPS.index('record')}  Record your first loop")
        self._para("Sit as you would in a meeting. Hold still for the first 10 seconds, then look natural - "
                   "nod, blink, glance at the screen. In the last 10 seconds, step back into your silhouette "
                   "until it glows aqua, and hold still.")
        self.rec_button = ttk.Button(self.body, text="Start recording", style="Accent.TButton",
                                     command=lambda: self.e.request("record"))
        self.rec_button.pack(anchor="w", pady=(4, 0))
        self._status()

    def _page_zoom(self):
        self._title(f"{self.STEPS.index('zoom')}  Use it in your meeting")
        hk = self.main.cfg.hotkey.strip("<>").upper()
        gk = self.main.cfg.glitch_hotkey.strip("<>").upper()
        cam = "OBS Virtual Camera" if IS_MAC else "HappyHacks Virtual Camera"
        self._para(f"In your meeting app's video (or camera) settings, choose \"{cam}\". It works with Google Meet, "
                   "Microsoft Teams, Webex, Zoom and other apps that let you choose a camera.")
        if IS_MAC:
            hk, gk = f"fn+{hk}", f"fn+{gk}"
        self._para(f"Tap {hk} to cut instantly. Hold {hk} and Doppleganger cuts by itself the moment you're in "
                   f"sync. {gk} plays a short glitch effect that gives you a few seconds to settle back in.")
        self._para("While the loop is on air your microphone is muted, and Doppleganger never appears in "
                   "screen shares. The Mini Bar keeps the controls beside your meeting.")
        if IS_MAC and macos.has_input_monitoring() is False:
            ttk.Button(self.body, text="Allow hotkeys while your meeting app is in front (Input Monitoring)",
                       command=lambda: (macos.request_input_monitoring(),
                                        macos.open_input_monitoring_settings())).pack(anchor="w", pady=(2, 4))
        if winutil.startup_command() is not None:
            self._startup = tk.BooleanVar(value=winutil.starts_with_windows())
            ttk.Checkbutton(self.body, text=f"Start Doppleganger {WHEN_STARTS} (recommended - your meeting app then "
                            "always gets a picture)", variable=self._startup,
                            command=lambda: winutil.set_start_with_windows(self._startup.get())).pack(
                anchor="w", pady=(6, 0))

    def refresh(self):
        if not self.alive or self.status is None:
            return
        e = self.e
        page = self.STEPS[self.step]
        txt, col = "", CLOUD
        if page == "permissions":
            self._refresh_permissions()
            return
        if page == "camera" and IS_MAC:
            st = self.main.obs
            if e.output.connected:
                txt, col = "✓ Ready - Doppleganger is sending to OBS Virtual Camera. Choose it in your meeting app.", AQUA
            elif st["ready"]:
                txt, col = "✓ " + st["message"], AQUA
            else:
                txt, col = st["message"], AMBER
        elif page == "camera":
            if e.tray_driver_installed():
                txt, col = "✓ Installed - you can choose \"HappyHacks Virtual Camera\" in your meeting app.", AQUA
            elif not e.tray_driver_available():
                txt, col = "The camera comes with the installed app. (From source, OBS Virtual Camera is used.)", MUTED
            else:
                txt = e.message or "Not installed yet."
        elif page == "calibrate":
            if e.calibrator is not None:
                txt = f"Calibrating... {int(e.calibrator.coverage * 100)}% of the room seen"
            elif e.background is not None:
                txt, col = "✓ Calibrated.", AQUA
        elif page == "record":
            self.rec_button.configure(text="Cancel recording" if e.recording_active else "Start recording")
            st = e.recorder.state
            if st in (State.COUNTDOWN, State.RECORDING):
                txt = f"Recording... {int(e.recorder.elapsed)} / {int(e.cfg.loop_seconds)} s"
            elif st == State.PROCESSING:
                txt = "Stitching your loop..."
            elif e.player is not None:
                txt, col = "✓ You have a loop. Record more any time.", AQUA
        self.status.configure(text=txt, foreground=col)


def run_startup_update(rel, token: str) -> str | None:
    """Small 'Updating...' window while the new version downloads. Returns the downloaded path."""
    root = tk.Tk()
    root.title("Doppleganger")
    root.resizable(False, False)
    apply_theme(root)
    f = ttk.Frame(root, padding=22)
    f.pack()
    logo = _photo(brand.asset("logo_dark.png"), 260)
    tk.Label(f, image=logo, bg=MIDNIGHT).pack(anchor="w", pady=(0, 12))
    ttk.Label(f, text=f"Updating to v{rel.version}...", style="Head.TLabel").pack(anchor="w")
    bar = ttk.Progressbar(f, maximum=100, length=360, style="InSync.Horizontal.TProgressbar")
    bar.pack(pady=(10, 0))
    result: dict = {}

    def work():
        try:
            result["path"] = updater.download(rel, token, lambda p: result.__setitem__("p", p))
        except updater.UpdateError as ex:
            result["err"] = str(ex)

    t = threading.Thread(target=work, daemon=True)
    t.start()

    def poll():
        bar["value"] = int(result.get("p", 0) * 100)
        if t.is_alive():
            root.after(100, poll)
        else:
            root.destroy()

    root.after(100, poll)
    root.mainloop()
    return result.get("path")
