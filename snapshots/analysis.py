"""Photo checks that need no downloads: near-duplicates and bursts, blurry and dark photos, and events.

Each photo's thumbnail is measured once, in the background:
* a "difference hash" (dHash): 64 bits describing the picture's light and dark pattern. Copies of a
  photo (resized, re-saved, slightly edited) have nearly the same bits; different photos don't.
* sharpness: how much fine detail there is (variance of second differences), in the sharpest part of
  the picture, so a sharp subject in front of a soft background still counts as sharp. Each part is
  judged by its weaker direction, which is what camera shake blurs.
* brightness: the average light level.

Events need nothing stored: they are worked out from dates and places when the Events page is opened.
"""

import logging
import math
import re
import threading
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta

log = logging.getLogger("snapshots.analysis")

# ---------------------------------------------------------------- measuring one photo

HASH_W, HASH_H = 9, 8          # 9x8 grey pixels -> 8x8 = 64 left/right comparisons
FLAT_STD = 6.0                 # pictures flatter than this (all black, all white) get no hash
SHARP_SIDE = 480               # sharpness is measured at (at most) this size, so photos are comparable
DARK_HIGHLIGHTS = 80           # "too dark" also needs the brightest 1% to be dim: night and concert shots
                               # with bright spots are fine (measured: 0 of 400 normal photos flagged)
SHARP_GRID = 4                 # ... in each cell of a 4x4 grid; the photo's sharpness is its sharpest cell


def _signed(bits: int) -> int:
    """SQLite integers are signed 64-bit."""
    return bits - (1 << 64) if bits >= (1 << 63) else bits


def _unsigned(value: int) -> int:
    return value + (1 << 64) if value < 0 else value


def measure(img) -> dict:
    """dhash (or None for a flat picture), sharpness, brightness and highlights of a PIL image."""
    import numpy as np
    from PIL import Image

    grey = img.convert("L")
    small = np.asarray(grey.resize((HASH_W, HASH_H), Image.LANCZOS), dtype=np.int16)
    tiny = np.asarray(grey.resize((32, 32), Image.BILINEAR), dtype=np.float32)
    dhash = None
    if float(tiny.std()) >= FLAT_STD:
        bits = (small[:, 1:] > small[:, :-1]).flatten()
        value = 0
        for b in bits:
            value = (value << 1) | int(b)
        dhash = _signed(value)

    w, h = grey.size
    scale = SHARP_SIDE / max(w, h)
    if scale < 0.99:  # never enlarge: an enlarged small photo would look blurry
        grey = grey.resize((max(1, round(w * scale)), max(1, round(h * scale))), Image.BILINEAR)
    g = np.asarray(grey, dtype=np.float32)
    # Second differences across and down. Camera shake smears detail in one direction only, so each
    # cell is judged by its weaker direction; the photo by its sharpest cell (a sharp subject is enough).
    cells = []
    rows = np.array_split(np.arange(g.shape[0]), SHARP_GRID)
    cols = np.array_split(np.arange(g.shape[1]), SHARP_GRID)
    for r in rows:
        for c in cols:
            cell = g[r[0]:r[-1] + 1, c[0]:c[-1] + 1]
            if cell.shape[0] > 4 and cell.shape[1] > 4:
                across = cell[:, :-2] + cell[:, 2:] - 2 * cell[:, 1:-1]
                down = cell[:-2] + cell[2:] - 2 * cell[1:-1]
                cells.append(min(float(across.var()), float(down.var())))
    sharpness = max(cells) if cells else 0.0
    return {"dhash": dhash, "sharpness": round(sharpness, 2), "brightness": round(float(g.mean()), 2),
            "highlights": round(float(np.percentile(g, 99)), 2)}


def hamming(a: int, b: int) -> int:
    return (_unsigned(a) ^ _unsigned(b)).bit_count()


