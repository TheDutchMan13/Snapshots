"""Finding faces in photos and grouping them into people. Everything runs on this computer.

Models (downloaded once, about 39 MB, checked against known SHA-256 hashes):
* YuNet face detector, MIT licence           (https://huggingface.co/opencv/face_detection_yunet)
* SFace face recogniser, Apache 2.0 licence  (https://huggingface.co/opencv/face_recognition_sface)

Each face becomes a 128-number "embedding". Faces whose embeddings point the same way are
the same person. A new face joins the person whose average embedding is most similar, if
that similarity is at least `face_match_threshold`; otherwise it starts a new person.
The default (0.50) was chosen on 1,200 photos of 598 people from the LFW face set: no group
ever held two different people, while 98.8% of same-person pairs were grouped together.
(0.45 grouped slightly more but let a stranger into 2 groups.) Splits can be merged in the app.

Faces are grouped one at a time, so the same person sometimes ends up in two groups (say, with and
without glasses). After each round of new photos, `regroup` compares whole groups: two groups whose
faces are, on average, at least `face_merge_threshold` alike become one. Only groups of 2 or more
faces take part: on LFW (1,862 faces: 26 people with many photos among 572 others), merging single
faces as well let strangers in, while this rule removed splits without a single wrong merge.
Two differently named people, and hidden people, are never merged.
"""

import hashlib
import json
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path

from . import media, libraries

log = logging.getLogger("snapshots.faces")

MODELS = {
    "detector": {
        "file": "face_detection_yunet_2023mar.onnx",
        "sha256": "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4",
        "urls": [
            "https://huggingface.co/opencv/face_detection_yunet/resolve/main/face_detection_yunet_2023mar.onnx",
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/face_detection_yunet_2023mar.onnx",
        ],
    },
    "recognizer": {
        "file": "face_recognition_sface_2021dec.onnx",
        "sha256": "0ba9fbfa01b5270c96627c4ef784da859931e02f04419c829e83484087c34e79",
        "urls": [
            "https://huggingface.co/opencv/face_recognition_sface/resolve/main/face_recognition_sface_2021dec.onnx",
            "https://github.com/opencv/opencv_zoo/raw/main/models/face_recognition_sface/face_recognition_sface_2021dec.onnx",
        ],
    },
}

DETECT_MAX_SIDE = 1920   # photos are shrunk to this before looking for faces
DETECT_SCORE = 0.9       # how sure the detector must be that it found a face
MIN_FACE_PX = 40         # smaller faces are too blurry to recognise reliably (measured on LFW)
FACE_THUMB_PX = 160


def available() -> str | None:
    """None if face recognition can run here, otherwise the reason it can't."""
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the install
        return f"Face recognition needs OpenCV and NumPy ({exc})."
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def ensure_models(model_dir: Path) -> dict[str, Path]:
    """Make sure both models are present and genuine, downloading them if needed."""
    model_dir.mkdir(parents=True, exist_ok=True)
    paths = {}
    for key, spec in MODELS.items():
        path = model_dir / spec["file"]
        if path.exists() and _sha256(path) == spec["sha256"]:
            paths[key] = path
            continue
        last_error = None
        for url in spec["urls"]:
            tmp = path.with_name(path.name + ".part")
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "Snapshots photo library"})
                with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
                    while chunk := resp.read(1 << 16):
                        out.write(chunk)
                if _sha256(tmp) != spec["sha256"]:
                    raise ValueError("the downloaded file did not match the expected checksum")
                os.replace(tmp, path)
                paths[key] = path
                break
            except Exception as exc:
                last_error = exc
                tmp.unlink(missing_ok=True)
        if key not in paths:
            raise RuntimeError(f"Could not download {spec['file']}: {last_error}")
    return paths


# ---------------------------------------------------------------- how good a face looks

