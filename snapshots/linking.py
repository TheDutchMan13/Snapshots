"""Files that belong together: Live Photos, motion photos, RAW + JPEG pairs, and keywords as tags.

* An iPhone Live Photo is two files with the same name in the same folder: the photo (IMG_1234.HEIC
  or .JPG) and a short clip (IMG_1234.MOV). The clip is shown with its photo, not on its own.
* A motion photo (Google Pixel, Samsung and other Android phones) is one file: the video is stored
  at the end of the photo. It is found when the photo is indexed.
* A camera that saves RAW + JPEG writes two files with the same name. They are put in a stack, with
  the JPEG on top, unless someone took them apart before.
"""

import logging
import os
from collections import defaultdict
from datetime import datetime

from . import libraries, media

log = logging.getLogger("snapshots.linking")

LIVE_VIDEO_EXTS = {".mov", ".mp4"}
LIVE_VIDEO_MAX_BYTES = 30 * 1024 * 1024   # Live Photo clips are about 3 seconds: a few MB


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _key(path: str) -> tuple[str, str]:
    folder, name = os.path.split(path)
    return folder.lower(), os.path.splitext(name)[0].lower()


def link_pairs(conn, cfg=None) -> dict:
    """Link Live Photos to their clips and stack RAW + JPEG pairs. Safe to run after every scan."""
    rows = conn.execute(
        "SELECT id, path, kind, size, live_video, stack_id, no_auto_stack, trashed_at, locked_by FROM assets"
    ).fetchall()
    groups = defaultdict(list)
    for r in rows:
        groups[_key(r["path"])].append(r)

    want_live: dict[int, int] = {}
    raw_pairs: list[tuple] = []
    for members in groups.values():
        if len(members) < 2:
            continue
        photos = [m for m in members if m["kind"] == "photo"]
        shown = [m for m in photos if not media.is_raw(m["path"])]
        raws = [m for m in photos if media.is_raw(m["path"])]
        clips = [m for m in members if m["kind"] == "video"
                 and os.path.splitext(m["path"])[1].lower() in LIVE_VIDEO_EXTS and m["size"] <= LIVE_VIDEO_MAX_BYTES]
        if len(clips) == 1 and len(shown) <= 1 and (shown or raws):
            owner = shown[0] if shown else raws[0]
            want_live[owner["id"]] = clips[0]["id"]
        if len(shown) == 1 and len(raws) == 1:
            raw_pairs.append((shown[0], raws[0]))

    linked = unlinked = stacked = 0
    for r in rows:
        want = want_live.get(r["id"])
        if r["live_video"] != want:
            conn.execute("UPDATE assets SET live_video = ? WHERE id = ?", (want, r["id"]))
            if cfg is not None:
                cfg.motion_path(r["id"]).unlink(missing_ok=True)
            if want:
                linked += 1
                # The clip follows its photo into (or out of) the trash and the locked folder.
                conn.execute("UPDATE assets SET trashed_at = ?, locked_by = ? WHERE id = ?",
                             (r["trashed_at"], r["locked_by"], want))
            else:
                unlinked += 1

    for jpeg, raw in raw_pairs:
        if jpeg["no_auto_stack"] or raw["no_auto_stack"] or jpeg["stack_id"] or raw["stack_id"]:
            continue
        stack_id = conn.execute("INSERT INTO stacks (cover_id, auto, created_at) VALUES (?, 1, ?)",
                                (jpeg["id"], _now())).lastrowid
        conn.execute("UPDATE assets SET stack_id = ? WHERE id IN (?, ?)", (stack_id, jpeg["id"], raw["id"]))
        stacked += 1

    tidy_stacks(conn)
    return {"live": linked, "unlinked": unlinked, "stacked": stacked}


def tidy_stacks(conn) -> None:
    """A stack needs two photos: dissolve any that lost the rest, and give coverless ones a cover."""
    conn.execute(
        "UPDATE assets SET stack_id = NULL WHERE stack_id IN"
        " (SELECT stack_id FROM assets WHERE stack_id IS NOT NULL GROUP BY stack_id HAVING COUNT(*) < 2)"
    )
    conn.execute("DELETE FROM stacks WHERE id NOT IN (SELECT stack_id FROM assets WHERE stack_id IS NOT NULL)")
    conn.execute(
        "UPDATE stacks SET cover_id = (SELECT MIN(id) FROM assets WHERE stack_id = stacks.id)"
        " WHERE cover_id IS NULL OR cover_id NOT IN (SELECT id FROM assets WHERE stack_id = stacks.id)"
    )


