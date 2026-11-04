"""Places for photos that have none: from the photos taken just before and after them.

A camera without GPS, a scan, or a phone with location turned off leaves photos without a place. Often
another photo taken minutes earlier or later (on a phone that does record where) shows where they were.
A photo without a place gets:
* the place of a photo with GPS taken at most CLOSE_MINUTES before or after it, or
* a place between two such photos, one before and one after, at most SPAN_MINUTES apart in all and at
  most SPAN_KM apart (people move during a day: a guess between two far-apart places would be wrong).
The guess is marked as one (place_guess = 'nearby'); it is shown as guessed, is never written to a
sidecar file, and goes away when someone sets or removes the place by hand. A place recognised from a
landmark (landmarks.py) is marked 'landmark'.
"""

import logging
import math
from datetime import datetime, timedelta

log = logging.getLogger("snapshots.placeguess")

CLOSE_MINUTES = 15
SPAN_MINUTES = 90
SPAN_KM = 2.0
FMT = "%Y-%m-%dT%H:%M:%S"

# Photos whose own place counts as evidence: a real position, a date of their own (not the file's), and not
# in anyone's locked folder (its place must not show up on someone else's photo).
SOURCE = ("lat IS NOT NULL AND place_guess IS NULL AND trashed_at IS NULL AND locked_by IS NULL"
          " AND coalesce(date_guess, '') NOT IN ('file', 'nearby')")


def km(lat1, lon1, lat2, lon2) -> float:
    p = math.pi / 180
    h = (math.sin((lat2 - lat1) * p / 2) ** 2
         + math.cos(lat1 * p) * math.cos(lat2 * p) * math.sin((lon2 - lon1) * p / 2) ** 2)
    return 12742.0 * math.asin(min(1.0, math.sqrt(h)))


def _parse(text: str) -> datetime | None:
    try:
        return datetime.strptime(text[:19], FMT)
    except (TypeError, ValueError):
        return None


def guess(taken: datetime, before, after) -> tuple[float, float] | None:
    """A position for a photo taken at `taken`, from (time, lat, lon) of the nearest photo with GPS before
    and after it (either may be None)."""
    close = timedelta(minutes=CLOSE_MINUTES)
    if before and after and after[0] - before[0] <= timedelta(minutes=SPAN_MINUTES) \
            and km(before[1], before[2], after[1], after[2]) <= SPAN_KM:
        span = (after[0] - before[0]).total_seconds()
        share = (taken - before[0]).total_seconds() / span if span > 0 else 0.0
        return (round(before[1] + (after[1] - before[1]) * share, 6),
                round(before[2] + (after[2] - before[2]) * share, 6))
    near = [p for p in (before, after) if p and abs(p[0] - taken) <= close]
    if not near:
        return None
    best = min(near, key=lambda p: abs(p[0] - taken))
    return round(best[1], 6), round(best[2], 6)


def neighbours(conn, taken_at: str):
    """(time, lat, lon) of the nearest photo with GPS before and after taken_at (None where there is none)."""
    out = []
    for op, order in (("<=", "DESC"), (">=", "ASC")):
        row = conn.execute(f"SELECT taken_at, lat, lon FROM assets WHERE {SOURCE} AND taken_at {op} ?"
                           f" ORDER BY taken_at {order} LIMIT 1", (taken_at,)).fetchone()
        t = _parse(row[0]) if row else None
        out.append((t, row[1], row[2]) if t else None)
    return out[0], out[1]


class PlaceGuessJob:
    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "guessed": 0}

    def run_batch(self, conn, now: float, should_stop, batch: int = 200) -> bool:
        if not self.cfg.guess_places:
            return False
        # A screenshot or document (sorted out later, or by hand) never keeps a guessed place.
        conn.execute("UPDATE assets SET lat = NULL, lon = NULL, place_guess = NULL, geo_done = 1, city = NULL,"
                     " state = NULL, country = NULL WHERE place_guess IS NOT NULL"
                     " AND category IN ('screenshot', 'document', 'receipt')")
        # Photos are looked at once sorted (so screenshots are known).
        rows = conn.execute(
            "SELECT id, taken_at FROM assets WHERE place_checked = 0 AND lat IS NULL AND position_edited = 0"
            " AND sort_done = 1 AND trashed_at IS NULL AND coalesce(date_guess, '') != 'file'"
            " AND coalesce(category, '') NOT IN ('screenshot', 'document', 'receipt') LIMIT ?", (batch,)).fetchall()
        # Photos that have a place (or can't be guessed) need no look.
        conn.execute("UPDATE assets SET place_checked = 1 WHERE place_checked = 0 AND (lat IS NOT NULL"
                     " OR position_edited = 1 OR coalesce(date_guess, '') = 'file'"
                     " OR coalesce(category, '') IN ('screenshot', 'document', 'receipt'))")
        if not rows:
            conn.commit()
            self.state["running"] = False
            return False
        self.state["running"] = True
        for r in rows:
            if should_stop():
                break
            taken = _parse(r["taken_at"])
            pos = guess(taken, *neighbours(conn, r["taken_at"])) if taken else None
            if pos:
                # Unless a place was set meanwhile, or the date changed (then it is looked at again).
                n = conn.execute("UPDATE assets SET lat = ?, lon = ?, place_guess = 'nearby', geo_done = 0,"
                                 " city = NULL, state = NULL, country = NULL"
                                 " WHERE id = ? AND lat IS NULL AND position_edited = 0 AND taken_at IS ?",
                                 (pos[0], pos[1], r["id"], r["taken_at"])).rowcount
                self.state["guessed"] += n
            conn.execute("UPDATE assets SET place_checked = 1 WHERE id = ? AND taken_at IS ?", (r["id"], r["taken_at"]))
        conn.commit()
        return True