EYE_PATCH = 0.18      # half the side of the square looked at around each eye, as a share of the eye distance
CHEEK_DROP = 0.45     # the cheek used for comparison sits this far below the eye (share of the eye distance)
FACE_SIDE = 112       # faces are measured at this width, so big and small faces compare


def _patch(grey, cx: float, cy: float, half: float):
    h, w = grey.shape
    x0, x1 = int(max(0, cx - half)), int(min(w, cx + half + 1))
    y0, y1 = int(max(0, cy - half)), int(min(h, cy + half + 1))
    return grey[y0:y1, x0:x1]


def face_quality(grey, landmarks: list[float], box: tuple[float, float, float, float]) -> dict:
    """Eyes, sharpness and frontalness of one face, from a greyscale picture (a float32 array, in pixels).

    * eyes: the detail (spread of light and dark) around each eye, over that of the cheek below it. An open
      eye has white, iris and pupil; a closed one is mostly skin and a lash line, so the number drops
      when someone blinks. It is only compared between photos of the same person in one burst.
    * sharp: fine detail in the face (variance of second differences, the weaker direction), at FACE_SIDE.
    * frontal: 1 when the nose is midway between the eyes (looking at the camera), 0 when turned well away.
    """
    import numpy as np

    rx, ry, lx, ly, nx, ny = landmarks[:6]
    d = float(np.hypot(lx - rx, ly - ry))
    out = {"eyes": None, "sharp": None, "frontal": None}
    if d < 6:
        return out
    ratios = []
    for ex, ey in ((rx, ry), (lx, ly)):
        eye = _patch(grey, ex, ey, EYE_PATCH * d)
        cheek = _patch(grey, ex, ey + CHEEK_DROP * d, EYE_PATCH * d)
        if eye.size >= 9 and cheek.size >= 9:
            ratios.append(float(eye.std()) / (float(cheek.std()) + 4.0))
    if ratios:
        out["eyes"] = round(sum(ratios) / len(ratios), 4)
    mid_x = (rx + lx) / 2
    out["frontal"] = round(max(0.0, 1.0 - abs(nx - mid_x) / (0.35 * d)), 3)
    x, y, w, h = box
    face = grey[int(max(0, y)):int(max(0, y + h)), int(max(0, x)):int(max(0, x + w))]
    if face.shape[0] > 8 and face.shape[1] > 8:
        from PIL import Image

        scale = FACE_SIDE / face.shape[1]
        img = Image.fromarray(face.astype(np.uint8)).resize(
            (FACE_SIDE, max(8, round(face.shape[0] * scale))), Image.BILINEAR)
        g = np.asarray(img, dtype=np.float32)
        across = g[:, :-2] + g[:, 2:] - 2 * g[:, 1:-1]
        down = g[:-2] + g[2:] - 2 * g[1:-1]
        out["sharp"] = round(min(float(across.var()), float(down.var())), 2)
    return out


def _grey(img):
    import numpy as np

    return np.asarray(img.convert("L"), dtype=np.float32)


