"""Sorting photos: screenshots, documents and receipts apart from ordinary photos, and how good each looks.

Both use the content vectors from search by content (clipsearch) when that is switched on, compared with
short descriptions ("a screenshot of a phone screen", "a beautiful photo"). This is CLIP's zero-shot
classification (Radford et al., 2021): the description a picture's vector is closest to is what it most
likely shows. Without the model, screenshots are still found by their name and shape, and receipts and
documents by the text read in them.

* category: 'screenshot', 'document' or 'receipt', or None for a photo. Someone can change it by hand,
  and then it stays.
* beauty: how much more a photo looks like the "good photo" descriptions than the "bad photo" ones. The
  numbers are small (about -0.05 to 0.05); only their order matters. It picks covers, memories, the year
  in review and Highlights. Judging photos this way agrees only roughly with people (Hentschel, Kobs and
  Hotho, "CLIP knows image aesthetics", 2022), so it is used to choose among photos, never to hide any.
"""

import logging
import re
import threading

log = logging.getLogger("snapshots.sorting")

CATEGORIES = ("screenshot", "document", "receipt")

# ---------------------------------------------------------------- what the descriptions are

CLASS_PROMPTS = {
    "photo": [
        "a photo of people", "a photo of a family", "a photo of a place", "a photo of a landscape",
        "a photo of an animal", "a photo of food on a plate", "a photo of a building", "a photo of an object",
        "a selfie", "a photo of a street",
    ],
    "screenshot": [
        "a screenshot of a phone screen", "a screenshot of an app", "a screenshot of a website",
        "a screenshot of a computer screen", "a screenshot of a text message conversation",
        "a screenshot of a social media post",
    ],
    "document": [
        "a photo of a document", "a scanned page of printed text", "a photo of a printed form",
        "a photo of a page in a book", "a photo of handwritten notes", "a photo of a whiteboard with writing",
    ],
    "receipt": [
        "a photo of a receipt", "a photo of a shopping receipt", "a photo of a restaurant bill",
        "a photo of an invoice",
    ],
}
GOOD = ["a beautiful photo", "a stunning, professional photograph", "an award-winning photo",
        "a high quality photo with great composition", "a breathtaking photo", "a lovely, well lit photo"]
BAD = ["a blurry photo", "a bad photo", "a boring, low quality snapshot", "an out of focus picture",
       "a dark, underexposed photo", "a photo taken by accident"]
LOGIT_SCALE = 100.0   # CLIP's own temperature
SURE = 0.70           # a category needs at least this share (after softmax over the four)

# ---------------------------------------------------------------- without the model

_SCREENSHOT_NAME = re.compile(r"screen[\s_\-]?shot|screen[\s_\-]?cap|scrnli|screenrecord|snip|captura|bildschirmfoto",
                              re.IGNORECASE)
_PRICE = re.compile(r"\$?\b\d{1,5}[.,]\d{2}\b")
_RECEIPT_WORDS = re.compile(r"\b(sub\s?total|total|tax|cash|change|visa|mastercard|amex|debit|credit|receipt|"
                            r"qty|amount|balance|tip|gratuity|thank you for (shopping|your purchase))\b", re.IGNORECASE)
# Common screen sizes (width x height in either order), for pictures that came from no camera.
_SCREEN_RATIOS = (16 / 9, 16 / 10, 4 / 3, 19.5 / 9, 20 / 9, 21 / 9, 18 / 9, 19 / 9, 3 / 2)


def _screen_shaped(w, h) -> bool:
    if not w or not h:
        return False
    r = max(w, h) / min(w, h)
    return any(abs(r - s) < 0.02 for s in _SCREEN_RATIOS) and max(w, h) >= 800


def text_signals(text: str | None) -> dict:
    text = text or ""
    return {"chars": len(text.strip()), "prices": len(_PRICE.findall(text)),
            "receipt_words": len({m.group(1).lower() for m in _RECEIPT_WORDS.finditer(text)})}


