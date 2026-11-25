"""Camera moved, something in the background changed, different clothes - on simple synthetic scenes."""
import cv2
import numpy as np

from doppleganger import facekeys as FK, scenecheck as SC

H, W = 720, 1280


def room(seed=3):
    rng = np.random.default_rng(seed)
    img = np.full((H, W, 3), 170, np.uint8)
    for _ in range(140):   # shelves, frames, objects: things to recognise
        x, y = int(rng.integers(0, W - 80)), int(rng.integers(0, H - 80))
        w, h = int(rng.integers(20, 90)), int(rng.integers(20, 90))
        cv2.rectangle(img, (x, y), (x + w, y + h), tuple(int(c) for c in rng.integers(20, 240, 3)), -1)
    return cv2.GaussianBlur(img, (3, 3), 0)


def with_person(img, shirt=(150, 110, 30)):
    out = img.copy()
    cv2.ellipse(out, (640, 330), (95, 120), 0, 0, 360, (140, 170, 210), -1)      # head
    cv2.ellipse(out, (640, 720), (300, 260), 0, 180, 360, shirt, -1)             # shoulders / top
    mask = np.zeros((H, W), np.float32)
    cv2.ellipse(mask, (640, 330), (95, 120), 0, 0, 360, 1.0, -1)
    cv2.ellipse(mask, (640, 720), (300, 260), 0, 180, 360, 1.0, -1)
    return out, mask


def noisy(img, seed=9):
    rng = np.random.default_rng(seed)
    return np.clip(img.astype(np.int16) + rng.integers(-6, 7, img.shape), 0, 255).astype(np.uint8)


def shift(img, dx, dy=0, scale=1.0):
    m = cv2.getRotationMatrix2D((W / 2, H / 2), 0, scale)
    m[0, 2] += dx
    m[1, 2] += dy
    return cv2.warpAffine(img, m, (W, H), borderMode=cv2.BORDER_REFLECT)


def test_nothing_changed():
    loop, pm = with_person(room())
    live, lm = with_person(noisy(room()))
    assert SC.compare(loop, pm, live, lm).messages() == []


def test_camera_moved():
    loop, pm = with_person(room())
    live, lm = with_person(noisy(shift(room(), 60)))   # whole room shifted ~5% of the width
    assert SC.compare(loop, pm, live, lm).moved


def test_camera_zoomed():
    loop, pm = with_person(room())
    live, lm = with_person(noisy(shift(room(), 0, 0, 1.08)))
    assert SC.compare(loop, pm, live, lm).moved


def test_something_new_in_the_background_and_where():
    loop, pm = with_person(room())
    changed = room()
    cv2.rectangle(changed, (40, 380), (260, 700), (30, 30, 30), -1)      # a dark chair / person, bottom left
    live, lm = with_person(noisy(changed))
    f = SC.compare(loop, pm, live, lm)
    assert not f.moved and f.background and "left" in f.background and "bottom" in f.background


def test_someone_else_in_shot_counts_as_background():
    loop, pm = with_person(room())
    live, lm = with_person(noisy(room()))
    cv2.ellipse(live, (120, 560), (90, 160), 0, 0, 360, (60, 90, 140), -1)    # a second person, at the left
    lm2 = lm.copy()
    cv2.ellipse(lm2, (120, 560), (90, 160), 0, 0, 360, 1.0, -1)               # the detector marks them too
    assert SC.compare(loop, pm, live, lm2).background


def test_different_top():
    loop, pm = with_person(room(), shirt=(150, 110, 30))        # teal
    live, lm = with_person(noisy(room()), shirt=(215, 215, 220))   # white
    f = SC.compare(loop, pm, live, lm)
    assert f.clothes and not f.background


def test_away_from_the_desk_clothes_not_compared():
    loop, pm = with_person(room())
    f = SC.compare(loop, pm, noisy(room()), np.zeros((H, W), np.float32))
    assert f.clothes is None


# ---------- face guide ----------
def face(dx=0.0, dy=0.0, s=1.0):
    c = np.array([0.5 + dx, 0.45 + dy])
    pts = [(-0.03, 0.0), (0.03, 0.0), (0.0, 0.05), (0.0, 0.1)]
    return FK.Face(*[tuple(c + np.array(p) * [s, s * 16 / 9]) for p in pts])


def test_face_lined_up():
    a = FK.align(face(), face(0.002, 0.003))
    assert a.aligned and all(a.matched) and a.hint is None


def test_face_hints():
    assert FK.align(face(), face(s=0.8)).hint == "Come a little closer"
    assert FK.align(face(), face(s=1.25)).hint == "Sit back a little"
    # live face further right in the webcam's picture = further LEFT in the mirrored preview: move right
    assert FK.align(face(), face(dx=0.02)).hint == "Move a little right"
    assert FK.align(face(), face(dx=0.02), mirrored=False).hint == "Move a little left"
    assert FK.align(face(), face(dy=-0.03)).hint == "Sit a little lower"


def test_warnings_need_several_checks_in_a_row():
    from doppleganger import engine as E
    e = E.Engine.__new__(E.Engine)
    e.scene_warnings, e._scene_seen = [], {}
    e.notify = lambda *a, **k: None
    m = "Your top looks different from this loop"
    e.scene_warnings = e._confirm([m])
    e.scene_warnings = e._confirm([m])
    assert e.scene_warnings == []                      # twice isn't enough
    e.scene_warnings = e._confirm([m])
    assert e.scene_warnings == [m]
    e.scene_warnings = e._confirm([])
    assert e.scene_warnings == [m]                     # one miss: still shown
    e.scene_warnings = e._confirm([])
    assert e.scene_warnings == []                      # cleared
