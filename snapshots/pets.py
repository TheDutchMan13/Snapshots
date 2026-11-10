"""Pets: dogs and cats found in photos, and the same animal grouped across photos (like People).

There is no face model for animals here, so the content model (clipsearch.py) does it:
1. A photo shows a dog or a cat when its content vector is much closer to "a photo of a dog" (or cat)
   than to descriptions of other things photos show (zero-shot classification, as in sorting.py).
2. The animal is found by looking at 14 overlapping parts of the photo (the whole, four quarters and
   nine smaller parts) and taking the one that looks most like "a close-up photo of a dog" (or cat).
3. That part's content vector describes the animal: its colours, coat, shape and breed. Animals whose
   vectors are very alike are grouped as one pet, the same way faces are grouped into people.

Two animals of the same breed and colour can look alike to the model and end up together, and one pet can
be split in two: name a pet, and use "Same pet as…" to join two groups. One animal is taken per photo
(the clearest one).
"""

import logging
from datetime import datetime

log = logging.getLogger("snapshots.pets")

SPECIES = ("dog", "cat")
SPECIES_PROMPTS = {
    "dog": ["a photo of a dog", "a photo of a puppy", "a photo of my dog"],
    "cat": ["a photo of a cat", "a photo of a kitten", "a photo of my cat"],
}
OTHER_PROMPTS = [
    "a photo of people", "a photo of a person", "a selfie", "a photo of a family", "a photo of a landscape",
    "a photo of a building", "a photo of food", "a photo of a street", "a photo of a room", "a photo of a car",
    "a screenshot", "a photo of a document", "a photo of a bird", "a photo of a horse", "a photo of a toy",
]
CLOSE_UP = {"dog": "a close-up photo of a dog's face", "cat": "a close-up photo of a cat's face"}
LOGIT_SCALE = 100.0
SURE = 0.80            # a photo shows a dog (or cat) when the model gives it this share
MATCH = 0.86           # two sightings this alike are the same animal
MIN_PHOTOS = 3         # unnamed pets with fewer photos are not listed

# The parts of the picture looked at: (left, top, width, height) as fractions.
PARTS = ([(0.0, 0.0, 1.0, 1.0)]
         + [(x, y, 0.6, 0.6) for y in (0.0, 0.4) for x in (0.0, 0.4)]
         + [(x, y, 0.4, 0.4) for y in (0.0, 0.3, 0.6) for x in (0.0, 0.3, 0.6)])


class Words:
    """The description vectors, made once."""

    def __init__(self, engine):
        import numpy as np

        self.species = {s: np.stack([engine.embed_text(t) for t in SPECIES_PROMPTS[s]]) for s in SPECIES}
        self.other = np.stack([engine.embed_text(t) for t in OTHER_PROMPTS])
        self.close_up = {s: engine.embed_text(t) for s, t in CLOSE_UP.items()}

    def species_of(self, vec) -> tuple[str | None, float]:
        """('dog' or 'cat' or None, its share)."""
        import numpy as np

        best = {s: float((m @ vec).max()) for s, m in self.species.items()}
        scores = np.array(list(best.values()) + [float((self.other @ vec).max())])
        e = np.exp((scores - scores.max()) * LOGIT_SCALE)
        p = e / e.sum()
        k = int(p[:len(SPECIES)].argmax())
        return (SPECIES[k], float(p[k])) if p[k] >= SURE else (None, float(p[k]))


def find_animal(engine, words: Words, img, species: str):
    """(box, vector, score) of the part of the picture that shows the animal most clearly."""
    w, h = img.size
    best = None
    for x, y, pw, ph in PARTS:
        part = img.crop((int(x * w), int(y * h), int((x + pw) * w), int((y + ph) * h)))
        v = engine.embed_image(part)
        score = float(v @ words.close_up[species])
        if best is None or score > best[2]:
            best = ((x, y, pw, ph), v, score)
    return best