def make_stack(conn, ids: list[int], cover: int | None = None) -> int:
    """Put photos in one stack (taking them out of any other). Returns the stack's id."""
    ids = list(dict.fromkeys(ids))
    cover = cover if cover in ids else ids[0]
    stack_id = conn.execute("INSERT INTO stacks (cover_id, auto, created_at) VALUES (?, 0, ?)",
                            (cover, _now())).lastrowid
    marks = ",".join("?" * len(ids))
    conn.execute(f"UPDATE assets SET stack_id = ?, no_auto_stack = 0 WHERE id IN ({marks})", [stack_id, *ids])
    tidy_stacks(conn)
    return stack_id


def unstack(conn, stack_ids: list[int]) -> int:
    """Take stacks apart. Snapshots won't stack those photos again by itself."""
    n = 0
    for sid in set(stack_ids):
        n += conn.execute("UPDATE assets SET stack_id = NULL, no_auto_stack = 1 WHERE stack_id = ?"
                          " AND locked_by IS NULL", (sid,)).rowcount
        conn.execute("DELETE FROM stacks WHERE id = ?", (sid,))
    return n


def stack_members(conn, stack_id: int) -> list:
    return conn.execute(
        "SELECT a.id, a.filename, a.sha1, a.edit_v, a.taken_at, a.kind, a.width, a.height, (a.id = s.cover_id) AS cover"
        " FROM assets a JOIN stacks s ON s.id = a.stack_id"
        " WHERE a.stack_id = ? AND a.trashed_at IS NULL AND a.locked_by IS NULL ORDER BY (a.id = s.cover_id) DESC, a.taken_at, a.id",
        (stack_id,),
    ).fetchall()


def with_clips(conn, ids: list[int]) -> list[int]:
    """These items plus the Live Photo clips that belong to them (trash and restore move both)."""
    out = list(ids)
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        marks = ",".join("?" * len(chunk))
        out += [r[0] for r in conn.execute(
            f"SELECT live_video FROM assets WHERE id IN ({marks}) AND live_video IS NOT NULL", chunk)]
    return list(dict.fromkeys(out))


def add_keywords(conn, asset_id: int, words: list[str]) -> None:
    """Tags from keywords other programs wrote into the file."""
    for word in words:
        conn.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (word, _now()))
        conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) SELECT ?, id FROM tags WHERE name = ?",
                     (asset_id, word))


class MotionJob:
    """Looks inside photos indexed by older versions of Snapshots: for videos and keywords (before 0.5)
    and for 360° panorama markers (before 0.6)."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE motion_done = 0 OR pano_done = 0").fetchone()[0]

    def run_batch(self, conn, now: float, should_stop, batch: int = 200) -> bool:
        rows = conn.execute("SELECT id, path, motion_done, pano_done FROM assets WHERE motion_done = 0 OR pano_done = 0"
                            " LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state.update(running=False, pending=0)
            return False
        root = self.cfg.library_path
        if root is None or not root.is_dir():
            return False
        self.state["running"] = True
        for row in rows:
            if should_stop():
                break
            path = libraries.full_path(self.cfg, row["path"])
            if path is None or not path.is_file():
                # Its folder isn't connected. Rather than hold up the rest, it is left as it is.
                conn.execute("UPDATE assets SET motion_done = 1, pano_done = 1 WHERE id = ?", (row["id"],))
                continue
            if not row["motion_done"]:
                found = media.find_motion_video(path)
                words = media.read_keywords(path)
                if words:  # keywords in photos indexed before Snapshots read them
                    add_keywords(conn, row["id"], words)
                conn.execute("UPDATE assets SET motion_offset = ?, motion_length = ?, motion_done = 1 WHERE id = ?",
                             (found[0] if found else None, found[1] if found else None, row["id"]))
            if not row["pano_done"]:
                conn.execute("UPDATE assets SET projection = ?, pano_done = 1 WHERE id = ?",
                             (media.read_projection(path), row["id"]))
        conn.commit()
        self.state["pending"] = self.pending(conn)
        return True