class FaceEngine:
    def __init__(self, paths: dict[str, Path], threads: int = 2):
        import cv2

        cv2.setNumThreads(max(1, int(threads)))
        self.cv2 = cv2
        self.detector = cv2.FaceDetectorYN.create(str(paths["detector"]), "", (320, 320), DETECT_SCORE, 0.3, 5000)
        self.recognizer = cv2.FaceRecognizerSF.create(str(paths["recognizer"]), "")

    def analyze(self, path: Path) -> list[dict]:
        """Faces in one photo: position (as fractions), score, embedding and a small picture."""
        import numpy as np

        img = media.load_oriented(path, DETECT_MAX_SIDE)
        w, h = img.size
        if min(w, h) < MIN_FACE_PX:
            return []
        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(bgr)
        if found is None:
            return []
        faces = []
        grey = _grey(img)
        for row in found:
            x, y, fw, fh = (float(v) for v in row[:4])
            if fw < MIN_FACE_PX or fh < MIN_FACE_PX:
                continue
            emb = self.recognizer.feature(self.recognizer.alignCrop(bgr, row)).flatten().astype(np.float32)
            norm = float(np.linalg.norm(emb))
            if not norm:
                continue
            x0, y0 = max(0.0, x), max(0.0, y)
            marks = [float(v) for v in row[4:14]]
            faces.append({
                "x": x0 / w, "y": y0 / h,
                "w": (min(w, x + fw) - x0) / w, "h": (min(h, y + fh) - y0) / h,
                "score": float(row[14]),
                "embedding": emb / norm,
                "thumb": _face_picture(img, x, y, fw, fh),
                "landmarks": [round(v / (w if i % 2 == 0 else h), 4) for i, v in enumerate(marks)],
                **face_quality(grey, marks, (x, y, fw, fh)),
            })
        return faces

    def measure(self, path: Path, boxes: list[tuple[float, float, float, float]]) -> list[dict | None]:
        """Landmarks and face_quality for faces found before (boxes as fractions), matched by position.
        For photos whose faces were found before this was measured."""
        img = media.load_oriented(path, DETECT_MAX_SIDE)
        w, h = img.size
        import numpy as np

        bgr = np.ascontiguousarray(np.asarray(img)[:, :, ::-1])
        self.detector.setInputSize((w, h))
        _, found = self.detector.detect(bgr)
        grey = _grey(img)
        out: list[dict | None] = []
        for bx, by, bw, bh in boxes:
            best, best_iou = None, 0.3
            for row in (found if found is not None else []):
                iou = _iou((bx, by, bw, bh), (row[0] / w, row[1] / h, row[2] / w, row[3] / h))
                if iou > best_iou:
                    best, best_iou = row, iou
            if best is None:
                out.append(None)
                continue
            marks = [float(v) for v in best[4:14]]
            q = face_quality(grey, marks, tuple(float(v) for v in best[:4]))
            q["landmarks"] = [round(v / (w if i % 2 == 0 else h), 4) for i, v in enumerate(marks)]
            out.append(q)
        return out


def _iou(a, b) -> float:
    ax, ay, aw, ah = a
    bx, by, bw, bh = b
    ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
    iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
    inter = ix * iy
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _face_picture(img, x, y, fw, fh):
    side = max(fw, fh) * 1.6
    cx, cy = x + fw / 2, y + fh / 2
    left = max(0, int(cx - side / 2))
    top = max(0, int(cy - side / 2))
    right = min(img.width, int(cx + side / 2))
    bottom = min(img.height, int(cy + side / 2))
    crop = img.crop((left, top, right, bottom))
    crop.thumbnail((FACE_THUMB_PX, FACE_THUMB_PX))
    return crop


class Clusterer:
    """Average embedding of every person, kept up to date as faces are added."""

    def __init__(self, conn, threshold: float):
        import numpy as np

        self.np = np
        self.threshold = threshold
        self.index: dict[int, int] = {}
        self.ids: list[int] = []
        sums: dict[int, "np.ndarray"] = {}
        for person_id, blob in conn.execute(
            "SELECT person_id, embedding FROM faces WHERE person_id IS NOT NULL"
        ):
            v = np.frombuffer(blob, dtype=np.float32)
            if person_id in sums:
                sums[person_id] += v
            else:
                sums[person_id] = v.copy()
        capacity = max(64, len(sums) * 2)
        self.sums = np.zeros((capacity, 128), np.float32)
        self.centroids = np.zeros((capacity, 128), np.float32)
        for person_id, total in sums.items():
            self._append(person_id, total)

    def _append(self, person_id: int, vector) -> None:
        np = self.np
        row = len(self.ids)
        if row == len(self.sums):
            self.sums = np.vstack([self.sums, np.zeros_like(self.sums)])
            self.centroids = np.vstack([self.centroids, np.zeros_like(self.centroids)])
        self.sums[row] = vector
        self._refresh(row)
        self.index[person_id] = row
        self.ids.append(person_id)

    def _refresh(self, row: int) -> None:
        norm = float(self.np.linalg.norm(self.sums[row]))
        self.centroids[row] = self.sums[row] / norm if norm else 0

    def match(self, embedding) -> int | None:
        if not self.ids:
            return None
        sims = self.centroids[: len(self.ids)] @ embedding
        best = int(sims.argmax())
        return self.ids[best] if float(sims[best]) >= self.threshold else None

    def add(self, person_id: int, embedding) -> None:
        row = self.index.get(person_id)
        if row is None:
            self._append(person_id, embedding)
        else:
            self.sums[row] += embedding
            self._refresh(row)

    def move(self, old: int | None, new: int, embedding) -> None:
        """A face changed person (a name from an import)."""
        row = self.index.get(old) if old is not None else None
        if row is not None:
            self.sums[row] -= embedding
            self._refresh(row)
        self.add(new, embedding)


