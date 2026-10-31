"""Dates for files that have none: from the photos beside them.

A file with no date inside it (a photo saved by an app, an old scan, a picture copied without its
details) is dated by when the file was last changed, which is often years off: the day it was copied.
Cameras and phones number their files, so IMG_1234.JPG with no date was almost certainly taken between
IMG_1233.JPG and IMG_1235.JPG. This job finds such neighbours in the same folder (same name apart from
the number, numbers close together) and dates the file between them.

It also looks once at files indexed by older versions, which did not record where a date came from:
a date equal to the file's own change time is taken to be that guess.
"""

import logging
import re
from datetime import datetime

log = logging.getLogger("snapshots.dateguess")

NUMBERED = re.compile(r"^(.*?)(\d+)(\D*)\.[^.]+$")
MAX_GAP = 20                 # neighbours at most this many numbers away
MAX_SPAN_DAYS = 3            # two neighbours further apart than this in time say nothing about what's between


def _numbered(name: str):
    m = NUMBERED.match(name)
    if not m:
        return None
    return m.group(1).lower(), int(m.group(2)), m.group(3).lower()


def guess_between(name: str, neighbours: list[tuple[str, str]]) -> str | None:
    """A date for `name` from [(filename, taken_at)] of dated files in the same folder, or None."""
    me = _numbered(name)
    if me is None:
        return None
    prefix, number, suffix = me
    before = after = None        # (gap, taken_at)
    for other, taken in neighbours:
        n = _numbered(other)
        if n is None or n[0] != prefix or n[2] != suffix or n[1] == number:
            continue
        gap = n[1] - number
        if -MAX_GAP <= gap < 0 and (before is None or -gap < before[0]):
            before = (-gap, taken)
        elif 0 < gap <= MAX_GAP and (after is None or gap < after[0]):
            after = (gap, taken)
    if before and after:
        a, b = datetime.fromisoformat(before[1]), datetime.fromisoformat(after[1])
        if b < a or (b - a).days > MAX_SPAN_DAYS:
            return None
        share = before[0] / (before[0] + after[0])
        return (a + (b - a) * share).isoformat(timespec="seconds")
    one = before or after
    if one and one[0] <= 3:
        return one[1]
    return None


def _folder_rows(conn, folder: str, columns: str, extra: str = "", params: tuple = ()):
    """Rows directly in `folder` ("" = the top of the main library), found with the index on path."""
    if folder:
        return conn.execute(
            f"SELECT {columns} FROM assets WHERE path >= ? AND path < ? AND instr(substr(path, ?), '/') = 0 {extra}",
            (folder, folder + "\U0010ffff", len(folder) + 1) + params).fetchall()
    return conn.execute(f"SELECT {columns} FROM assets WHERE instr(path, '/') = 0 {extra}", params).fetchall()


class DateGuessJob:
    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "guessed": 0, "message": None}

    def run_batch(self, conn, now: float, should_stop, batch: int = 200) -> bool:
        if not self.cfg.guess_dates:
            return False
        rows = conn.execute(
            "SELECT id, path, filename, taken_at, mtime_ns, date_guess FROM assets"
            " WHERE date_checked = 0 AND taken_at_file IS NULL AND trashed_at IS NULL LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state["running"] = False
            return False
        self.state["running"] = True
        dated: dict[str, list] = {}
        for r in rows:
            if should_stop():
                break
            guess = r["date_guess"]
            if guess is None and r["mtime_ns"]:
                # Indexed by an older version: is the date simply the file's change time?
                changed = datetime.fromtimestamp(r["mtime_ns"] / 1e9).isoformat(timespec="seconds")
                if changed == r["taken_at"]:
                    guess = "file"
                    conn.execute("UPDATE assets SET date_guess = 'file' WHERE id = ?", (r["id"],))
            if guess == "file":
                folder = r["path"].rsplit("/", 1)[0] + "/" if "/" in r["path"] else ""
                if folder not in dated:
                    dated[folder] = self._dated_in(conn, folder)
                taken = guess_between(r["filename"], [(n, t) for n, t in dated[folder] if n != r["filename"]])
                if taken:
                    # Unless someone set a date by hand meanwhile.
                    n = conn.execute("UPDATE assets SET taken_at = ?, date_guess = 'nearby'"
                                     " WHERE id = ? AND taken_at_file IS NULL", (taken, r["id"])).rowcount
                    self.state["guessed"] += n
            conn.execute("UPDATE assets SET date_checked = 1 WHERE id = ?", (r["id"],))
        conn.commit()
        return True

    @staticmethod
    def _dated_in(conn, folder: str) -> list[tuple[str, str]]:
        """(filename, taken_at) of the files in `folder` whose date is their own."""
        return [(r[0], r[1]) for r in _folder_rows(
            conn, folder, "filename, taken_at",
            "AND trashed_at IS NULL AND coalesce(date_guess, '') NOT IN ('file', 'nearby')")]