class PetGroups:
    """Average vector of every pet (per species), for grouping new sightings."""

    def __init__(self, conn):
        import numpy as np

        self.np = np
        sums: dict[int, "np.ndarray"] = {}
        self.species: dict[int, str] = {}
        for pid, species, blob in conn.execute(
                "SELECT s.pet_id, s.species, s.vec FROM pet_sightings s WHERE s.pet_id IS NOT NULL"):
            v = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
            sums[pid] = sums.get(pid, 0) + v
            self.species[pid] = species
        self.sums = sums

    def match(self, vec, species: str, threshold: float = MATCH) -> int | None:
        np = self.np
        best, best_sim = None, threshold
        for pid, total in self.sums.items():
            if self.species.get(pid) != species:
                continue
            n = float(np.linalg.norm(total))
            sim = float(total @ vec) / n if n else 0.0
            if sim >= best_sim:
                best, best_sim = pid, sim
        return best

    def add(self, pid: int, vec, species: str) -> None:
        self.sums[pid] = self.sums.get(pid, 0) + vec
        self.species[pid] = species


class PetsJob:
    """Looks for dogs and cats in photos that have a content vector, a batch at a time."""

    def __init__(self, cfg, clip):
        self.cfg = cfg
        self.clip = clip
        self.retry_at = 0.0
        self.words: Words | None = None
        self.groups: PetGroups | None = None
        self.state = {"running": False, "found": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 8) -> bool:
        if now < self.retry_at or not self.cfg.smart_search or not self.clip.ready():
            return False
        conn.execute("UPDATE assets SET pets_done = 1 WHERE pets_done = 0 AND kind != 'photo'")
        conn.commit()
        rows = conn.execute(
            "SELECT a.id, c.vec FROM assets a JOIN clip_vectors c ON c.asset_id = a.id"
            " WHERE a.pets_done = 0 AND a.trashed_at IS NULL AND a.missing_since IS NULL AND a.has_thumb = 1 LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state["running"] = False
            return False
        import numpy as np
        from PIL import Image

        try:
            engine = self.clip.load_engine()
            if self.words is None:
                self.words = Words(engine)
        except Exception as exc:
            log.warning("pets can't be looked for: %s", exc)
            self.retry_at = now + 3600
            return False
        self.state["running"] = True
        # First the slow part (up to 14 looks at each dog or cat photo), outside any write, so the
        # database stays free for everything else meanwhile...
        results = []                                        # (asset id, species, found)
        for r in rows:
            if should_stop():
                break
            vec = np.frombuffer(r["vec"], dtype=np.float16).astype(np.float32)
            species, _ = self.words.species_of(vec / (float(np.linalg.norm(vec)) or 1.0))
            found = None
            if species:
                try:
                    with Image.open(self.cfg.thumb_path(r["id"])) as img:
                        found = find_animal(engine, self.words, img.convert("RGB"), species)
                except Exception as exc:
                    log.info("could not look for a pet in asset %s: %s", r["id"], exc)
            results.append((r["id"], species, found))
        # ... then a short write.
        self.groups = PetGroups(conn)          # read again each batch: merges and removed photos count
        stamp = datetime.now().isoformat(timespec="seconds")
        for asset_id, species, found in results:
            conn.execute("DELETE FROM pet_sightings WHERE asset_id = ?", (asset_id,))
            if found:
                (x, y, w, h), v, score = found
                pid = self.groups.match(v, species)
                if pid is None:
                    pid = conn.execute("INSERT INTO pets (species, created_at) VALUES (?, ?)",
                                       (species, stamp)).lastrowid
                conn.execute("INSERT INTO pet_sightings (asset_id, pet_id, species, x, y, w, h, vec, score,"
                             " created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                             (asset_id, pid, species, x, y, w, h, v.astype(np.float16).tobytes(), score, stamp))
                self.groups.add(pid, v, species)
                self.state["found"] += 1
            # Unless the photo changed meanwhile (then it is looked at again).
            conn.execute("UPDATE assets SET pets_done = 1 WHERE id = ? AND has_thumb = 1", (asset_id,))
        # Pets whose every sighting went (photos changed or removed) go too, unless named.
        conn.execute("DELETE FROM pets WHERE name IS NULL AND id NOT IN"
                     " (SELECT pet_id FROM pet_sightings WHERE pet_id IS NOT NULL)")
        conn.commit()
        return True
