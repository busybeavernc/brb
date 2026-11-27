import os
import unittest

import cv2
import numpy as np

from doppleganger import facekeys

PERSON = os.path.join(os.path.dirname(__file__), "data", "person.jpg")


def scene(face_frac: float, x_frac: float, w: int = 1280, h: int = 720):
    """The test person, shrunk so their face is about `face_frac` of a 16:9 picture's width (sitting back from the
    webcam), centred at `x_frac` across. -> (picture, where their right eye should be found)"""
    img = cv2.imread(PERSON)
    ref = facekeys.FaceFinder().find(img)
    s = face_frac * w / (img.shape[1] * 0.28)
    small = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    sh, sw = small.shape[:2]
    sw = min(sw, w)
    x0 = max(0, min(w - sw, int(w * x_frac - sw / 2)))
    y0 = h // 5
    hh = min(sh, h - y0)
    out = np.full((h, w, 3), 120, np.uint8)
    out[y0:y0 + hh, x0:x0 + sw] = small[:hh, :sw]
    return out, ((x0 + ref.right_eye[0] * sw) / w, (y0 + ref.right_eye[1] * sh) / h)


class FaceFinderTest(unittest.TestCase):
    def test_finds_a_face_far_from_the_camera(self):
        f = facekeys.FaceFinder()
        for frac in (0.2, 0.08, 0.05):           # 0.08: someone sitting back - the whole-picture pass misses it
            for x in (0.25, 0.5, 0.75):
                img, (ex, ey) = scene(frac, x)
                face = f.find(img)
                self.assertIsNotNone(face, f"face {frac} of the width at {x}")
                self.assertLess(abs(face.right_eye[0] - ex) + abs(face.right_eye[1] - ey), 0.03)

    def test_no_face(self):
        self.assertIsNone(facekeys.FaceFinder().find(np.full((720, 1280, 3), 120, np.uint8)))


if __name__ == "__main__":
    unittest.main()
