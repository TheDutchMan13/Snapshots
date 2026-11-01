"""Photos stored on their side or upside down (scans, and cameras that didn't record which way up they were).

The content model (clipsearch.py) understands pictures the right way up best: an upright photo matches
plain descriptions of what photos show ("a photo of people", "a photo of a street") more closely than
the same photo turned, and matches "an upright photo" more than "an upside down photo". So each photo
is also looked at turned a quarter, a half and three quarters, and if one of those fits clearly better
than the photo as it is, that turn is suggested. Photos with faces are skipped: faces were found in them,
and faces are only found the right way up. Nothing is turned until someone says so (Clean up).

It is a rough judgement, so it only suggests; the margin is set so that most suggestions should be right.
"""

import logging

from .sorting import CLASS_PROMPTS

log = logging.getLogger("snapshots.sideways")

TURNS = (90, 180, 270)          # degrees clockwise
MARGIN = 0.03                   # a turn must fit at least this much better than the photo as it is
UPRIGHT = ["an upright photo", "a photo taken the right way up"]
TURNED = ["an upside down photo", "a photo turned on its side", "a sideways photo", "a rotated photo"]


class Fit:
    """How well a picture fits being the right way up, from its content vector."""

    def __init__(self, engine):
        import numpy as np

        prompts = [t for texts in CLASS_PROMPTS.values() for t in texts]
        self.content = np.stack([engine.embed_text(t) for t in prompts])
        self.upright = np.stack([engine.embed_text(t) for t in UPRIGHT])
        self.turned = np.stack([engine.embed_text(t) for t in TURNED])

    def __call__(self, vec) -> float:
        import numpy as np

        top = np.sort(self.content @ vec)[-3:].mean()       # its three best descriptions
        way_up = float((self.upright @ vec).max() - (self.turned @ vec).max())
        return float(top) + way_up


def best_turn(fit, vectors: dict[int, "object"], margin: float = MARGIN) -> int | None:
    """The turn (90, 180 or 270) that fits clearly better than none, or None. vectors: {turn: vector}."""
    as_is = fit(vectors[0])
    scored = {t: fit(v) for t, v in vectors.items() if t}
    if not scored:
        return None
    turn = max(scored, key=scored.get)
    return turn if scored[turn] - as_is >= margin else None


class TurnJob:
    """Looks for sideways photos, a batch at a time, once they have a content vector."""

    def __init__(self, cfg, clip):
        self.cfg = cfg
        self.clip = clip
        self.retry_at = 0.0
        self.fit: Fit | None = None
        self.state = {"running": False, "found": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 16) -> bool:
        if now < self.retry_at or not self.cfg.smart_search or not self.clip.ready():
            return False
        # Edited photos (someone turned or cropped them) and photos with faces need no check.
        # (A suggestion also goes when faces are found in the photo later: faces are only found upright.)
        conn.execute("UPDATE assets SET turn_done = 1, turn = NULL WHERE (turn_done = 0 OR turn IS NOT NULL)"
                     " AND (edits IS NOT NULL OR kind != 'photo'"
                     " OR EXISTS (SELECT 1 FROM faces f WHERE f.asset_id = assets.id))")
        conn.commit()
        rows = conn.execute(
            "SELECT a.id, c.vec FROM assets a JOIN clip_vectors c ON c.asset_id = a.id"
            " WHERE a.turn_done = 0 AND a.trashed_at IS NULL AND a.has_thumb = 1 LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state["running"] = False
            return False
        import numpy as np
        from PIL import Image

        try:
            engine = self.clip.load_engine()
            if self.fit is None:
                self.fit = Fit(engine)
        except Exception as exc:
            log.warning("sideways photos can't be looked for: %s", exc)
            self.retry_at = now + 3600
            return False
        self.state["running"] = True
        results = []                     # the looks first, outside any write; then one short write
        for r in rows:
            if should_stop():
                break
            turn = None
            try:
                with Image.open(self.cfg.thumb_path(r["id"])) as img:
                    img = img.convert("RGB")
                    vectors = {0: np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)}
                    for t in TURNS:
                        vectors[t] = engine.embed_image(img.rotate(-t, expand=True))
                turn = best_turn(self.fit, vectors)
            except Exception as exc:
                log.info("could not check which way up asset %s is: %s", r["id"], exc)
            results.append((r["id"], turn))
        for asset_id, turn in results:
            # Not if someone edited (turned) it meanwhile.
            n = conn.execute("UPDATE assets SET turn = ?, turn_done = 1 WHERE id = ? AND turn_done = 0 AND edits IS NULL",
                             (turn, asset_id)).rowcount
            if turn and n:
                self.state["found"] += 1
        conn.commit()
        return True