class QualityJob:
    """Measures photos (from their thumbnails) in small batches. Videos are skipped."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE quality_done = 0 AND trashed_at IS NULL").fetchone()[0]

    def run_batch(self, conn, now: float, should_stop, batch: int = 100) -> bool:
        rows = conn.execute(
            "SELECT id, kind, has_thumb FROM assets WHERE quality_done = 0 AND trashed_at IS NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            self.state["running"] = False
            return False
        from PIL import Image

        self.state["running"] = True
        for row in rows:
            if should_stop():
                break
            m = {"dhash": None, "sharpness": None, "brightness": None, "highlights": None}
            if row["kind"] == "photo" and row["has_thumb"]:
                try:
                    with Image.open(self.cfg.thumb_path(row["id"])) as img:
                        m = measure(img)
                except Exception as exc:
                    log.info("could not measure asset %s: %s", row["id"], exc)
            conn.execute(
                "UPDATE assets SET dhash = ?, sharpness = ?, brightness = ?, highlights = ?, quality_done = 1"
                " WHERE id = ?",
                (m["dhash"], m["sharpness"], m["brightness"], m["highlights"], row["id"]),
            )
        conn.commit()
        self.state["pending"] = self.pending(conn)
        return True


# ---------------------------------------------------------------- blurry and dark

def low_quality_sql(cfg) -> str:
    """SQL condition (on alias a) for photos that look blurry or too dark, and weren't marked as fine."""
    return (
        f"(a.kind = 'photo' AND a.keep_quality = 0 AND a.quality_done = 1 AND"
        f" (a.sharpness < {float(cfg.blur_threshold)!r}"
        f"  OR (a.brightness < {float(cfg.dark_threshold)!r} AND a.highlights < {float(DARK_HIGHLIGHTS)!r})))"
    )


def quality_label(cfg, sharpness, brightness, highlights) -> str | None:
    if brightness is not None and brightness < cfg.dark_threshold and (highlights or 0) < DARK_HIGHLIGHTS:
        return "dark"
    if sharpness is not None and sharpness < cfg.blur_threshold:
        return "blurry"
    return None


# ---------------------------------------------------------------- near-duplicates and bursts

