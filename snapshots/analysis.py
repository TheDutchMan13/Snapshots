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


class DuplicateFinder:
    """Groups of near-identical photos, recomputed only when the library has changed."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._key = None
        self._groups: list[dict] = []

    def _version(self, conn):
        return tuple(conn.execute(
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COALESCE(SUM(dhash % 1000003), 0),"
            " (SELECT COUNT(*) FROM not_duplicates), COALESCE(SUM(trashed_at IS NOT NULL), 0)"
            " FROM assets WHERE dhash IS NOT NULL"
        ).fetchone())

    def groups(self, conn) -> list[dict]:
        key = self._version(conn)
        with self._lock:
            if key != self._key:
                self._groups = self._compute(conn)
                self._key = key
            return self._groups

    def _is_burst(self, ms: list[dict]) -> bool:
        """Shots taken one after another (each at a different moment, seconds apart), not copies of one."""
        times = [datetime.fromisoformat(m["taken_at"]) for m in ms]
        if len(set(times)) < len(times):
            return False
        return all((b - a).total_seconds() <= self.cfg.burst_seconds for a, b in zip(times, times[1:]))

    def _compute(self, conn) -> list[dict]:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, dhash, taken_at, width, height, size, sharpness FROM assets"
            " WHERE dhash IS NOT NULL AND trashed_at IS NULL AND kind = 'photo'"
        )]
        by_id = {r["id"]: r for r in rows}
        copies = similar_pairs([(r["id"], r["dhash"]) for r in rows], self.cfg.duplicate_distance)
        bursts = burst_pairs(rows, self.cfg.burst_seconds, self.cfg.burst_distance) - copies
        dismissed = {(a, b) for a, b in conn.execute("SELECT a, b FROM not_duplicates")}
        uf = _Union()
        for a, b in (copies | bursts) - dismissed:
            uf.join(a, b)
        members = defaultdict(list)
        for i in list(uf.parent):
            members[uf.find(i)].append(by_id[i])
        groups = []
        for ms in members.values():
            if len(ms) < 2:
                continue
            ms.sort(key=lambda m: (m["taken_at"], m["id"]))
            groups.append({
                "kind": "burst" if self._is_burst(ms) else "copies",
                "ids": [m["id"] for m in ms],
                "keep": _best(ms),
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
    """A photo for the event's card: the sharpest of the middle half, or the middle one."""
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
            "SELECT COUNT(*), COALESCE(MAX(id), 0), COUNT(city), COALESCE(SUM(LENGTH(taken_at) + id % 97), 0)"
            " FROM assets WHERE trashed_at IS NULL"
        ).fetchone())

    def events(self, conn) -> list[dict]:
        key = self._version(conn)
        with self._lock:
            if key != self._key:
                rows = [dict(r) for r in conn.execute(
                    "SELECT id, taken_at, lat, lon, city, state, country, sharpness FROM assets"
                    " WHERE trashed_at IS NULL ORDER BY taken_at, id"
                )]
                self._events = find_events(rows, self.cfg.event_gap_hours, min_items=self.cfg.event_min_items)
                self._key = key
            return self._events


def record_not_duplicates(conn, ids: list[int]) -> None:
    """Remember that these photos are not copies of each other."""
    ids = sorted(set(ids))
    conn.executemany("INSERT OR IGNORE INTO not_duplicates (a, b) VALUES (?, ?)",
                     [(a, b) for i, a in enumerate(ids) for b in ids[i + 1:]])
