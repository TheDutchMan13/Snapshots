"""Search by what is in a photo ("dog on the beach"), on this computer.

Uses OpenAI's CLIP (ViT-B/32, MIT licence), converted to ONNX and 8-bit quantized by Xenova
(https://huggingface.co/Xenova/clip-vit-base-patch32). About 154 MB, downloaded once and checked
against known SHA-256 hashes.

CLIP turns a picture into 512 numbers, and a sentence into 512 numbers, so that a picture and a
sentence describing it point the same way. Every photo is turned into numbers once, in the
background (from its thumbnail, so it is quick); a search then compares the typed words with all
photos at once.
"""

import html
import json
import logging
import re
import sqlite3
import threading
from functools import lru_cache
from pathlib import Path

from . import downloads

log = logging.getLogger("snapshots.clip")

_HF = "https://huggingface.co/Xenova/clip-vit-base-patch32/resolve/main/"
FILES = {
    "clip_vision_int8.onnx": {
        "sha256": "583fd1110a514667812fee7d684952aaf82a99b959760c8d7dca7e0ab9839299",
        "urls": [_HF + "onnx/vision_model_quantized.onnx"],
    },
    "clip_text_int8.onnx": {
        "sha256": "73baab855d406190da9faa498cfedf65f15cf309f4cc7385b7b032e6d08e5c3a",
        "urls": [_HF + "onnx/text_model_quantized.onnx"],
    },
    "clip_vocab.json": {
        "sha256": "5047b556ce86ccaf6aa22b3ffccfc52d391ea4accdab9c2f2407da5b742d4363",
        "urls": [_HF + "vocab.json"],
    },
    "clip_merges.txt": {
        "sha256": "9fd691f7c8039210e0fced15865466c65820d09b63988b0174bfe25de299051a",
        "urls": [_HF + "merges.txt"],
    },
}
DOWNLOAD_MB = 154

DIM = 512
SIZE = 224
MEAN = (0.48145466, 0.4578275, 0.40821073)
STD = (0.26862954, 0.26130258, 0.27577711)


def available() -> str | None:
    """None if content search can run here, otherwise the reason it can't."""
    try:
        import numpy  # noqa: F401
        import onnxruntime  # noqa: F401
    except Exception as exc:  # pragma: no cover - depends on the install
        return f"Search by content needs onnxruntime and NumPy ({exc})."
    return None


# ---------------------------------------------------------------- the tokenizer

def _bytes_to_unicode() -> dict[int, str]:
    """CLIP's (and GPT-2's) reversible mapping from bytes to printable characters."""
    bs = list(range(ord("!"), ord("~") + 1)) + list(range(ord("¡"), ord("¬") + 1)) + list(range(ord("®"), ord("ÿ") + 1))
    cs = bs[:]
    n = 0
    for b in range(256):
        if b not in bs:
            bs.append(b)
            cs.append(256 + n)
            n += 1
    return dict(zip(bs, map(chr, cs)))


class Tokenizer:
    """CLIP's byte-pair tokenizer, enough for search words (lower-cased, max 77 tokens)."""

    _SPLIT = re.compile(r"'s|'t|'re|'ve|'m|'ll|'d|[^\W\d_]+|\d|(?:[^\s\w]|_)+", re.IGNORECASE)
    MAX_TOKENS = 77

    def __init__(self, vocab_path: Path, merges_path: Path):
        self.encoder: dict[str, int] = json.loads(Path(vocab_path).read_text(encoding="utf-8"))
        lines = Path(merges_path).read_text(encoding="utf-8").split("\n")
        merges = [tuple(line.split()) for line in lines[1:] if line.strip() and not line.startswith("#")]
        self.ranks = {pair: i for i, pair in enumerate(merges)}
        self.byte_encoder = _bytes_to_unicode()
        self.bos = self.encoder["<|startoftext|>"]
        self.eos = self.encoder["<|endoftext|>"]
        self._cache: dict[str, list[str]] = {}

    def _bpe(self, token: str) -> list[str]:
        if token in self._cache:
            return self._cache[token]
        word = list(token[:-1]) + [token[-1] + "</w>"]
        while len(word) > 1:
            pairs = {(word[i], word[i + 1]) for i in range(len(word) - 1)}
            best = min(pairs, key=lambda p: self.ranks.get(p, float("inf")))
            if best not in self.ranks:
                break
            first, second = best
            merged, i = [], 0
            while i < len(word):
                if i < len(word) - 1 and word[i] == first and word[i + 1] == second:
                    merged.append(first + second)
                    i += 2
                else:
                    merged.append(word[i])
                    i += 1
            word = merged
        if len(self._cache) < 10000:
            self._cache[token] = word
        return word

    def encode(self, text: str) -> list[int]:
        text = html.unescape(html.unescape(text))
        text = re.sub(r"\s+", " ", text).strip().lower()
        ids = [self.bos]
        for piece in self._SPLIT.findall(text):
            piece = "".join(self.byte_encoder[b] for b in piece.encode("utf-8"))
            ids.extend(self.encoder[t] for t in self._bpe(piece) if t in self.encoder)
        return ids[: self.MAX_TOKENS - 1] + [self.eos]


