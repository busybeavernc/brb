"""Loop library: names, thumbnails, and "does this loop's lighting match the room right now?"."""
from __future__ import annotations

import glob
import json
import os
import time
from dataclasses import dataclass

import cv2
import numpy as np

LIGHT_SIZE = (64, 36)


@dataclass
class LoopInfo:
    path: str
    name: str
    created: float
    seconds: float
    lighting: tuple[float, float, float] | None  # mean L, a, b of the first frame

    @property
    def stem(self) -> str:
        return os.path.basename(self.path)


def _meta_path(path: str) -> str:
    return path + ".json"


def read_meta(path: str) -> dict:
    try:
        with open(_meta_path(path), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


def write_meta(path: str, meta: dict) -> None:
    with open(_meta_path(path), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


def default_name(created: float) -> str:
    return time.strftime("%b %d, %I:%M %p", time.localtime(created)).replace(" 0", " ")


def lighting_of(frame: np.ndarray) -> tuple[float, float, float]:
    """Average brightness (L) and colour (a, b) of a frame."""
    small = cv2.resize(frame, LIGHT_SIZE, interpolation=cv2.INTER_AREA)
    lab = cv2.cvtColor(small, cv2.COLOR_BGR2LAB).reshape(-1, 3).astype(np.float32)
    return tuple(float(v) for v in lab.mean(axis=0))


def _first_frame(path: str) -> np.ndarray | None:
    cap = cv2.VideoCapture(path)
    ok, f = cap.read()
    cap.release()
    return f if ok else None


def info(path: str) -> LoopInfo:
    meta = read_meta(path)
    created = os.path.getmtime(path)
    if "lighting" not in meta:
        f = _first_frame(path)
        if f is not None:
            meta["lighting"] = list(lighting_of(f))
            write_meta(path, meta)
    fps = meta.get("fps") or 30
    seconds = (meta.get("frames") or 0) / fps
    light = tuple(meta["lighting"]) if meta.get("lighting") else None
    return LoopInfo(path, meta.get("name") or default_name(created), created, seconds, light)


def list_loops(loops_dir: str) -> list[LoopInfo]:
    paths = sorted(glob.glob(os.path.join(loops_dir, "loop_*.mp4")), reverse=True)  # newest first
    return [info(p) for p in paths]


def rename(path: str, name: str) -> None:
    meta = read_meta(path)
    meta["name"] = name.strip() or default_name(os.path.getmtime(path))
    write_meta(path, meta)


def delete(path: str) -> None:
    for p in (path, path + ".json", path + ".mask.png", path + ".thumb.jpg"):
        try:
            os.remove(p)
        except OSError:
            pass


def thumbnail(path: str, width: int = 112) -> np.ndarray | None:
    """First frame, small (BGR). Cached next to the loop."""
    cache = path + ".thumb.jpg"
    img = cv2.imread(cache) if os.path.exists(cache) else None
    if img is None or img.shape[1] != width:
        f = _first_frame(path)
        if f is None:
            return None
        img = cv2.resize(f, (width, width * f.shape[0] // f.shape[1]), interpolation=cv2.INTER_AREA)
        cv2.imwrite(cache, img)
    return img


def lighting_warning(loop_light, live_light, matched: bool = False) -> str | None:
    """Plain-language warning when the room's light no longer matches the loop. `matched`: BRB adjusts the loop
    to the room's light, so only differences too big for that are worth a warning."""
    if loop_light is None or live_light is None:
        return None
    dl = live_light[0] - loop_light[0]
    dc = float(np.hypot(live_light[1] - loop_light[1], live_light[2] - loop_light[2]))
    big, tint = (45, 16) if matched else (14, 7)
    if dl < -big:
        return "Room is much darker than this loop" if matched else "Room is darker than this loop"
    if dl > big:
        return "Room is much brighter than this loop" if matched else "Room is brighter than this loop"
    if dc > tint:
        return "Light colour differs a lot from this loop" if matched else "Light colour differs from this loop"
    return None


SHORT = {"Room is darker than this loop": "darker room",
         "Room is brighter than this loop": "brighter room",
         "Light colour differs from this loop": "different light",
         "Room is much darker than this loop": "much darker room",
         "Room is much brighter than this loop": "much brighter room",
         "Light colour differs a lot from this loop": "very different light"}


def lighting_short(warning: str | None) -> str | None:
    return SHORT.get(warning, warning) if warning else None
