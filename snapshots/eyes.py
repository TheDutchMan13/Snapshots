"""Shut eyes: is each eye in a face open or closed?

Each eye is cut out around the point the face detector puts on it (a square 0.6 times the distance between
the eyes), shrunk to 24 x 24 grey pixels, its contrast evened out, and described by the directions of its
edges (a histogram of oriented gradients, Dalal and Triggs 2005). An open eye has the round edges of the
iris and the eye's outline; a shut one mostly a single lash line. A logistic regression, trained here on
6,794 eyes from the Open and Closed Eyes Dataset (Michał Młodawski, ODC-By), turns that into the chance
that the eye is shut. The weights are below; nothing is downloaded.

Measured: on held-out faces it caught 86% of shut eyes (both eyes over 0.8) and flagged none of the open
ones. Real photos are harder: in 400 everyday photos (Flickr30k) it also flags people looking down, squinting
in a big smile, or wearing sunglasses, and faces turned to the side. So a face only counts when it looks at
the camera, is one of the main faces, both eyes are over SHUT, and that person's eyes don't always look this
way (eyes.EyesJob).
"""

import base64
import logging

log = logging.getLogger("snapshots.eyes")

SIDE = 24
SHUT = 0.90            # both eyes at least this likely shut
MIN_FRONTAL = 0.6      # the face looks at the camera
MIN_SIDE = 0.5         # ... and is at least half as big as the biggest face in the photo
PERSON_FACES = 5       # with this many faces of a person known ...
PERSON_USUAL = 0.5     # ... whose eyes usually look shut (glasses, narrow eyes): never flagged

_BIAS = 28.33470916748047
_WEIGHTS = (
    "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"
)
_W = None


def _model():
    global _W
    if _W is None:
        import numpy as np

        _W = np.frombuffer(base64.b64decode("".join(_WEIGHTS)), dtype="<f4").astype(np.float32)
    return _W


_HOG = None


def _hog():
    global _HOG
    if _HOG is None:
        import cv2

        _HOG = cv2.HOGDescriptor((SIDE, SIDE), (8, 8), (4, 4), (4, 4), 9)
    return _HOG


def eye_shut(grey, ex: float, ey: float, d: float) -> float | None:
    """Chance (0-1) that the eye at (ex, ey) is shut. grey: uint8 array; d: distance between the eyes."""
    import cv2
    import numpy as np

    h, w = grey.shape[:2]
    half = 0.3 * d
    x0, y0, x1, y1 = int(ex - half), int(ey - half), int(ex + half), int(ey + half)
    if x0 < 0 or y0 < 0 or x1 > w or y1 > h or x1 - x0 < 6:
        return None
    patch = cv2.resize(np.ascontiguousarray(grey[y0:y1, x0:x1]), (SIDE, SIDE), interpolation=cv2.INTER_AREA)
    f = _hog().compute(cv2.equalizeHist(patch)).ravel()
    z = float(f @ _model()) + _BIAS
    return 1.0 / (1.0 + float(np.exp(-z)))


def face_shut(grey, landmarks: list[float]) -> float | None:
    """The chance that both eyes of a face are shut (the lower of the two), from landmarks in pixels."""
    import numpy as np

    rx, ry, lx, ly = landmarks[:4]
    d = float(np.hypot(lx - rx, ly - ry))
    if d < 6:
        return None
    if grey.dtype != np.uint8:
        grey = np.clip(grey, 0, 255).astype(np.uint8)
    both = [eye_shut(grey, rx, ry, d), eye_shut(grey, lx, ly, d)]
    if None in both:
        return None
    return round(min(both), 4)


def shut_photos(rows) -> set[int]:
    """Photos where someone's eyes are shut, from face rows
    (asset_id, person_id, w, shut, frontal, person's usual shut or None)."""
    biggest: dict[int, float] = {}
    for asset_id, _, w, *_ in rows:
        biggest[asset_id] = max(biggest.get(asset_id, 0.0), w or 0.0)
    out = set()
    for asset_id, _, w, shut, frontal, usual in rows:
        if shut is None or shut < SHUT or (frontal or 0) < MIN_FRONTAL:
            continue
        if (w or 0) < MIN_SIDE * biggest[asset_id]:
            continue                       # someone in the background
        if usual is not None and usual >= PERSON_USUAL:
            continue                       # this person's eyes always look like this
        out.add(asset_id)
    return out


class EyesJob:
    """Marks photos where someone's eyes are shut (for Clean up), again whenever faces change."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self._key = None
        self.state = {"running": False, "found": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 0) -> bool:
        key = tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(person_id), 0),"
                                 " COUNT(shut) FROM faces").fetchone())
        if key == self._key:
            return False
        import statistics

        usual: dict[int, float] = {}
        per_person: dict[int, list[float]] = {}
        for pid, shut in conn.execute("SELECT person_id, shut FROM faces WHERE person_id IS NOT NULL"
                                      " AND shut IS NOT NULL AND frontal >= ?", (MIN_FRONTAL,)):
            per_person.setdefault(pid, []).append(shut)
        for pid, values in per_person.items():
            if len(values) >= PERSON_FACES:
                usual[pid] = statistics.median(values)
        rows = [(r[0], r[1], r[2], r[3], r[4], usual.get(r[1])) for r in conn.execute(
            "SELECT asset_id, person_id, w, shut, frontal FROM faces")]
        found = shut_photos(rows)
        conn.execute("UPDATE assets SET eyes_closed = 0 WHERE eyes_closed = 1")
        for start in range(0, len(found), 900):
            chunk = list(found)[start:start + 900]
            conn.execute(f"UPDATE assets SET eyes_closed = 1 WHERE id IN ({','.join('?' * len(chunk))})", chunk)
        conn.commit()
        self._key = key
        self.state["found"] = len(found)
        return True