# ---------------------------------------------------------------- the model

def preprocess(img):
    """A PIL image -> 1x3x224x224 float32, the way CLIP was trained (shortest side 224, centre crop)."""
    import numpy as np
    from PIL import Image

    img = img.convert("RGB")
    w, h = img.size
    scale = SIZE / min(w, h)
    img = img.resize((max(SIZE, round(w * scale)), max(SIZE, round(h * scale))), Image.BICUBIC)
    left = (img.width - SIZE) // 2
    top = (img.height - SIZE) // 2
    img = img.crop((left, top, left + SIZE, top + SIZE))
    x = np.asarray(img, dtype=np.float32) / 255.0
    x = (x - np.array(MEAN, np.float32)) / np.array(STD, np.float32)
    return x.transpose(2, 0, 1)[None].astype(np.float32)


class ClipEngine:
    def __init__(self, paths: dict[str, Path], threads: int = 2):
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(threads))
        opts.inter_op_num_threads = 1
        opts.log_severity_level = 3
        providers = ["CPUExecutionProvider"]
        self.vision = ort.InferenceSession(str(paths["clip_vision_int8.onnx"]), opts, providers=providers)
        self.text = ort.InferenceSession(str(paths["clip_text_int8.onnx"]), opts, providers=providers)
        self.tokenizer = Tokenizer(paths["clip_vocab.json"], paths["clip_merges.txt"])
        self._lock = threading.Lock()  # the text model is used by web requests, one at a time

    @staticmethod
    def _unit(v):
        import numpy as np

        v = np.asarray(v, dtype=np.float32).reshape(-1)
        n = float(np.linalg.norm(v))
        return v / n if n else v

    def embed_image(self, img):
        out = self.vision.run(None, {"pixel_values": preprocess(img)})[0]
        return self._unit(out[0])

    def embed_text(self, text: str):
        import numpy as np

        ids = np.array([self.tokenizer.encode(text)], dtype=np.int64)
        with self._lock:
            out = self.text.run(None, {"input_ids": ids})[0]
        return self._unit(out[0])


# ---------------------------------------------------------------- searching

# Matches are listed best first. A photo is listed if it stands out for these words: its score is at
# least SPREAD standard deviations above the average over the whole library, and within RELATIVE of the
# best match (and never below MIN_SCORE). Measured on 400 captioned Flickr30k photos: the right photo
# was the best match for 57% of captions and in the top 5 for 84%. For words like "dog" or "beach" this
# cutoff listed about half the photos showing it (best first) with about half of the listed photos
# correct by the captions (which often leave things out); a looser one (best - 0.05) found 76% but
# listed twice as many photos, mostly wrong.
MIN_SCORE = 0.18
RELATIVE = 0.06
SPREAD = 1.5
MAX_RESULTS = 500