def classify(*, filename: str, camera: str | None, width: int | None, height: int | None,
             text: str | None = None, probs: dict[str, float] | None = None) -> str | None:
    """The category of one picture. probs: the softmax over CLASS_PROMPTS, when the model is there."""
    name = filename or ""
    ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    t = text_signals(text)
    no_camera = not (camera or "").strip()
    if no_camera and _SCREENSHOT_NAME.search(name):
        return "screenshot"
    receipt_by_text = t["prices"] >= 3 and t["receipt_words"] >= 2
    document_by_text = t["chars"] >= 400
    if probs:
        best = max(probs, key=probs.get)
        if best == "screenshot" and not no_camera:
            # A camera took it, so it is a photo of a screen at most: count it with the photos.
            probs = {k: v for k, v in probs.items() if k != "screenshot"}
            best = max(probs, key=probs.get)
        if best == "receipt" and (probs["receipt"] >= SURE or receipt_by_text):
            return "receipt"
        if best == "document" and (probs["document"] >= SURE or document_by_text):
            return "receipt" if receipt_by_text else "document"
        if best == "screenshot" and (probs["screenshot"] >= SURE or (ext == "png" and _screen_shaped(width, height))):
            return "screenshot"
        if best == "photo":
            # The text can still say it is a receipt, if the model was nearly as sure.
            if receipt_by_text and probs.get("receipt", 0) + probs.get("document", 0) >= 0.4:
                return "receipt"
        return None
    # No model: only what is certain enough.
    if no_camera and ext == "png" and _screen_shaped(width, height):
        return "screenshot"
    if receipt_by_text:
        return "receipt"
    if document_by_text and no_camera:
        return "document"
    return None


# ---------------------------------------------------------------- the job

class Prompts:
    """The description vectors, made once with the content search model."""

    def __init__(self, engine):
        import numpy as np

        def mean(texts):
            v = np.mean([engine.embed_text(t) for t in texts], axis=0)
            return v / np.linalg.norm(v)

        self.classes = list(CLASS_PROMPTS)
        self.class_vecs = [np.stack([engine.embed_text(t) for t in CLASS_PROMPTS[c]]) for c in self.classes]
        self.good = mean(GOOD)
        self.bad = mean(BAD)

    def probs(self, vec) -> dict[str, float]:
        import numpy as np

        best = np.array([float((m @ vec).max()) for m in self.class_vecs])
        e = np.exp((best - best.max()) * LOGIT_SCALE)
        p = e / e.sum()
        return {c: float(x) for c, x in zip(self.classes, p)}

    def beauty(self, vec) -> float:
        return float(vec @ self.good - vec @ self.bad)


