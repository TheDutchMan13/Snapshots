"""Finding text in photos (signs, screenshots, receipts, documents), so it can be searched.

Uses PaddleOCR's English models (PP-OCRv3, Apache 2.0 licence), converted to ONNX by the RapidOCR
project (https://huggingface.co/SWHL/RapidOCR). About 11 MB, downloaded once and checked against
known SHA-256 hashes. Runs on this computer.

Two steps: a detector marks where lines of text are, then a recogniser reads each line.
"""

import logging
import math
import re
import sqlite3
from pathlib import Path

from . import downloads, media

log = logging.getLogger("snapshots.ocr")

_HF = "https://huggingface.co/SWHL/RapidOCR/resolve/main/"
FILES = {
    "ocr_detect_en_v3.onnx": {
        "sha256": "f139598bc2af4e4b6fe98dec11574e30edfdd91fc94ac1425c18ace3bd5a866b",
        "urls": [_HF + "PP-OCRv4/en_PP-OCRv3_det_infer.onnx"],
    },
    "ocr_read_en_v3.onnx": {
        "sha256": "ef7abd8bd3629ae57ea2c28b425c1bd258a871b93fd2fe7c433946ade9b5d9ea",
        "urls": [_HF + "PP-OCRv3/en_PP-OCRv3_rec_infer.onnx"],
    },
}
DOWNLOAD_MB = 11

LOAD_SIDE = 1600        # photos are read at this size; lines of text are cut out of it
DETECT_SIDE = 960       # ... and shrunk to this to look for text (PaddleOCR's default)
DETECT_PIXEL = 0.3      # a pixel is "text" above this probability
DETECT_BOX = 0.6        # a line is kept if its average probability is at least this
UNCLIP = 1.6            # how much to grow each detected line (the detector finds the core of the text)
MIN_BOX_PX = 5
MAX_LINES = 60          # per photo; more is a page of small print, the first lines are enough to search
READ_HEIGHT = 48
MIN_CONFIDENCE = 0.75   # lines read with less confidence than this are dropped
MAX_TEXT = 2000         # characters stored per photo


def available() -> str | None:
    try:
        import cv2  # noqa: F401
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the install
        return f"Text search needs onnxruntime, OpenCV and NumPy ({exc})."
    return None


