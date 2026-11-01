"""Titles for events ("Beach trip to Rosarito", "Anna's birthday") and who was at each.

* What happened: every photo with a content vector (clipsearch.py) is compared with a short list of
  occasions ("a photo of a beach", "a birthday party with a cake", ...) and with plain descriptions
  (zero-shot classification, as in sorting.py). A photo counts for an occasion when the model gives it
  a clear share. An event is about an occasion when enough of its photos are. Each photo's answer is
  remembered, so new photos are the only ones looked at again.
* Who was there: named people seen in at least two of the event's photos (one, for a small event), most
  photos first.
* A birthday party on (or within a few days of) a named person's birthday, with that person in it, is
  called that person's birthday.
"""

import logging
import threading
from collections import Counter
from datetime import date

log = logging.getLogger("snapshots.eventnames")

LOGIT_SCALE = 100.0
PHOTO_SHARE = 0.8      # a photo is about an occasion when the model gives it at least this share (at 0.5,
                       # a third of 400 everyday photos counted for something; at 0.8, one in seven, mostly right)
EVENT_SHARE = 0.25     # an event is about it when at least this share of its photos are ...
EVENT_MIN = 3          # ... and at least this many

# (key, what the model is asked, title on a day out, title of a trip)
THEMES = [
    ("beach", ["a photo of a beach", "a photo of people at the beach by the sea"], "Beach day", "Beach trip"),
    ("snow", ["a photo of people skiing", "a photo of people snowboarding on a snowy mountain"],
     "Snow day", "Snow trip"),
    ("hiking", ["a photo of people hiking on a mountain trail"], "Hiking", "Hiking trip"),
    ("camping", ["a photo of a tent at a campsite", "a photo of people camping"], "Camping", "Camping trip"),
    ("birthday", ["a photo of a birthday party with a birthday cake", "a photo of a birthday cake with candles"],
     "Birthday", "Birthday trip"),
    ("wedding", ["a photo of a wedding", "a photo of a bride and groom"], "Wedding", "Wedding"),
    ("christmas", ["a photo of a Christmas tree with presents", "a photo of a Christmas celebration"],
     "Christmas", "Christmas trip"),
    ("halloween", ["a photo of people in Halloween costumes", "a photo of carved pumpkins for Halloween"],
     "Halloween", "Halloween"),
    ("concert", ["a photo of a concert on a stage", "a photo of a band playing live music"], "Concert", "Concert trip"),
    ("graduation", ["a photo of a graduation ceremony with caps and gowns"], "Graduation", "Graduation"),
    ("zoo", ["a photo of animals at the zoo"], "Zoo visit", "Zoo trip"),
    ("themepark", ["a photo of a roller coaster at an amusement park"], "Theme park", "Theme park trip"),
    ("game", ["a photo of a sports game in a stadium"], "Game day", "Game trip"),
    ("fireworks", ["a photo of fireworks in the night sky"], "Fireworks", "Fireworks"),
    ("pool", ["a photo of people in a swimming pool"], "Pool day", "Pool trip"),
    ("boat", ["a photo of people on a boat on the water"], "Boat trip", "Boat trip"),
    ("desert", ["a photo of the desert with cacti"], "Desert day", "Desert trip"),
    ("museum", ["a photo inside a museum with paintings"], "Museum visit", "Museum trip"),
]
OTHERS = [
    "a photo", "a photo of people", "a photo of a person", "a selfie", "a photo of a family", "a photo of a room",
    "a photo of a house", "a photo of a street", "a photo of a city", "a photo of a landscape", "a photo of food",
    "a photo of a car", "a photo of a dog", "a photo of a cat", "a photo of a garden", "a photo of a park",
    "a photo of a restaurant", "a photo of a building", "a photo of trees", "a photo of the sky",
    "a screenshot", "a photo of a document",
]
NONE = -1


def theme_of(votes: list[int]) -> int | None:
    """The occasion (index into THEMES) most of an event's photos are about, if enough are."""
    counted = Counter(v for v in votes if v != NONE)
    if not counted or not votes:
        return None
    best, n = counted.most_common(1)[0]
    return best if n >= EVENT_MIN and n >= EVENT_SHARE * len(votes) else None


def near_birthday(birthday: str | None, start: str, end: str, days: int = 3) -> bool:
    """Is a birthday ('YYYY-MM-DD' or '--MM-DD') within `days` of an event (its dates as taken_at)?"""
    if not birthday:
        return False
    try:
        a, b = date.fromisoformat(start[:10]), date.fromisoformat(end[:10])
        month, day = int(birthday[-5:-3]), int(birthday[-2:])
    except ValueError:
        return False
    for year in {a.year, b.year}:
        try:
            when = date(year, month, day)
        except ValueError:          # 29 February in a year without one
            when = date(year, 3, 1)
        if (a - when).days <= days and (when - b).days <= days:
            return True
    return False