def _overlap(a, b) -> float:
    """How much two boxes (x, y, w, h as fractions) cover each other: intersection over union."""
    x1, y1 = max(a[0], b[0]), max(a[1], b[1])
    x2, y2 = min(a[0] + a[2], b[0] + b[2]), min(a[1] + a[3], b[1] + b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _near(a, b) -> bool:
    """The middle of one box lies inside the other (face finders draw boxes of different sizes)."""
    ax, ay = a[0] + a[2] / 2, a[1] + a[3] / 2
    bx, by = b[0] + b[2] / 2, b[1] + b[3] / 2
    return (b[0] <= ax <= b[0] + b[2] and b[1] <= ay <= b[1] + b[3]) or \
        (a[0] <= bx <= a[0] + a[2] and a[1] <= by <= a[1] + a[3])


def apply_hints(conn, asset_id: int) -> list[tuple[int, int | None, int]]:
    """Give the faces in a photo the names an import brought along (from Immich or Google Photos).

    A name with a position goes to the face found there. A name without one (Google Photos only says who
    is in the photo) is used only when the photo has exactly one face and one name. The face's group gets
    the name if it has none yet, so the rest of that person's photos get it too; otherwise the face moves
    to the person with that name. Returns the faces that changed person: (face id, old person, new person).
    The hints are used up, whatever happens.
    """
    hints = conn.execute("SELECT name, x, y, w, h FROM face_hints WHERE asset_id = ?", (asset_id,)).fetchall()
    if not hints:
        return []
    conn.execute("DELETE FROM face_hints WHERE asset_id = ?", (asset_id,))
    faces = conn.execute("SELECT id, person_id, x, y, w, h FROM faces WHERE asset_id = ?", (asset_id,)).fetchall()
    if not faces:
        return []
    pairs = []
    used: set[int] = set()
    boxed = [h for h in hints if h["x"] is not None]
    for hint in boxed:
        box = (hint["x"], hint["y"], hint["w"], hint["h"])
        best, best_score = None, 0.0
        for face in faces:
            if face["id"] in used:
                continue
            fbox = (face["x"], face["y"], face["w"], face["h"])
            score = _overlap(box, fbox)
            if score < 0.2 and _near(box, fbox):
                score = 0.2
            if score > best_score:
                best, best_score = face, score
        if best is not None and best_score >= 0.2:
            used.add(best["id"])
            pairs.append((best, hint["name"]))
    unboxed = [h for h in hints if h["x"] is None]
    if not boxed and len(unboxed) == 1 and len(faces) == 1:
        pairs.append((faces[0], unboxed[0]["name"]))

    moves = []
    stamp = datetime.now().isoformat(timespec="seconds")
    for face, name in pairs:
        name = " ".join(name.split())[:200]
        if not name:
            continue
        named = conn.execute("SELECT id FROM people WHERE name = ? COLLATE NOCASE ORDER BY hidden, id LIMIT 1",
                             (name,)).fetchone()
        current = conn.execute("SELECT id, name FROM people WHERE id = ?", (face["person_id"],)).fetchone() \
            if face["person_id"] is not None else None
        if named is not None:
            target = named["id"]
        elif current is not None and not current["name"]:
            conn.execute("UPDATE people SET name = ? WHERE id = ?", (name, current["id"]))
            continue  # the whole group now has the name; the face stays where it is
        else:
            target = conn.execute("INSERT INTO people (name, created_at) VALUES (?, ?)", (name, stamp)).lastrowid
        if target != face["person_id"]:
            conn.execute("UPDATE faces SET person_id = ? WHERE id = ?", (target, face["id"]))
            moves.append((face["id"], face["person_id"], target))
    if moves:
        conn.execute("DELETE FROM people WHERE name IS NULL AND id NOT IN (SELECT person_id FROM faces"
                     " WHERE person_id IS NOT NULL)")
    return moves


def regroup(conn, threshold: float, min_faces: int = 2) -> int:
    """Merge groups that are the same person (average-linkage). Returns how many merges were made."""
    import numpy as np

    if threshold <= 0:
        return 0
    people = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM people WHERE hidden = 0")}
    sums: dict[int, "np.ndarray"] = {}
    counts: dict[int, int] = {}
    for person_id, blob in conn.execute("SELECT person_id, embedding FROM faces WHERE person_id IS NOT NULL"):
        if person_id not in people:
            continue
        v = np.frombuffer(blob, dtype=np.float32)
        if person_id in sums:
            sums[person_id] = sums[person_id] + v
            counts[person_id] += 1
        else:
            sums[person_id] = v.astype(np.float32)
            counts[person_id] = 1
    ids = [i for i in sums if counts[i] >= min_faces]
    if len(ids) < 2:
        return 0
    S = np.array([sums[i] for i in ids], dtype=np.float32)
    n = np.array([counts[i] for i in ids], dtype=np.float32)
    named = np.array([bool(people[i]) for i in ids])
    # Average similarity of every face in one group with every face in another: unit vectors make
    # that the dot product of the two sums, divided by both sizes.
    link = (S @ S.T) / np.outer(n, n)
    np.fill_diagonal(link, -1)
    link[np.outer(named, named)] = -1          # two names: the person decided, leave them
    pos = {pid: k for k, pid in enumerate(ids)}
    apart: dict[int, set[int]] = {}            # someone said these are different people
    for a, b in conn.execute("SELECT a, b FROM people_not_same"):
        if a in pos and b in pos:
            apart.setdefault(pos[a], set()).add(pos[b])
            apart.setdefault(pos[b], set()).add(pos[a])
            link[pos[a], pos[b]] = link[pos[b], pos[a]] = -1
    alive = np.ones(len(ids), bool)
    merges = 0
    stamp = datetime.now().isoformat(timespec="seconds")
    while True:
        best = int(link.argmax())
        a, b = divmod(best, len(ids))
        if link[a, b] < threshold:
            break
        # keep the named group, or else the larger one
        keep, gone = (a, b) if (named[a], n[a]) >= (named[b], n[b]) else (b, a)
        conn.execute("UPDATE faces SET person_id = ? WHERE person_id = ?", (ids[keep], ids[gone]))
        move_apart(conn, ids[gone], ids[keep])
        conn.execute("DELETE FROM people WHERE id = ?", (ids[gone],))
        log.info("merged person %s into %s (alike %.2f) at %s", ids[gone], ids[keep], float(link[a, b]), stamp)
        merges += 1
        S[keep] += S[gone]
        n[keep] += n[gone]
        alive[gone] = False
        row = (S @ S[keep]) / (n * n[keep])
        row[~alive] = -1
        row[keep] = -1
        if named[keep]:
            row[named] = -1
        far = apart.setdefault(keep, set())
        far |= apart.pop(gone, set())
        far.discard(keep)
        for j in far:
            row[j] = -1
            apart.setdefault(j, set()).add(keep)
        link[keep, :] = row
        link[:, keep] = row
        link[gone, :] = -1
        link[:, gone] = -1
    return merges


def move_apart(conn, gone: int, keep: int) -> None:
    """Before `gone` is merged into `keep`: whoever was "not the same person" as gone isn't keep either."""
    for (other,) in conn.execute("SELECT CASE WHEN a = ? THEN b ELSE a END FROM people_not_same WHERE a = ? OR b = ?",
                                 (gone, gone, gone)).fetchall():
        if other != keep:
            mark_apart(conn, keep, other)


def mark_apart(conn, a: int, b: int) -> None:
    if a != b:
        conn.execute("INSERT OR IGNORE INTO people_not_same (a, b) VALUES (?, ?)", (min(a, b), max(a, b)))


def _group_sums(conn):
    """Sum and count of the face embeddings of every visible person, from photos everyone can see."""
    import numpy as np

    sums, counts = {}, {}
    for person_id, blob in conn.execute(
        "SELECT f.person_id, f.embedding FROM faces f JOIN people p ON p.id = f.person_id AND p.hidden = 0"
        " JOIN assets a ON a.id = f.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
    ):
        v = np.frombuffer(blob, dtype=np.float32)
        if person_id in sums:
            sums[person_id] = sums[person_id] + v
            counts[person_id] += 1
        else:
            sums[person_id] = v.astype(np.float32)
            counts[person_id] = 1
    return sums, counts


def suggestions(conn, min_faces: int, threshold: float, limit: int = 200) -> list[dict]:
    """People who may be the same person, most alike first. Two kinds:
    * "merge": two listed people (named, or with at least min_faces faces) that look alike;
    * "is_this": a small unnamed group (too small to be listed) that looks like a listed person.
    Pairs with two different names, and pairs someone said are different people, are left out."""
    import numpy as np

    sums, counts = _group_sums(conn)
    if len(sums) < 2:
        return []
    names = {r[0]: r[1] for r in conn.execute("SELECT id, name FROM people WHERE hidden = 0")}
    apart = {(a, b) for a, b in conn.execute("SELECT a, b FROM people_not_same")}
    listed = [i for i in sums if names.get(i) or counts[i] >= min_faces]
    small = [i for i in sums if not names.get(i) and counts[i] < min_faces]
    if not listed:
        return []
    L = np.array([sums[i] for i in listed], dtype=np.float32)
    ln = np.array([counts[i] for i in listed], dtype=np.float32)
    out = []

    def same_name(a, b):
        return bool(names.get(a)) and bool(names.get(b)) and names[a].strip().lower() == names[b].strip().lower()

    link = (L @ L.T) / np.outer(ln, ln)
    for x in range(len(listed)):
        for y in range(x + 1, len(listed)):
            a, b = listed[x], listed[y]
            if (min(a, b), max(a, b)) in apart:
                continue
            both_named = bool(names.get(a)) and bool(names.get(b))
            if both_named and not same_name(a, b):
                continue
            score = float(link[x, y])
            if score >= threshold or same_name(a, b):
                out.append({"kind": "merge", "a": a, "b": b, "score": round(score, 3), "same_name": same_name(a, b)})
    for start in range(0, len(small), 2000):
        chunk = small[start:start + 2000]
        U = np.array([sums[i] for i in chunk], dtype=np.float32)
        un = np.array([counts[i] for i in chunk], dtype=np.float32)
        sim = (U @ L.T) / np.outer(un, ln)
        best = sim.argmax(axis=1)
        for k, pid in enumerate(chunk):
            j = int(best[k])
            score = float(sim[k, j])
            target = listed[j]
            if score >= threshold and (min(pid, target), max(pid, target)) not in apart:
                out.append({"kind": "is_this", "a": target, "b": pid, "score": round(score, 3), "same_name": False})
    # Name the bigger or named person first in a merge.
    for o in out:
        if o["kind"] == "merge":
            a, b = o["a"], o["b"]
            if (bool(names.get(b)), counts[b]) > (bool(names.get(a)), counts[a]):
                o["a"], o["b"] = b, a
    out.sort(key=lambda o: (not o["same_name"], -o["score"]))
    return out[:limit]


def recluster(conn, match_threshold: float, merge_threshold: float) -> int:
    """Group the faces of unnamed people again (after the strictness changed). Named and hidden people stay as
    they are, though faces may now join them. Returns how many unnamed groups there are afterwards."""
    import numpy as np

    rows = conn.execute(
        "SELECT f.id, f.embedding FROM faces f JOIN people p ON p.id = f.person_id"
        " WHERE p.name IS NULL AND p.hidden = 0 ORDER BY f.id").fetchall()
    conn.execute("DELETE FROM people WHERE name IS NULL AND hidden = 0")   # their faces lose their person
    clusterer = Clusterer(conn, match_threshold)
    stamp = datetime.now().isoformat(timespec="seconds")
    for face_id, blob in rows:
        emb = np.frombuffer(blob, dtype=np.float32)
        person_id = clusterer.match(emb)
        if person_id is None:
            person_id = conn.execute("INSERT INTO people (created_at) VALUES (?)", (stamp,)).lastrowid
        conn.execute("UPDATE faces SET person_id = ? WHERE id = ?", (person_id, face_id))
        clusterer.add(person_id, emb)
    regroup(conn, merge_threshold)
    return conn.execute("SELECT COUNT(*) FROM people WHERE name IS NULL AND hidden = 0").fetchone()[0]


class FacesJob:
    """Looks for faces in photos not yet checked. Run in small batches by the worker."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine: FaceEngine | None = None
        self.clusterer: Clusterer | None = None
        self.people_version = 0       # bumped whenever people are merged or faces reassigned
        self._clusterer_version = -1
        self.needs_regroup = True     # after new faces (and once after starting): look for split people
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}
        self.recluster_requested = False

    def invalidate(self) -> None:
        self.people_version += 1

    def pending(self, conn) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM assets WHERE faces_done = 0 AND trashed_at IS NULL"
        ).fetchone()[0]

    def unload(self) -> None:
        self.engine = None
        self.clusterer = None

    def run_batch(self, conn, now: float, should_stop, batch: int = 20) -> bool:
        if self.recluster_requested and available() is None:
            # Asked for from the settings: done here, in the worker, so it never races new faces.
            self.recluster_requested = False
            self.state["message"] = "Grouping faces again…"
            try:
                recluster(conn, self.cfg.face_match_threshold, self.cfg.face_merge_threshold)
                conn.commit()
            except Exception:
                conn.rollback()
                log.exception("could not group faces again")
            self.state["message"] = None
            self.clusterer = None
            self.invalidate()
            return True
        if now < self.retry_at:
            return False
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE faces_done = 0 AND trashed_at IS NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            if self.needs_regroup:            # first: people who are split in two get put back together
                self.needs_regroup = False
                try:
                    if regroup(conn, self.cfg.face_merge_threshold):
                        self.invalidate()
                    conn.commit()
                except Exception:
                    conn.rollback()
                    log.exception("could not regroup faces")
            if self._measure_older(conn, now, should_stop):
                return True
            self.state["running"] = False
            self.unload()
            return False
        root = self.cfg.library_path
        if root is None or not root.is_dir():
            return False
        if not self._load_engine(now):
            return False
        self.state["running"] = True

        for row in rows:
            if should_stop():
                break
            if self.clusterer is None or self._clusterer_version != self.people_version:
                self._clusterer_version = self.people_version
                self.clusterer = Clusterer(conn, self.cfg.face_match_threshold)
            try:
                found = self.engine.analyze(libraries.full_path(self.cfg, row["path"]) or root / "-missing-")
            except Exception as exc:  # unreadable or vanished: don't try it again and again
                log.info("no faces read from %s: %s", row["path"], exc)
                found = []
            try:
                self._store(conn, row["id"], found)
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()  # the photo was removed meanwhile
                self.clusterer = None
        self.state["pending"] = self.pending(conn)
        return True

    def _load_engine(self, now: float) -> bool:
        reason = available()
        if reason:
            self.state["message"] = reason
            self.retry_at = now + 24 * 3600
            return False
        if self.engine is None:
            try:
                if not all((self.cfg.model_dir / s["file"]).exists() for s in MODELS.values()):
                    self.state["message"] = "Downloading the face recognition models (about 39 MB)…"
                paths = ensure_models(self.cfg.model_dir)
                self.engine = FaceEngine(paths, self.cfg.face_threads)
                self.state["message"] = None
            except Exception as exc:
                log.warning("face models unavailable: %s", exc)
                self.state["message"] = "Could not download the face recognition models. Will try again in an hour."
                self.state["running"] = False
                self.retry_at = now + 3600
                return False
        return True

    def _measure_older(self, conn, now: float, should_stop, batch: int = 20) -> bool:
        """Faces found before eyes and sharpness were measured: measure them (once). True if any were."""
        rows = conn.execute(
            "SELECT a.id, a.path FROM assets a WHERE a.faceq_done = 0 AND a.faces_done = 1 AND a.trashed_at IS NULL"
            " LIMIT ?", (batch,)).fetchall()
        if not rows:
            return False
        self.state["running"] = True
        for row in rows:
            if should_stop():
                break
            faces = conn.execute("SELECT id, x, y, w, h FROM faces WHERE asset_id = ? ORDER BY id",
                                 (row["id"],)).fetchall()
            if not faces:                     # a video, or a photo without faces: nothing to measure
                conn.execute("UPDATE assets SET faceq_done = 1 WHERE id = ?", (row["id"],))
                conn.commit()
                continue
            if not self._load_engine(now):
                return False
            try:
                path = libraries.full_path(self.cfg, row["path"])
                found = self.engine.measure(path, [(f["x"], f["y"], f["w"], f["h"]) for f in faces]) if path else []
            except Exception as exc:
                log.info("could not measure the faces in %s: %s", row["path"], exc)
                found = []
            for f, q in zip(faces, found):
                if q:
                    conn.execute("UPDATE faces SET landmarks = ?, eyes = ?, sharp = ?, frontal = ? WHERE id = ?",
                                 (json.dumps(q["landmarks"]), q["eyes"], q["sharp"], q["frontal"], f["id"]))
            conn.execute("UPDATE assets SET faceq_done = 1 WHERE id = ?", (row["id"],))
            conn.commit()
        return True

    def _store(self, conn, asset_id: int, found: list[dict]) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        for face in found:
            emb = face["embedding"]
            person_id = self.clusterer.match(emb)
            if person_id is None:
                person_id = conn.execute("INSERT INTO people (created_at) VALUES (?)", (stamp,)).lastrowid
            face_id = conn.execute(
                "INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at,"
                " landmarks, eyes, sharp, frontal) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, person_id, face["x"], face["y"], face["w"], face["h"], face["score"],
                 emb.tobytes(), stamp, json.dumps(face["landmarks"]) if face.get("landmarks") else None,
                 face.get("eyes"), face.get("sharp"), face.get("frontal")),
            ).lastrowid
            self.clusterer.add(person_id, emb)
            self.needs_regroup = True
            try:
                face["thumb"].save(self.cfg.face_path(face_id), "JPEG", quality=85)
            except OSError:
                pass
        conn.execute("UPDATE assets SET faces_done = 1, faceq_done = 1 WHERE id = ?", (asset_id,))
        for face_id, old, new in apply_hints(conn, asset_id):
            blob = conn.execute("SELECT embedding FROM faces WHERE id = ?", (face_id,)).fetchone()[0]
            self.clusterer.move(old, new, self.clusterer.np.frombuffer(blob, dtype=self.clusterer.np.float32))
