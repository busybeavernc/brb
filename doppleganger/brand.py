"""BRB (Be Right Back) brand: "Need a moment? Be right back."

Palette (from the brand sheet):
  Deep Teal #244B4B   Eucalyptus #8FB9AA   Apricot #EAB394   Ivory #FAF8F3
Usage: LIVE = Eucalyptus, LOOP = Apricot, with Deep Teal text. The cut button wears the colour of where you're cutting TO.
The names below are the app's roles (kept from the first brand): MIDNIGHT is the window background, CLOUD the text,
AQUA the LIVE colour, VIOLET the LOOP colour.
"""
from __future__ import annotations

import os
import sys

TAGLINE = "Need a moment? Be right back."
APP_NAME = "BRB"
APP_LONG = "BRB - Be Right Back"

TEAL = "#244B4B"
EUCALYPTUS = "#8FB9AA"
APRICOT = "#EAB394"
IVORY = "#FAF8F3"

MIDNIGHT = IVORY          # window background
CLOUD = TEAL              # text
INK = "#1D3D3D"           # text on coloured chips (LIVE / LOOP / warnings)
AQUA = EUCALYPTUS         # LIVE
VIOLET = APRICOT          # LOOP

# supporting shades (derived)
SURFACE = "#FFFFFF"       # buttons, fields, lists on Ivory
SURFACE_HI = "#E7F0EC"    # hover / selected (a touch of Eucalyptus)
BORDER = "#D8D1C5"
MUTED = "#6B807C"
VIOLET_DARK = "#D99A74"
AQUA_DARK = "#5E9A86"
TEAL_DARK = "#1A3838"
AMBER = "#F0BE5A"         # warnings (functional, not a brand colour)
DANGER = "#E07A6E"
# coloured text on Ivory (the chip colours above are too light for text)
OK_TEXT = "#2F7A64"
WARN_TEXT = "#9A6212"
BAD_TEXT = "#B5443A"


def bgr(hex_color: str) -> tuple[int, int, int]:
    """'#RRGGBB' -> OpenCV (B, G, R)."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return b, g, r


# OpenCV colours for the preview overlays: deeper versions of the brand colours, so white text and thin outlines
# stand out on video
CV_AQUA = bgr("#4FA387")          # LIVE
CV_VIOLET = bgr("#D9814F")        # LOOP
CV_VIOLET_DARK = bgr("#B5643A")
CV_AQUA_DARK = bgr("#2F7A64")
CV_AMBER = bgr("#D99A2B")
CV_MIDNIGHT = bgr(TEAL)           # dark tracks / panels on video
CV_DANGER = bgr("#D2544A")


def asset(name: str) -> str:
    base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
    for p in (os.path.join(base, "doppleganger", "assets", name), os.path.join(base, "assets", name)):
        if os.path.exists(p):
            return p
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "assets", name)


# ---------- themes (Settings > Appearance) ----------
# Each theme sets the colour roles above. Ivory is the brand look; Slate is a cooler, neutral alternative.
THEMES = {
    "ivory": {
        "label": "Ivory (default)", "LIVE_WORD": "green", "LOOP_WORD": "apricot",
        "MIDNIGHT": IVORY, "CLOUD": TEAL, "INK": "#1D3D3D", "AQUA": EUCALYPTUS, "VIOLET": APRICOT,
        "SURFACE": "#FFFFFF", "SURFACE_HI": "#E7F0EC", "BORDER": "#D8D1C5", "MUTED": "#6B807C",
        "VIOLET_DARK": "#D99A74", "AQUA_DARK": "#5E9A86", "TEAL": TEAL, "TEAL_DARK": "#1A3838", "IVORY": IVORY,
        "AMBER": "#F0BE5A", "DANGER": "#E07A6E", "OK_TEXT": "#2F7A64", "WARN_TEXT": "#9A6212", "BAD_TEXT": "#B5443A",
        "cv": {"AQUA": "#4FA387", "VIOLET": "#D9814F", "VIOLET_DARK": "#B5643A", "AQUA_DARK": "#2F7A64",
               "AMBER": "#D99A2B", "MIDNIGHT": TEAL, "DANGER": "#D2544A"},
    },
    "slate": {
        "label": "Slate", "LIVE_WORD": "blue", "LOOP_WORD": "copper",
        "MIDNIGHT": "#ECEFF3", "CLOUD": "#1E2A36", "INK": "#15202B", "AQUA": "#8FB3D1", "VIOLET": "#DDA272",
        "SURFACE": "#FFFFFF", "SURFACE_HI": "#E1E8F0", "BORDER": "#C9D1DB", "MUTED": "#5F6B78",
        "VIOLET_DARK": "#C2814C", "AQUA_DARK": "#5E86A8", "TEAL": "#2C4A66", "TEAL_DARK": "#1F364C", "IVORY": "#F5F7FA",
        "AMBER": "#F0BE5A", "DANGER": "#E07A6E", "OK_TEXT": "#2E6A8F", "WARN_TEXT": "#93600F", "BAD_TEXT": "#B5443A",
        "cv": {"AQUA": "#3F7BAE", "VIOLET": "#C8763A", "VIOLET_DARK": "#A85F2A", "AQUA_DARK": "#2B5C86",
               "AMBER": "#D99A2B", "MIDNIGHT": "#1E2A36", "DANGER": "#D2544A"},
    },
}
THEME = "ivory"
LIVE_WORD, LOOP_WORD = "green", "apricot"   # the LIVE / LOOP colours by name, for labels


def set_theme(name: str) -> str:
    """Switch every colour role to `name` (unknown names: the default). Takes effect for windows built afterwards
    (the window is rebuilt when the theme changes); the preview overlays use it straight away."""
    global THEME
    t = THEMES.get(name) or THEMES["ivory"]
    THEME = name if name in THEMES else "ivory"
    g = globals()
    for k, v in t.items():
        if k not in ("label", "cv"):
            g[k] = v
    for k, v in t["cv"].items():
        g["CV_" + k] = bgr(v)
    return THEME