class SortJob:
    """Sorts photos (category and beauty) in batches. Each is sorted again when more is known about it."""

    def __init__(self, cfg, clip):
        self.cfg = cfg
        self.clip = clip              # the ClipJob, for its model
        self.prompts: Prompts | None = None
        self.retry_at = 0.0
        self._wait_until = 0.0
        self.state = {"running": False, "pending": 0, "message": None}
        self._lock = threading.Lock()

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE sort_done = 0 AND trashed_at IS NULL").fetchone()[0]

    def _prompts(self):
        if not self.cfg.smart_search or not self.clip.ready():
            return None
        with self._lock:
            if self.prompts is None:
                try:
                    self.prompts = Prompts(self.clip.load_engine())
                except Exception as exc:
                    log.warning("could not prepare the sorting descriptions: %s", exc)
                    return None
            return self.prompts

    def _model_expected(self) -> bool:
        """Content search is on and can run here (its model may still be downloading)."""
        from .clipsearch import available

        return bool(self.cfg.smart_search) and available() is None

    def run_batch(self, conn, now: float, should_stop, batch: int = 200) -> bool:
        if now < self.retry_at:
            return False
        # While the model is expected but can't be used yet, photos with a content vector wait (so they
        # get their beauty and a better category later); the others are sorted now.
        waiting = now < self._wait_until
        rows = conn.execute(
            "SELECT a.id, a.kind, a.filename, a.camera, a.width, a.height, a.ocr_text, a.category_hand, c.vec"
            " FROM assets a LEFT JOIN clip_vectors c ON c.asset_id = a.id"
            " WHERE a.sort_done = 0 AND a.trashed_at IS NULL"
            + (" AND c.asset_id IS NULL" if waiting else "") + " LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state.update(running=False, pending=0 if not waiting else self.pending(conn))
            return False
        import numpy as np

        self.state["running"] = True
        with_vec = any(r["vec"] for r in rows)
        prompts = self._prompts() if with_vec and self._model_expected() else None
        if with_vec and prompts is None and self._model_expected():
            self._wait_until = now + 600
            rows = [r for r in rows if not r["vec"]]
            if not rows:
                return True
        for r in rows:
            if should_stop():
                break
            category, beauty = None, None
            vec = None
            if r["vec"] and prompts is not None:
                vec = np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
                n = float(np.linalg.norm(vec))
                vec = vec / n if n else vec
            if r["kind"] == "photo":
                category = classify(filename=r["filename"], camera=r["camera"], width=r["width"], height=r["height"],
                                    text=r["ocr_text"], probs=prompts.probs(vec) if vec is not None else None)
            if vec is not None and category is None:
                beauty = round(prompts.beauty(vec), 5)
            # Someone may set the category by hand meanwhile (the descriptions take a while the first
            # time): theirs stays, and a picture they called a screenshot gets no beauty.
            conn.execute("UPDATE assets SET category = ?, beauty = ?, sort_done = 1 WHERE id = ? AND category_hand = 0",
                         (category, beauty, r["id"]))
            conn.execute("UPDATE assets SET beauty = CASE WHEN coalesce(category, 'photo') = 'photo' THEN ? END,"
                         " sort_done = 1 WHERE id = ? AND category_hand = 1", (beauty, r["id"]))
        conn.commit()
        self.state["pending"] = self.pending(conn)
        return True


# ---------------------------------------------------------------- suggested albums

# Things a smart album could gather, as (album name, what it searches for by content).
CONCEPTS = [
    ("Dogs", "a dog"), ("Cats", "a cat"), ("Food", "a plate of food"), ("Sunsets", "a sunset"),
    ("Beach", "a beach"), ("Mountains", "mountains"), ("Snow", "snow"), ("Flowers", "flowers"),
    ("Cars", "a car"), ("Birthdays", "a birthday cake with candles"), ("Fireworks", "fireworks"),
    ("Night sky", "the night sky with stars"), ("Babies", "a baby"), ("Hiking", "people hiking on a trail"),
    ("Concerts", "a concert stage"), ("Christmas", "a christmas tree"), ("Lakes", "a lake"),
    ("Desert", "a desert landscape"), ("Birds", "a bird"), ("Horses", "a horse"),
]
SUGGEST_MIN_SCORE = 0.26   # a photo must clearly show it (CLIP scores of unrelated pictures are about 0.15-0.24)
SUGGEST_MIN_PHOTOS = 12


def suggest_albums(conn, clip) -> list[dict]:
    """[{name, look, count, cover}] for concepts that many photos clearly show, most first."""
    import json

    out = []
    for name, look in CONCEPTS:
        found = [x for x in clip.search(conn, look) if x[1] >= SUGGEST_MIN_SCORE]
        if found:
            alive = {r[0] for r in conn.execute(
                "SELECT id FROM assets WHERE trashed_at IS NULL AND archived_at IS NULL"
                " AND id IN (SELECT value FROM json_each(?))", (json.dumps([i for i, _ in found]),))}
            found = [x for x in found if x[0] in alive]
        if len(found) >= SUGGEST_MIN_PHOTOS:
            out.append({"name": name, "look": look, "count": len(found), "cover": found[0][0]})
    out.sort(key=lambda x: -x["count"])
    return out