class VectorIndex:
    """All photo vectors in memory (float16, 1 KB per photo), reloaded when the library changes."""

    def __init__(self):
        self._lock = threading.Lock()
        self._key = None
        self.ids = None
        self.matrix = None

    def _version(self, conn):
        return tuple(conn.execute("SELECT COUNT(*), COALESCE(MAX(rowid), 0), COALESCE(SUM(asset_id), 0)"
                                  " FROM clip_vectors").fetchone())

    def load(self, conn) -> int:
        import numpy as np

        key = self._version(conn)
        with self._lock:
            if key != self._key:
                rows = conn.execute("SELECT asset_id, vec FROM clip_vectors ORDER BY asset_id").fetchall()
                self.ids = np.array([r[0] for r in rows], dtype=np.int64)
                self.matrix = (np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(-1, DIM)
                               if rows else np.zeros((0, DIM), np.float16))
                self._key = key
            return len(self.ids)

    def search(self, conn, text_vector, min_score=MIN_SCORE, relative=RELATIVE, spread=SPREAD, limit=MAX_RESULTS):
        """[(asset_id, score)] best first."""
        import numpy as np

        if not self.load(conn):
            return []
        with self._lock:
            ids, matrix = self.ids, self.matrix
        scores = matrix.astype(np.float32) @ text_vector.astype(np.float32)
        best = float(scores.max())
        cutoff = max(min_score, best - relative, float(scores.mean() + spread * scores.std()))
        keep = np.where(scores >= cutoff)[0]
        keep = keep[np.argsort(-scores[keep])][:limit]
        return [(int(ids[i]), float(scores[i])) for i in keep]


class ClipJob:
    """Turns photos (and videos, from their thumbnail) into CLIP vectors, in small batches."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.engine: ClipEngine | None = None
        self.index = VectorIndex()
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}
        self._load_lock = threading.Lock()

    @property
    def folder(self) -> Path:
        return self.cfg.model_dir / "clip"

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE clip_done = 0 AND trashed_at IS NULL").fetchone()[0]

    def load_engine(self) -> ClipEngine:
        with self._load_lock:
            if self.engine is None:
                if downloads.missing(self.folder, FILES):
                    self.state["message"] = f"Downloading the search-by-content model (about {DOWNLOAD_MB} MB)…"
                paths = downloads.ensure_files(self.folder, FILES)
                self.engine = ClipEngine(paths, self.cfg.search_threads)
                self.state["message"] = None
            return self.engine

    def ready(self) -> bool:
        """Can searches use content right now? (The model is loaded, or is on disk to be loaded.)"""
        if not self.cfg.smart_search or available():
            return False
        return self.engine is not None or not downloads.missing(self.folder, FILES)

    def run_batch(self, conn, now: float, should_stop, batch: int = 32) -> bool:
        if now < self.retry_at:
            return False
        rows = conn.execute(
            "SELECT id FROM assets WHERE clip_done = 0 AND trashed_at IS NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            self.state["running"] = False
            return False
        reason = available()
        if reason:
            self.state["message"] = reason
            self.retry_at = now + 24 * 3600
            return False
        self.state["running"] = True
        try:
            engine = self.load_engine()
        except Exception as exc:
            log.warning("content search model unavailable: %s", exc)
            self.state.update(running=False, message="Could not download the search-by-content model. "
                                                      "Will try again in an hour.")
            self.retry_at = now + 3600
            return False

        from PIL import Image

        for row in rows:
            if should_stop():
                break
            vec = None
            try:
                with Image.open(self.cfg.thumb_path(row["id"])) as img:
                    vec = engine.embed_image(img)
            except Exception as exc:  # no thumbnail (unreadable file): nothing to search on
                log.info("no content vector for asset %s: %s", row["id"], exc)
            try:
                if vec is not None:
                    conn.execute("INSERT OR REPLACE INTO clip_vectors (asset_id, vec) VALUES (?, ?)",
                                 (row["id"], vec.astype("float16").tobytes()))
                conn.execute("UPDATE assets SET clip_done = 1 WHERE id = ?", (row["id"],))
                conn.commit()
            except sqlite3.IntegrityError:
                conn.rollback()  # the photo was removed meanwhile
        self.state["pending"] = self.pending(conn)
        return True

    def search(self, conn, text: str) -> list[tuple[int, float]]:
        """Photos showing `text`, best first. Empty if content search isn't ready."""
        if not self.ready():
            return []
        try:
            vector = _cached_text_vector(self.load_engine(), text.strip().lower())
        except Exception as exc:
            log.warning("content search failed: %s", exc)
            return []
        return self.index.search(conn, vector)


@lru_cache(maxsize=256)
def _cached_text_vector(engine: ClipEngine, text: str):
    return engine.embed_text(text)