def title(event: dict, theme: int | None, people: list[dict]) -> str | None:
    """A title for an event, or None (then it is shown by its place and dates only)."""
    place = event.get("place")
    trip = event["kind"] == "trip"
    if theme is not None:
        key, _, day_title, trip_title = THEMES[theme]
        if key == "birthday":
            for p in people:
                if p.get("birthday") and near_birthday(p["birthday"], event["start"], event["end"]):
                    return f"{p['name']}'s birthday"
        if trip:
            return f"{trip_title} to {place}" if place and trip_title.endswith("trip") else \
                f"{trip_title} in {place}" if place else trip_title
        return f"{day_title} in {place}" if place and key not in ("birthday", "christmas", "halloween") else day_title
    if trip:
        return f"Trip to {place}" if place else None
    return None


class Namer:
    """Remembers what each photo is about, and names events."""

    def __init__(self, clip):
        self.clip = clip
        self._lock = threading.Lock()
        self._words = None
        self._votes: dict[int, tuple[str, int]] = {}   # asset id -> (its picture's version, theme index or NONE)

    def _load_words(self):
        import numpy as np

        engine = self.clip.load_engine()
        prompts, owner = [], []
        for i, (_, texts, _, _) in enumerate(THEMES):
            for t in texts:
                prompts.append(engine.embed_text(t))
                owner.append(i)
        self._words = (np.stack(prompts), np.array(owner), np.stack([engine.embed_text(t) for t in OTHERS]))

    def votes(self, conn, ids: list[int]) -> dict[int, int]:
        """{asset id: theme index or NONE} for those of `ids` that have a content vector."""
        import numpy as np

        if not self.clip.ready():
            return {}
        with self._lock:
            current: dict[int, str] = {}      # asset id -> its picture's version (a new picture: a new vector)
            for start in range(0, len(ids), 900):
                chunk = ids[start:start + 900]
                current.update(conn.execute(
                    "SELECT a.id, a.sha1 || coalesce(a.edit_v, '') FROM assets a"
                    f" JOIN clip_vectors c ON c.asset_id = a.id WHERE a.id IN ({','.join('?' * len(chunk))})", chunk).fetchall())
            missing = [i for i, version in current.items() if self._votes.get(i, (None,))[0] != version]
            if missing:
                if self._words is None:
                    self._load_words()
                prompts, owner, others = self._words
                for start in range(0, len(missing), 900):
                    chunk = missing[start:start + 900]
                    for aid, blob in conn.execute(
                            f"SELECT asset_id, vec FROM clip_vectors WHERE asset_id IN ({','.join('?' * len(chunk))})",
                            chunk):
                        v = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
                        v = v / (float(np.linalg.norm(v)) or 1.0)
                        per_theme = np.full(len(THEMES), -1.0, np.float32)
                        np.maximum.at(per_theme, owner, prompts @ v)
                        scores = np.concatenate([per_theme, [float((others @ v).max())]])
                        e = np.exp((scores - scores.max()) * LOGIT_SCALE)
                        p = e / e.sum()
                        k = int(p[:len(THEMES)].argmax())
                        self._votes[aid] = (current[aid], k if p[k] >= PHOTO_SHARE else NONE)
            return {i: self._votes[i][1] for i in current if i in self._votes}


def people_at(conn, events: list[dict]) -> None:
    """Add each event's named people ('people': [{id, name, photos, birthday}]) to it."""
    seen: dict[int, Counter] = {}
    who: dict[int, tuple] = {}
    for aid, pid, name, birthday in conn.execute(
            "SELECT DISTINCT f.asset_id, p.id, p.name, p.birthday FROM faces f JOIN people p ON p.id = f.person_id"
            " WHERE p.name IS NOT NULL AND p.hidden = 0"):
        seen.setdefault(aid, Counter())[pid] += 1
        who[pid] = (name, birthday)
    for e in events:
        counts: Counter = Counter()
        for aid in e["ids"]:
            for pid in seen.get(aid, ()):
                counts[pid] += 1
        need = 1 if e["count"] < 20 else 2
        e["people"] = [{"id": pid, "name": who[pid][0], "photos": n, "birthday": who[pid][1]}
                       for pid, n in counts.most_common() if n >= need]


def name_events(conn, events: list[dict], namer: "Namer | None") -> None:
    """Add 'title', 'theme' and 'people' to events (each with its 'ids')."""
    people_at(conn, events)
    all_ids = [i for e in events for i in e["ids"]]
    votes = namer.votes(conn, all_ids) if namer is not None else {}
    for e in events:
        got = [votes[i] for i in e["ids"] if i in votes]
        theme = theme_of(got)
        e["theme"] = THEMES[theme][0] if theme is not None else None
        e["title"] = title(e, theme, e["people"])
