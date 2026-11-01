"""Famous places in photos ("Eiffel Tower", "Grand Canyon"), from the content model (clipsearch.py).

The content model was trained on hundreds of millions of captioned web photos, so it knows what the
best-known landmarks look like. Each photo's content vector is compared with "a photo of the Eiffel
Tower in Paris" for every place on the list (landmark_list.py), and with plain descriptions of what
such photos usually show instead ("a photo of a tower", "a photo of a church", ...), so an ordinary tower
is not taken for the Eiffel Tower. The shares come from a softmax, as in sorting.py.

* A photo with a GPS position is only compared with the places near it, and a fairly clear match is
  enough: the position already says it is likely.
* A photo without one needs a very clear match. If the settings allow guessing places, it then gets
  the landmark's position, marked as guessed (placeguess.py).
"""

import logging

from .landmark_list import LANDMARKS
from .placeguess import km

log = logging.getLogger("snapshots.landmarks")

LOGIT_SCALE = 100.0
# Measured on 1,483 photos of 50 European landmarks (28 of them on the list) and 400 everyday photos
# (Flickr30k): with the real position, 68% of photos of listed places were named, 1.5% wrongly, and 1% of
# everyday photos taken at a listed place were given its name. Without a position, only the clearest
# photos are named (about 7%), with almost no mistakes (0.2% wrong, 1% of unlisted landmarks, 0 of 400).
NEAR_SHARE = 0.60     # with a GPS position close by: this share is enough
FAR_SHARE = 0.85      # without a position: only a very clear match counts
OTHERS = [
    "a photo of a building", "a photo of a house", "a photo of a church", "a photo of a cathedral",
    "a photo of a tower", "a photo of a bridge", "a photo of a castle", "a photo of a palace",
    "a photo of a skyscraper", "a photo of a city street", "a photo of a city skyline", "a photo of a square",
    "a photo of a statue", "a photo of a fountain", "a photo of a monument", "a photo of an arch",
    "a photo of a temple", "a photo of a mosque", "a photo of ruins", "a photo of a museum",
    "a photo of a mountain", "a photo of a canyon", "a photo of a waterfall", "a photo of a lake",
    "a photo of a beach", "a photo of a desert", "a photo of rocks", "a photo of a park",
    "a photo of a harbour", "a photo of a ferris wheel", "a photo of an amusement park",
    "a photo of people", "a selfie", "a photo of a family", "a photo of food", "a photo of a room",
    "a photo of a car", "a photo of an animal", "a photo of a plant", "a screenshot", "a photo of a document",
]


def prompt(entry) -> str:
    name, city = entry[0], entry[1]
    return f"a photo of the {name} in {city}" if city.lower() not in name.lower() else f"a photo of the {name}"


class Recogniser:
    """The description vectors, made once."""

    def __init__(self, engine, landmarks=LANDMARKS):
        import numpy as np

        self.landmarks = list(landmarks)
        self.places = np.stack([engine.embed_text(prompt(e)) for e in self.landmarks])
        self.others = np.stack([engine.embed_text(t) for t in OTHERS])

    def near(self, lat: float, lon: float) -> list[int]:
        """The landmarks a photo taken at (lat, lon) could show."""
        return [i for i, e in enumerate(self.landmarks) if km(lat, lon, e[3], e[4]) <= e[5]]

    def shares(self, vec, candidates: list[int]):
        """{candidate index: share}, the rest going to the plain descriptions."""
        import numpy as np

        scores = np.concatenate([self.places[candidates] @ vec, [float((self.others @ vec).max())]]) \
            if candidates else np.array([0.0])
        e = np.exp((scores - scores.max()) * LOGIT_SCALE)
        p = e / e.sum()
        return {c: float(p[k]) for k, c in enumerate(candidates)}

    def recognise(self, vec, position: tuple[float, float] | None) -> tuple | None:
        """The landmark entry the photo shows, or None. position: its real (not guessed) GPS position."""
        if position is not None:
            candidates = self.near(*position)
            need = NEAR_SHARE
        else:
            candidates = list(range(len(self.landmarks)))
            need = FAR_SHARE
        if not candidates:
            return None
        shares = self.shares(vec, candidates)
        best = max(shares, key=shares.get)
        return self.landmarks[best] if shares[best] >= need else None


class LandmarkJob:
    """Looks for landmarks in photos that have a content vector, a batch at a time."""

    def __init__(self, cfg, clip):
        self.cfg = cfg
        self.clip = clip
        self.retry_at = 0.0
        self.recogniser: Recogniser | None = None
        self.state = {"running": False, "found": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 200) -> bool:
        if now < self.retry_at or not self.cfg.smart_search or not self.clip.ready():
            return False
        conn.execute("UPDATE assets SET landmark_done = 1 WHERE landmark_done = 0 AND (kind != 'photo'"
                     " OR coalesce(category, '') IN ('screenshot', 'document', 'receipt'))")
        rows = conn.execute(
            "SELECT a.id, a.lat, a.lon, a.place_guess, a.position_edited, c.vec FROM assets a"
            " JOIN clip_vectors c ON c.asset_id = a.id"
            " WHERE a.landmark_done = 0 AND a.trashed_at IS NULL LIMIT ?", (batch,)).fetchall()
        if not rows:
            conn.commit()
            self.state["running"] = False
            return False
        import numpy as np

        try:
            if self.recogniser is None:
                self.recogniser = Recogniser(self.clip.load_engine())
        except Exception as exc:
            log.warning("landmarks can't be looked for: %s", exc)
            self.retry_at = now + 3600
            return False
        self.state["running"] = True
        for r in rows:
            if should_stop():
                break
            vec = np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
            vec = vec / (float(np.linalg.norm(vec)) or 1.0)
            # A position guessed from the photos around it is not proof: judged as if it had none.
            position = (r["lat"], r["lon"]) if r["lat"] is not None and r["place_guess"] is None else None
            found = self.recogniser.recognise(vec, position)
            name = found[0] if found else None
            conn.execute("UPDATE assets SET landmark = ?, landmark_done = 1 WHERE id = ?", (name, r["id"]))
            if found:
                self.state["found"] += 1
                if (r["lat"] is None or r["place_guess"] == "landmark") and not r["position_edited"] \
                        and self.cfg.guess_places:
                    conn.execute("UPDATE assets SET lat = ?, lon = ?, place_guess = 'landmark', geo_done = 0,"
                                 " city = NULL, state = NULL, country = NULL"
                                 " WHERE id = ? AND position_edited = 0 AND (lat IS NULL OR place_guess = 'landmark')",
                                 (found[3], found[4], r["id"]))
            elif r["place_guess"] == "landmark":
                # Not recognised any more (the photo changed): its guessed place goes too.
                conn.execute("UPDATE assets SET lat = NULL, lon = NULL, place_guess = NULL, geo_done = 1,"
                             " city = NULL, state = NULL, country = NULL, place_checked = 0"
                             " WHERE id = ? AND place_guess = 'landmark' AND position_edited = 0", (r["id"],))
        conn.commit()
        return True
