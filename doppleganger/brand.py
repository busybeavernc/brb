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
AMBER = "#F0BE5A"         # glitch / warnings (functional, not a brand colour)
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