class _Union:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        self.parent.setdefault(x, x)
        while self.parent[x] != x:
            self.parent[x] = self.parent[self.parent[x]]
            x = self.parent[x]
        return x

    def join(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[max(ra, rb)] = min(ra, rb)


def _bands(distance: int) -> list[tuple[int, int]]:
    """Split 64 bits into distance+1 bands: two hashes within `distance` bits agree on at least one band."""
    n = distance + 1
    edges = [round(i * 64 / n) for i in range(n + 1)]
    return [(edges[i], edges[i + 1]) for i in range(n)]


def similar_pairs(items: list[tuple[int, int]], distance: int) -> set[tuple[int, int]]:
    """Pairs of ids whose hashes differ in at most `distance` bits. items: [(id, dhash)]."""
    found = set()
    unsigned = {i: _unsigned(h) for i, h in items}
    for lo, hi in _bands(distance):
        mask = ((1 << (hi - lo)) - 1) << lo
        buckets = defaultdict(list)
        for i, h in unsigned.items():
            buckets[h & mask].append(i)
        for ids in buckets.values():
            if len(ids) < 2 or len(ids) > 2000:  # a giant bucket means a meaningless pattern: skip it
                continue
            for x in range(len(ids)):
                hx = unsigned[ids[x]]
                for y in range(x + 1, len(ids)):
                    if (hx ^ unsigned[ids[y]]).bit_count() <= distance:
                        a, b = ids[x], ids[y]
                        found.add((a, b) if a < b else (b, a))
    return found


def burst_pairs(rows, seconds: int, distance: int) -> set[tuple[int, int]]:
    """Pairs taken within `seconds` of each other that look alike (looser than for copies)."""
    found = set()
    timed = sorted(rows, key=lambda r: r["taken_at"])
    stamps = [datetime.fromisoformat(r["taken_at"]) for r in timed]
    for i, r in enumerate(timed):
        j = i + 1
        while j < len(timed) and (stamps[j] - stamps[i]).total_seconds() <= seconds:
            if hamming(r["dhash"], timed[j]["dhash"]) <= distance:
                a, b = r["id"], timed[j]["id"]
                found.add((a, b) if a < b else (b, a))
            j += 1
    return found


def _best(members: list[dict]) -> int:
    """The copy to keep: most pixels, then sharpest, then largest file, then the oldest in the library."""
    return max(members, key=lambda m: ((m["width"] or 0) * (m["height"] or 0), m["sharpness"] or 0,
                                       m["size"] or 0, -m["id"]))["id"]


def _best_identical(members: list[dict]) -> int:
    """Identical files: keep the one in the main library (not an extra folder), else the oldest."""
    return min(members, key=lambda m: (bool(re.match(r"^(/|[A-Za-z]:/)", m["path"])), m["id"]))["id"]


def why_best(members: list[dict], keep: int, identical: bool) -> str:
    """In a few words, why `keep` was picked."""
    k = next(m for m in members if m["id"] == keep)
    others = [m for m in members if m["id"] != keep]
    if identical:
        return "Same file: kept the first one added"
    px = lambda m: (m["width"] or 0) * (m["height"] or 0)  # noqa: E731
    if all(px(k) > px(o) for o in others):
        return "Most pixels"
    if all((k["sharpness"] or 0) > (o["sharpness"] or 0) for o in others if px(o) == px(k)):
        return "Sharpest"
    if all((k["size"] or 0) > (o["size"] or 0) for o in others):
        return "Largest file"
    return "Added first"


LOOKALIKE_SECONDS = 120   # a copy keeps the date of the photo it came from: only photos this close are compared


def lookalike_pairs(conn, items: list[tuple[int, str, bool]], threshold: float, block: int = 512) -> set[tuple[int, int]]:
    """Pairs whose content vectors (see clipsearch) are at least `threshold` alike (cosine).

    The fingerprint above misses copies that were cropped, rotated, straightened or given a filter: their
    light and dark pattern moved. What the picture shows did not, so its content vector stays almost the
    same. items: [(id, taken_at, dated)]. A copy keeps its photo's date, so photos with a date of their
    own are only compared with photos taken within LOOKALIKE_SECONDS (which also keeps the same view
    shot on two different days apart); photos with a guessed date are compared with every photo."""
    import bisect
    import json

    import numpy as np

    if threshold <= 0 or len(items) < 2:
        return set()
    info = {i: (t, d) for i, t, d in items}
    rows = conn.execute("SELECT asset_id, vec FROM clip_vectors WHERE asset_id IN (SELECT value FROM json_each(?))",
                        (json.dumps(sorted(info)),)).fetchall()
    if len(rows) < 2:
        return set()
    rows.sort(key=lambda r: (info[r[0]][0], r[0]))
    order = [r[0] for r in rows]
    stamps = []
    for i in order:
        try:
            stamps.append(datetime.fromisoformat(info[i][0]).timestamp())
        except (TypeError, ValueError):
            stamps.append(0.0)
    m = np.frombuffer(b"".join(r[1] for r in rows), dtype=np.float16).reshape(len(rows), -1).astype(np.float32)
    norms = np.linalg.norm(m, axis=1)
    m /= np.where(norms > 0, norms, 1)[:, None]
    t = np.array(stamps)
    n = len(order)
    found: set[tuple[int, int]] = set()

    def add(x, y):
        a, b = order[x], order[y]
        found.add((a, b) if a < b else (b, a))

    # Photos with dates of their own: in time order, each block against what follows it within the window.
    for i0 in range(0, n, block):
        i1 = min(n, i0 + block)
        k = bisect.bisect_right(stamps, stamps[i1 - 1] + LOOKALIKE_SECONDS)
        sims = m[i0:i1] @ m[i0:k].T
        close = np.abs(t[i0:k][None, :] - t[i0:i1][:, None]) <= LOOKALIKE_SECONDS
        for i, j in zip(*np.nonzero((sims >= threshold) & close)):
            x, y = i0 + int(i), i0 + int(j)
            if y > x:
                add(x, y)
    # Photos whose date is only a guess: against everything.
    loose = [x for x, i in enumerate(order) if not info[i][1]]
    for s0 in range(0, len(loose), block):
        part = loose[s0:s0 + block]
        sims = m[part] @ m.T
        for i, j in zip(*np.nonzero(sims >= threshold)):
            x, y = part[int(i)], int(j)
            if x != y:
                add(x, y)
    return found


# ---------------------------------------------------------------- the best shot of a burst

BLINK = 0.65     # a face whose eye detail is below this share of the same person's best in the burst: eyes shut


def faces_of(conn, ids: list[int]) -> dict[int, list[dict]]:
    import json

    out: dict[int, list[dict]] = defaultdict(list)
    for r in conn.execute("SELECT asset_id, person_id, x, y, w, h, eyes, sharp, frontal FROM faces"
                          " WHERE asset_id IN (SELECT value FROM json_each(?))", (json.dumps(ids),)):
        out[r["asset_id"]].append(dict(r))
    return out


def _who(face: dict):
    """The same person in each photo of a burst: their group, or else roughly where the face is."""
    if face["person_id"] is not None:
        return ("p", face["person_id"])
    return ("at", round((face["x"] + face["w"] / 2) * 5), round((face["y"] + face["h"] / 2) * 5))


def best_shot(members: list[dict], faces: dict[int, list[dict]]) -> tuple[int, str] | None:
    """The photo to keep from a burst, by its faces: everyone there, eyes open, faces sharp, looking at the
    camera. None when the burst has no measured faces (then the sharpest photo is kept, as before).

    Eye detail is compared between photos of the same person only: it depends on the light and the
    person, so on its own it says little, but a blink makes it drop well below that person's other shots."""
    measured = {m["id"]: [f for f in faces.get(m["id"], []) if f["eyes"] is not None or f["sharp"] is not None]
                for m in members}
    if not any(measured.values()):
        return None
    people = defaultdict(dict)                 # who -> {asset_id: face}
    for aid, fs in measured.items():
        for f in fs:
            key = _who(f)
            prev = people[key].get(aid)
            if prev is None or f["w"] * f["h"] > prev["w"] * prev["h"]:
                people[key][aid] = f
    # Only people in at least half the photos count (a passer-by in one frame doesn't decide).
    people = {k: v for k, v in people.items() if len(v) * 2 >= len(members)}
    if not people:
        return None
    sharp_top = max((m["sharpness"] or 0) for m in members) or 1.0
    scored = []
    for m in members:
        total, blinks = 0.0, 0
        for shots in people.values():
            f = shots.get(m["id"])
            if f is None:
                continue                        # not in this one (turned away, or out of the picture)
            eyes_top = max((g["eyes"] or 0) for g in shots.values())
            sharp_best = max((g["sharp"] or 0) for g in shots.values())
            eyes = (f["eyes"] / eyes_top) if f["eyes"] is not None and eyes_top else 1.0
            blink = eyes < BLINK
            blinks += blink
            sharp = (f["sharp"] / sharp_best) if f["sharp"] is not None and sharp_best else 1.0
            front = f["frontal"] if f["frontal"] is not None else 1.0
            total += 0.45 * min(1.0, eyes) + 0.3 * sharp + 0.25 * front - (0.6 if blink else 0.0)
        score = total / len(people) + 0.05 * ((m["sharpness"] or 0) / sharp_top)
        scored.append((score, -blinks, m["id"], blinks))
    best = max(scored)
    keep, keep_blinks = best[2], best[3]
    others_blink = any(b > 0 for _, _, i, b in scored if i != keep)
    why = "Eyes open" if others_blink and keep_blinks == 0 else "Sharpest faces"
    return keep, why


STRICTNESS = {"strict": 4, "normal": None, "loose": 12}   # None: the duplicate_distance setting
LOOKALIKE = {"strict": 0.98, "normal": None, "loose": 0.93}  # None: the lookalike_similarity setting


class DuplicateFinder:
    """Groups of identical files, near-identical photos and bursts, recomputed only when the library changed.

    * identical: the very same file content (photos and videos), for instance in two folders;
    * copies: photos that look the same (a resized or re-saved copy, an edit with the same picture);
    * burst: photos taken seconds apart that look alike.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._cache: dict = {}

    def _version(self, conn):
        return tuple(conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(dhash % 1000003), 0),"
            " (SELECT COUNT(*) FROM not_duplicates), COALESCE(SUM(trashed_at IS NOT NULL), 0),"
            " COALESCE(SUM(coalesce(stack_id, 0) % 1000003), 0), COALESCE(SUM(CASE WHEN locked_by IS NOT NULL THEN id END), 0),"
            # New content vectors (for edited copies): while photos are still being learned, only every
            # thousand counts, so indexing doesn't make this start over after each small batch
            " COALESCE(SUM(id % 1000003), 0), (SELECT COUNT(*) FROM clip_vectors)"
            " / CASE WHEN EXISTS (SELECT 1 FROM assets WHERE clip_done = 0 AND trashed_at IS NULL) THEN 1000 ELSE 1 END"
            " FROM assets"
        ).fetchone())

    def distance(self, strictness: str | None) -> int:
        d = STRICTNESS.get(strictness or "normal")
        return self.cfg.duplicate_distance if d is None else d

    def similarity(self, strictness: str | None) -> float:
        s = LOOKALIKE.get(strictness or "normal")
        base = getattr(self.cfg, "lookalike_similarity", 0.0) or 0.0
        if base <= 0:
            return 0.0           # switched off
        return base if s is None else s

    def groups(self, conn, strictness: str | None = None) -> list[dict]:
        key = self._version(conn)
        distance = self.distance(strictness)
        similarity = self.similarity(strictness)
        with self._lock:
            hit = self._cache.get((distance, similarity))
            if hit is None or hit[0] != key:
                hit = (key, self._compute(conn, distance, similarity))
                self._cache[(distance, similarity)] = hit
            return hit[1]

    def _is_burst(self, ms: list[dict]) -> bool:
        """Shots taken one after another (each at a different moment, seconds apart), not copies of one."""
        times = [datetime.fromisoformat(m["taken_at"]) for m in ms]
        if len(set(times)) < len(times):
            return False
        return all((b - a).total_seconds() <= self.cfg.burst_seconds for a, b in zip(times, times[1:]))

    def _compute(self, conn, distance: int | None = None, similarity: float | None = None) -> list[dict]:
        distance = self.cfg.duplicate_distance if distance is None else distance
        similarity = self.similarity(None) if similarity is None else similarity
        rows = [dict(r) for r in conn.execute(
            "SELECT a.id, a.dhash, a.taken_at, a.width, a.height, a.size, a.sharpness, a.stack_id, a.sha1, a.kind, a.path,"
            " a.date_guess"
            " FROM assets a WHERE a.trashed_at IS NULL AND a.locked_by IS NULL"
            " AND ((a.dhash IS NOT NULL AND a.kind = 'photo')"
            "      OR a.sha1 IN (SELECT sha1 FROM assets WHERE trashed_at IS NULL AND locked_by IS NULL"
            "                    GROUP BY sha1 HAVING COUNT(*) > 1))"
            " AND NOT EXISTS (SELECT 1 FROM assets lp WHERE lp.live_video = a.id)"   # a Live Photo's hidden clip
        )]
        by_id = {r["id"]: r for r in rows}
        same_file = defaultdict(list)
        for r in rows:
            same_file[r["sha1"]].append(r["id"])
        identical = {(min(a, b), max(a, b)) for ids in same_file.values() if len(ids) > 1
                     for i, a in enumerate(ids) for b in ids[i + 1:]}
        photos = [(r["id"], r["dhash"]) for r in rows if r["dhash"] is not None and r["kind"] == "photo"]
        copies = similar_pairs(photos, distance) | identical
        # Edited, cropped and filtered copies, which the fingerprint misses (flat pictures have no
        # fingerprint, and are left out here too: all black frames look alike).
        copies |= lookalike_pairs(conn, [(r["id"], r["taken_at"], r["date_guess"] not in ("file", "nearby"))
                                         for r in rows if r["dhash"] is not None and r["kind"] == "photo"], similarity)
        bursts = burst_pairs([r for r in rows if r["dhash"] is not None and r["kind"] == "photo"],
                             self.cfg.burst_seconds, self.cfg.burst_distance) - copies
        dismissed = {(a, b) for a, b in conn.execute("SELECT a, b FROM not_duplicates")}
        uf = _Union()
        for a, b in (copies | bursts) - dismissed:
            sa, sb = by_id[a]["stack_id"], by_id[b]["stack_id"]
            if sa is not None and sa == sb:
                continue  # stacked together on purpose (a RAW + JPEG pair, say)
            uf.join(a, b)
        members = defaultdict(list)
        for i in list(uf.parent):
            members[uf.find(i)].append(by_id[i])
        groups = []
        for ms in members.values():
            if len(ms) < 2:
                continue
            ms.sort(key=lambda m: (m["taken_at"], m["id"]))
            same = len({m["sha1"] for m in ms}) == 1
            kind = "identical" if same else "burst" if self._is_burst(ms) else "copies"
            keep = _best_identical(ms) if same else _best(ms)
            why = why_best(ms, keep, same)
            if kind == "burst":
                shot = best_shot(ms, faces_of(conn, [m["id"] for m in ms]))
                if shot:
                    keep, why = shot
            groups.append({
                "kind": kind,
                "ids": [m["id"] for m in ms],
                "keep": keep,
                "why": why,
                "space": sum(m["size"] or 0 for m in ms if m["id"] != keep),
                "taken_at": ms[-1]["taken_at"],
            })
        groups.sort(key=lambda g: g["taken_at"], reverse=True)
        return groups


# ---------------------------------------------------------------- events

def _km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    a = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742 * math.asin(min(1.0, math.sqrt(a)))


@dataclass
class _Moment:
    rows: list
    away: bool | None = None   # None: no position known


def find_events(rows: list[dict], gap_hours: float = 4, jump_km: float = 30, trip_gap_hours: float = 36,
                min_items: int = 8) -> list[dict]:
    """Group photos into events. rows: dicts with id, taken_at, lat, lon, city, state, country, sorted by
    taken_at. Returns events newest first.

    1. "Moments": photos follow on from each other unless more than gap_hours pass, or the place jumps
       more than jump_km.
    2. Home is the town most photos were taken in. Moments away from home that follow each other within
       trip_gap_hours (in the same country) become one trip.
    3. At home, the moments of one day become one event.
    Events with fewer than min_items photos are left out.
    """
    if not rows:
        return []
    towns = Counter((r["city"], r["country"]) for r in rows if r["city"])
    home = towns.most_common(1)[0][0] if towns else None
    home_states = Counter(r["state"] or r["country"] for r in rows if home and (r["city"], r["country"]) == home)
    home_region = home_states.most_common(1)[0][0] if home_states else None

    moments: list[_Moment] = []
    prev_t, prev_pos = None, None
    for r in rows:
        t = datetime.fromisoformat(r["taken_at"])
        pos = (r["lat"], r["lon"]) if r["lat"] is not None else None
        new = prev_t is None or (t - prev_t) > timedelta(hours=gap_hours)
        if not new and pos and prev_pos and _km(*prev_pos, *pos) > jump_km:
            new = True
        if new:
            moments.append(_Moment([]))
        moments[-1].rows.append(r)
        prev_t = t
        if pos:
            prev_pos = pos
    for m in moments:
        known = [(r["city"], r["country"]) for r in m.rows if r["city"]]
        if known:
            m.away = home is not None and Counter(known).most_common(1)[0][0] != home

    def country(m):
        c = Counter(r["country"] for r in m.rows if r["country"])
        return c.most_common(1)[0][0] if c else None

    def gap(a, b):
        return datetime.fromisoformat(b.rows[0]["taken_at"]) - datetime.fromisoformat(a.rows[-1]["taken_at"])

    events: list[list] = []
    kinds: list[str] = []
    for m in moments:
        if events:
            last = events[-1]
            if kinds[-1] == "trip" and m.away is not False and gap(last[-1], m) <= timedelta(hours=trip_gap_hours) \
                    and (m.away is None or country(m) == country(last[-1]) or country(m) is None):
                last.append(m)
                continue
            if kinds[-1] == "day" and m.away is not True and \
                    m.rows[0]["taken_at"][:10] == last[-1].rows[-1]["taken_at"][:10]:
                last.append(m)
                continue
        events.append([m])
        kinds.append("trip" if m.away else "day")

    out = []
    for parts, kind in zip(events, kinds):
        items = [r for m in parts for r in m.rows]
        if len(items) < min_items:
            continue
        place = _event_place(items, home, home_region, kind)
        start, end = items[0]["taken_at"], items[-1]["taken_at"]
        days = (datetime.fromisoformat(end[:10]) - datetime.fromisoformat(start[:10])).days + 1
        out.append({
            "kind": "trip" if kind == "trip" else "day",
            "place": place,
            "start": start, "end": end, "days": days,
            "count": len(items),
            "cover": _cover(items),
        })
    out.reverse()
    return out


def _event_place(items: list[dict], home, home_region, kind: str) -> str | None:
    """A name for where an event happened. A trip to a few towns lists them in the order visited; a
    longer tour is named after its region (unless that is the home region: then its main towns)."""
    away = [r for r in items if r["city"] and (not home or (r["city"], r["country"]) != home)]
    if kind != "trip" or not away:
        towns = Counter(r["city"] for r in items if r["city"])
        return towns.most_common(1)[0][0] if towns else None
    towns = list(dict.fromkeys(r["city"] for r in away))   # in the order visited
    counts = Counter(r["city"] for r in away)
    if len(towns) <= 3:
        return _join(towns)
    regions = Counter(r["state"] or r["country"] for r in away)
    region = regions.most_common(1)[0][0]
    if region and region != home_region and regions[region] >= 0.6 * len(away):
        return region
    main = [t for t, _ in counts.most_common(2)]
    main.sort(key=towns.index)
    return f"{main[0]}, {main[1]} and {len(towns) - 2} more"


def _join(names: list[str]) -> str:
    return names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]


def _cover(items: list[dict]) -> int:
    """A photo for the event's card: the best-looking one (sorting.py), else the sharpest of the middle
    half, else the middle one. Blurry, dark and screenshot-like pictures don't make covers."""
    usable = [r for r in items if not r.get("category")]
    pretty = [r for r in usable if r.get("beauty") is not None]
    if len(pretty) * 2 >= len(usable) and pretty:
        sharp = sorted(r["sharpness"] for r in pretty if r.get("sharpness") is not None)
        floor = sharp[len(sharp) // 4] if sharp else None     # leave out the blurriest quarter
        good = [r for r in pretty if floor is None or (r.get("sharpness") or 0) >= floor] or pretty
        return max(good, key=lambda r: r["beauty"])["id"]
    middle = items[len(items) // 4: max(len(items) // 4 + 1, 3 * len(items) // 4)]
    scored = [r for r in middle if r.get("sharpness") is not None]
    return max(scored, key=lambda r: r["sharpness"])["id"] if scored else middle[len(middle) // 2]["id"]


class EventFinder:
    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._key = None
        self._events: list[dict] = []

    def _version(self, conn):
        return tuple(conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COUNT(city), COALESCE(SUM(LENGTH(taken_at) + id % 97), 0),"
            " COUNT(archived_at), COUNT(live_video), COALESCE(SUM(CASE WHEN locked_by IS NOT NULL THEN id END), 0),"
            # dates and positions people changed by hand
            " COALESCE(SUM(CAST(replace(replace(replace(taken_at, '-', ''), 'T', ''), ':', '') AS INTEGER) % 1000003), 0),"
            " COALESCE(SUM(CAST(coalesce(lat, 0) * 10000 + coalesce(lon, 0) * 100 AS INTEGER) % 1000003), 0),"
            " COUNT(beauty), COUNT(category)"
            " FROM assets WHERE trashed_at IS NULL"
        ).fetchone())

    def events(self, conn) -> list[dict]:
        key = self._version(conn)
        with self._lock:
            if key != self._key:
                rows = [dict(r) for r in conn.execute(
                    "SELECT a.id, a.taken_at, a.lat, a.lon, a.city, a.state, a.country, a.sharpness, a.beauty,"
                    " CASE WHEN a.category IN ('screenshot', 'document', 'receipt') THEN a.category END AS category"
                    " FROM assets a"
                    " WHERE a.trashed_at IS NULL AND a.archived_at IS NULL AND a.locked_by IS NULL"
                    " AND NOT EXISTS (SELECT 1 FROM assets lp WHERE lp.live_video = a.id) ORDER BY a.taken_at, a.id"
                )]
                self._events = find_events(rows, self.cfg.event_gap_hours, min_items=self.cfg.event_min_items)
                self._key = key
            return self._events


def record_not_duplicates(conn, ids: list[int]) -> None:
    """Remember that these photos are not copies of each other."""
    ids = sorted(set(ids))
    conn.executemany("INSERT OR IGNORE INTO not_duplicates (a, b) VALUES (?, ?)",
                     [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]])
