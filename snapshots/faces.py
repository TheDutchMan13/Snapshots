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
"""

import hashlib
import logging
import os
import sqlite3
import urllib.request
from datetime import datetime
from pathlib import Path

from . import media

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
        for row in found:
            x, y, fw, fh = (float(v) for v in row[:4])
            if fw < MIN_FACE_PX or fh < MIN_FACE_PX:
                continue
            emb = self.recognizer.feature(self.recognizer.alignCrop(bgr, row)).flatten().astype(np.float32)
            norm = float(np.linalg.norm(emb))
            if not norm:
                continue
            x0, y0 = max(0.0, x), max(0.0, y)
            faces.append({
                "x": x0 / w, "y": y0 / h,
                "w": (min(w, x + fw) - x0) / w, "h": (min(h, y + fh) - y0) / h,
                "score": float(row[14]),
                "embedding": emb / norm,
                "thumb": _face_picture(img, x, y, fw, fh),
            })
        return faces


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


class FacesJob:
    """Looks for faces in photos not yet checked. Run in small batches by the worker."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine: FaceEngine | None = None
        self.clusterer: Clusterer | None = None
        self.people_version = 0       # bumped whenever people are merged or faces reassigned
        self._clusterer_version = -1
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}

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
        if now < self.retry_at:
            return False
        rows = conn.execute(
            "SELECT id, path FROM assets WHERE faces_done = 0 AND trashed_at IS NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            self.state["running"] = False
            self.unload()
            return False
        root = self.cfg.library_path
        if root is None or not root.is_dir():
            return False

        reason = available()
        if reason:
            self.state["message"] = reason
            self.retry_at = now + 24 * 3600
            return False
        self.state["running"] = True
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

        for row in rows:
            if should_stop():
                break
            if self.clusterer is None or self._clusterer_version != self.people_version:
                self._clusterer_version = self.people_version
                self.clusterer = Clusterer(conn, self.cfg.face_match_threshold)
            try:
                found = self.engine.analyze(root / row["path"])
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

    def _store(self, conn, asset_id: int, found: list[dict]) -> None:
        stamp = datetime.now().isoformat(timespec="seconds")
        for face in found:
            emb = face["embedding"]
            person_id = self.clusterer.match(emb)
            if person_id is None:
                person_id = conn.execute("INSERT INTO people (created_at) VALUES (?)", (stamp,)).lastrowid
            face_id = conn.execute(
                "INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (asset_id, person_id, face["x"], face["y"], face["w"], face["h"], face["score"],
                 emb.tobytes(), stamp),
            ).lastrowid
            self.clusterer.add(person_id, emb)
            try:
                face["thumb"].save(self.cfg.face_path(face_id), "JPEG", quality=85)
            except OSError:
                pass
        conn.execute("UPDATE assets SET faces_done = 1 WHERE id = ?", (asset_id,))