class OcrEngine:
    def __init__(self, paths: dict[str, Path], threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self.detector = ort.InferenceSession(str(paths["ocr_detect_en_v3.onnx"]), opts, providers=providers)
        self.reader = ort.InferenceSession(str(paths["ocr_read_en_v3.onnx"]), opts, providers=providers)
        chars = self.reader.get_modelmeta().custom_metadata_map["character"].split("\n")
        while chars and chars[-1] == "":
            chars.pop()
        # index 0 is the "nothing here" symbol; PaddleOCR adds a space as the last symbol
        self.chars = [""] + chars + [" "]

    # ------------------------------------------------------------ finding lines of text

    def detect(self, bgr):
        """Four-corner boxes (float32 4x2, in bgr's pixels) around lines of text, top to bottom."""
        import cv2
        import numpy as np

        h, w = bgr.shape[:2]
        scale = min(1.0, DETECT_SIDE / max(h, w))
        rh = max(32, int(round(h * scale / 32)) * 32)
        rw = max(32, int(round(w * scale / 32)) * 32)
        small = cv2.resize(bgr, (rw, rh))
        x = (small.astype(np.float32) / 255.0 - np.array([0.485, 0.456, 0.406], np.float32)) \
            / np.array([0.229, 0.224, 0.225], np.float32)
        prob = self.detector.run(None, {"x": x.transpose(2, 0, 1)[None]})[0][0, 0]

        mask = (prob > DETECT_PIXEL).astype(np.uint8)
        contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
        boxes = []
        for contour in contours[:1000]:
            if len(contour) < 4:
                continue
            (cx, cy), (bw, bh), angle = cv2.minAreaRect(contour)
            if min(bw, bh) < 3:
                continue
            # average probability inside the contour
            x0, y0, cw, ch = cv2.boundingRect(contour)
            local = np.zeros((ch, cw), np.uint8)
            cv2.fillPoly(local, [contour.reshape(-1, 2) - [x0, y0]], 1)
            score = float(prob[y0:y0 + ch, x0:x0 + cw][local.astype(bool)].mean())
            if score < DETECT_BOX:
                continue
            grow = bw * bh * UNCLIP / (2 * (bw + bh))
            bw, bh = bw + 2 * grow, bh + 2 * grow
            if min(bw, bh) < MIN_BOX_PX:
                continue
            pts = cv2.boxPoints(((cx, cy), (bw, bh), angle))
            pts[:, 0] = np.clip(pts[:, 0] * w / rw, 0, w - 1)
            pts[:, 1] = np.clip(pts[:, 1] * h / rh, 0, h - 1)
            boxes.append(_order(pts))
        boxes.sort(key=lambda b: (round(b[:, 1].min() / 10), b[:, 0].min()))
        return boxes[:MAX_LINES]

    # ------------------------------------------------------------ reading them

    def read(self, bgr, boxes) -> list[tuple[str, float]]:
        import cv2
        import numpy as np

        crops = []
        for b in boxes:
            cw = int(max(np.linalg.norm(b[0] - b[1]), np.linalg.norm(b[2] - b[3])))
            ch = int(max(np.linalg.norm(b[0] - b[3]), np.linalg.norm(b[1] - b[2])))
            if cw < 2 or ch < 2:
                continue
            target = np.array([[0, 0], [cw, 0], [cw, ch], [0, ch]], np.float32)
            crop = cv2.warpPerspective(bgr, cv2.getPerspectiveTransform(b.astype(np.float32), target), (cw, ch),
                                       borderMode=cv2.BORDER_REPLICATE, flags=cv2.INTER_CUBIC)
            if ch / cw >= 1.5:  # a vertical line of text: turn it to read across
                crop = np.rot90(crop)
            crops.append(crop)
        results = []
        order = sorted(range(len(crops)), key=lambda i: crops[i].shape[1] / crops[i].shape[0])
        texts: dict[int, tuple[str, float]] = {}
        for start in range(0, len(order), 8):
            batch = order[start:start + 8]
            ratio = max(crops[i].shape[1] / crops[i].shape[0] for i in batch)
            width = int(min(max(READ_HEIGHT * 4, math.ceil(READ_HEIGHT * ratio)), READ_HEIGHT * 40))
            x = np.zeros((len(batch), 3, READ_HEIGHT, width), np.float32)
            for k, i in enumerate(batch):
                c = crops[i]
                cw = min(width, max(1, int(math.ceil(READ_HEIGHT * c.shape[1] / c.shape[0]))))
                r = cv2.resize(c, (cw, READ_HEIGHT)).astype(np.float32) / 255.0
                x[k, :, :, :cw] = ((r - 0.5) / 0.5).transpose(2, 0, 1)
            out = self.reader.run(None, {"x": x})[0]
            for k, i in enumerate(batch):
                texts[i] = self._decode(out[k])
        for i in range(len(crops)):
            results.append(texts[i])
        return results

    def _decode(self, probs) -> tuple[str, float]:
        idx = probs.argmax(axis=1)
        best = probs.max(axis=1)
        chars, confs, prev = [], [], -1
        for t, c in enumerate(idx):
            if c != prev and c != 0 and c < len(self.chars):
                chars.append(self.chars[c])
                confs.append(float(best[t]))
            prev = c
        text = _fix_common_mistakes("".join(chars).strip())
        return text, (sum(confs) / len(confs) if confs else 0.0)

    def text_in(self, img) -> str:
        """All text found in a PIL image, one line per line of text."""
        import numpy as np

        rgb = np.asarray(img.convert("RGB"))
        bgr = np.ascontiguousarray(rgb[:, :, ::-1])
        boxes = self.detect(bgr)
        if not boxes:
            return ""
        lines = [t for t, conf in self.read(bgr, boxes) if conf >= MIN_CONFIDENCE and _worth_keeping(t)]
        return "\n".join(lines)[:MAX_TEXT]


def _order(pts):
    """Corners as top-left, top-right, bottom-right, bottom-left."""
    import numpy as np

    pts = pts[np.argsort(pts[:, 0])]
    left, right = pts[:2], pts[2:]
    tl, bl = left[np.argsort(left[:, 1])]
    tr, br = right[np.argsort(right[:, 1])]
    return np.array([tl, tr, br, bl], np.float32)


_CAPITAL_I_FOR_L = re.compile(r"(?<=\b[A-Z])I(?=[a-z]{2})")


def _fix_common_mistakes(text: str) -> str:
    """The reader often takes a small l for a capital I inside a word ("FIagstaff"): put it right."""
    return _CAPITAL_I_FOR_L.sub("l", text)


def _worth_keeping(text: str) -> bool:
    """Drop what is almost certainly noise: very short, or no letters or digits worth searching."""
    alnum = sum(ch.isalnum() for ch in text)
    return alnum >= 2 and alnum >= len(text.replace(" ", "")) * 0.5


class OcrJob:
    """Reads text in photos, in small batches, after everything else is done."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine: OcrEngine | None = None
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}

    @property
    def folder(self) -> Path:
        return self.cfg.model_dir / "ocr"

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE ocr_done = 0 AND trashed_at IS NULL").fetchone()[0]

    def run_batch(self, conn, now: float, should_stop, batch: int = 10) -> bool:
        if now < self.retry_at:
            return False
        rows = conn.execute(
            "SELECT id, path, kind FROM assets WHERE ocr_done = 0 AND trashed_at IS NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            self.state["running"] = False
            self.engine = None  # free the memory until there is new work
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
                if downloads.missing(self.folder, FILES):
                    self.state["message"] = f"Downloading the text-reading model (about {DOWNLOAD_MB} MB)…"
                self.engine = OcrEngine(downloads.ensure_files(self.folder, FILES), self.cfg.search_threads)
                self.state["message"] = None
            except Exception as exc:
                log.warning("text reading model unavailable: %s", exc)
                self.state.update(running=False, message="Could not download the text-reading model. "
                                                          "Will try again in an hour.")
                self.retry_at = now + 3600
                return False

        for row in rows:
            if should_stop():
                break
            text = None
            if row["kind"] == "photo":
                try:
                    text = self.engine.text_in(media.load_oriented(root / row["path"], LOAD_SIDE)) or None
                except Exception as exc:
                    log.info("no text read from %s: %s", row["path"], exc)
            try:
                conn.execute("UPDATE assets SET ocr_text = ?, ocr_done = 1 WHERE id = ?", (text, row["id"]))
                conn.commit()
            except sqlite3.Error:
                conn.rollback()
        self.state["pending"] = self.pending(conn)
        return True

