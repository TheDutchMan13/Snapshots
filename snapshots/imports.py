"""Bringing over what another photo app knew: from Immich, or from a Google Photos export (Takeout).

Both work the same way. Each photo the other app knew is found in Snapshots by its content (the SHA-1 of
the file), wherever it is in the library, so folders and file names don't matter. Photos Snapshots
doesn't have yet can be copied into the library first (into "From Immich" or "From Google Photos").
Then, for each photo:

* favourites become favourites of the person chosen for the import (favourites are per person);
* archived and trashed photos are archived and moved to the trash (both can be undone);
* albums are made, or added to when an album with that name exists (so running an import twice is fine);
* tags are added;
* a date or location changed in the other app is used, and kept as "changed by hand", so Reset in the
  details panel goes back to what the file says;
* a description is used when the photo has none in Snapshots;
* names of people are given to the faces Snapshots finds (now, or once it has looked at the photo).

Your files are never changed. Items in someone's locked folder are left alone.
"""

import hashlib
import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

from . import db, faces, libraries, linking, webhooks
from .scanner import index_file

log = logging.getLogger("snapshots.imports")

DATE_FMT = "%Y-%m-%dT%H:%M:%S"
NEAR = 0.0005          # positions closer than this (about 50 m) are the same place


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


class Cancelled(Exception):
    pass


class Applier:
    """Writes what another app knew about a photo into Snapshots, counting what changed."""

    def __init__(self, conn, user_id: int, source: str):
        self.conn = conn
        self.user_id = user_id
        self.source = source
        self.albums: dict[str, int] = {}
        self.counts = {"favourites": 0, "archived": 0, "trashed": 0, "albums": 0, "album_items": 0, "tags": 0,
                       "dates": 0, "locations": 0, "descriptions": 0, "names": 0}
        self.face_assets: set[int] = set()
        self.names: set[str] = set()

    def album_id(self, name: str) -> int:
        name = " ".join(name.split())[:200] or "Untitled"
        key = name.casefold()
        if key not in self.albums:
            row = self.conn.execute("SELECT id FROM albums WHERE name = ? COLLATE NOCASE AND rule IS NULL"
                                    " ORDER BY id LIMIT 1", (name,)).fetchone()
            if row is None:
                album = self.conn.execute(
                    "INSERT INTO albums (name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
                    (name, self.user_id, _now(), _now())).lastrowid
                self.counts["albums"] += 1
            else:
                album = row[0]
            self.albums[key] = album
        return self.albums[key]

    def apply(self, asset_id: int, meta: dict) -> None:
        conn = self.conn
        row = conn.execute("SELECT id, taken_at, taken_at_file, lat, lon, position_edited, description,"
                           " archived_at, trashed_at, locked_by, faces_done FROM assets WHERE id = ?",
                           (asset_id,)).fetchone()
        if row is None or row["locked_by"] is not None:
            return
        c = self.counts
        if meta.get("favorite"):
            c["favourites"] += conn.execute(
                "INSERT OR IGNORE INTO favorites (user_id, asset_id, created_at) VALUES (?, ?, ?)",
                (self.user_id, asset_id, _now())).rowcount
        if meta.get("archived") and row["archived_at"] is None:
            conn.execute("UPDATE assets SET archived_at = ? WHERE id = ?", (_now(), asset_id))
            c["archived"] += 1
        if meta.get("trashed") and row["trashed_at"] is None:
            conn.execute("UPDATE assets SET trashed_at = ? WHERE id = ?", (_now(), asset_id))
            c["trashed"] += 1
        for name in meta.get("albums") or []:
            album = self.album_id(name)
            n = conn.execute("INSERT OR IGNORE INTO album_assets (album_id, asset_id, added_at) VALUES (?, ?, ?)",
                             (album, asset_id, _now())).rowcount
            if n:
                c["album_items"] += 1
                conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (_now(), album))
        tags = [" ".join(t.split())[:100] for t in meta.get("tags") or []]
        tags = [t for t in tags if t]
        if tags:
            before = conn.total_changes
            linking.add_keywords(conn, asset_id, tags)
            c["tags"] += 1 if conn.total_changes > before else 0
        taken = meta.get("taken_at")
        if taken and not row["taken_at_file"]:
            try:
                new = datetime.strptime(taken[:19], DATE_FMT)
                old = datetime.strptime(row["taken_at"], DATE_FMT)
                if abs((new - old).total_seconds()) > 60:
                    conn.execute("UPDATE assets SET taken_at = ?, taken_at_file = ? WHERE id = ?",
                                 (new.strftime(DATE_FMT), row["taken_at"], asset_id))
                    c["dates"] += 1
            except ValueError:
                pass
        lat, lon = meta.get("lat"), meta.get("lon")
        if lat is not None and lon is not None and not row["position_edited"] \
                and -90 <= lat <= 90 and -180 <= lon <= 180 and not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            if row["lat"] is None or abs(row["lat"] - lat) > NEAR or abs(row["lon"] - lon) > NEAR:
                conn.execute("UPDATE assets SET lat = ?, lon = ?, position_edited = 1, city = NULL, state = NULL,"
                             " country = NULL, geo_done = 0 WHERE id = ?", (round(lat, 6), round(lon, 6), asset_id))
                c["locations"] += 1
        text = (meta.get("description") or "").strip()
        if text and not (row["description"] or "").strip():
            conn.execute("UPDATE assets SET description = ? WHERE id = ?", (text[:5000], asset_id))
            c["descriptions"] += 1
        people = meta.get("people") or []
        if people:
            self.names.update(p["name"] for p in people)
            conn.execute("DELETE FROM face_hints WHERE asset_id = ? AND source = ?", (asset_id, self.source))
            for p in people:
                box = p.get("box")
                conn.execute("INSERT INTO face_hints (asset_id, name, x, y, w, h, source) VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (asset_id, p["name"], *(box if box else (None, None, None, None)), self.source))
            if row["faces_done"]:
                self.face_assets.add(asset_id)

    def finish(self) -> bool:
        """Name faces in photos Snapshots has already looked at. True if people changed."""
        changed = False
        for asset_id in self.face_assets:
            before = self.conn.total_changes
            faces.apply_hints(self.conn, asset_id)
            changed = changed or self.conn.total_changes > before
        self.counts["names"] = len(self.names)
        return changed


def find_assets(conn, sha1: str) -> list[int]:
    """Items with this content, not in anyone's locked folder."""
    return [r[0] for r in conn.execute("SELECT id FROM assets WHERE sha1 = ? AND locked_by IS NULL", (sha1,))]


def claim_name(folder: Path, name: str) -> Path:
    """A free file name in folder, made exclusively so two copies never overwrite each other."""
    folder.mkdir(parents=True, exist_ok=True)
    stem, ext = os.path.splitext(name)
    n = 0
    while True:
        target = folder / (name if n == 0 else f"{stem}-{n}{ext}")
        try:
            open(target, "xb").close()
            return target
        except FileExistsError:
            n += 1


def add_file(cfg, conn, folder: Path, name: str, write, expect_sha1: str | None = None) -> int | None:
    """Write a new file into the main library (write(fh) fills it) and index it. Returns its id, or None
    when what arrived isn't what was expected."""
    from .app import safe_filename

    root = next(r for r in libraries.roots(cfg) if r.main)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f".{uuid.uuid4().hex}.part"     # a hidden name: scans skip it
    digest = hashlib.sha1(usedforsecurity=False)
    try:
        with open(tmp, "wb") as out:
            class Hashing:
                def write(self, data):
                    digest.update(data)
                    return out.write(data)
            write(Hashing())
        if expect_sha1 and digest.hexdigest() != expect_sha1:
            tmp.unlink(missing_ok=True)
            return None
        target = claim_name(folder, safe_filename(name))
        os.replace(tmp, target)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    return index_file(cfg, conn, libraries.stored_path(root, target), target, digest.hexdigest())


class Manager:
    """Runs one import at a time, in a thread of its own, and keeps its progress for the app."""

    def __init__(self, cfg, worker):
        self.cfg = cfg
        self.worker = worker
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.state: dict = {"running": False}
        self._cancel = False

    def snapshot(self) -> dict:
        with self.lock:
            return json.loads(json.dumps(self.state))

    def update(self, **kw) -> None:
        with self.lock:
            self.state.update(kw)

    def check(self) -> None:
        if self._cancel:
            raise Cancelled()

    def cancel(self) -> None:
        self._cancel = True

    def start(self, kind: str, run, user_id: int, dry_run: bool) -> bool:
        """run(manager) does the work and returns a summary dict."""
        with self.lock:
            if self.thread and self.thread.is_alive():
                return False
            self._cancel = False
            self.state = {"running": True, "kind": kind, "dry_run": dry_run, "phase": "Starting", "done": 0,
                          "total": 0, "started_at": _now(), "finished_at": None, "summary": None, "error": None}
            self.thread = threading.Thread(target=self._run, args=(kind, run, user_id, dry_run),
                                           name=f"snapshots-import-{kind}", daemon=True)
            self.thread.start()
        return True

    def _run(self, kind, run, user_id, dry_run) -> None:
        started = _now()
        summary, error = None, None
        try:
            summary = run(self)
        except Cancelled:
            error = "Stopped."
        except Exception as exc:
            log.exception("%s import failed", kind)
            error = str(exc) or exc.__class__.__name__
        self.update(running=False, phase="Finished" if not error else "Stopped", summary=summary, error=error,
                    finished_at=_now())
        if dry_run:
            return
        try:
            with db.session(self.cfg.db_path) as conn:
                conn.execute("INSERT INTO imports (kind, started_by, started_at, finished_at, summary) VALUES (?, ?, ?, ?, ?)",
                             (kind, user_id, started, _now(), json.dumps({"summary": summary, "error": error})))
                webhooks.emit(conn, "import.finished", {"kind": kind, "summary": summary, "error": error})
        except Exception:
            log.exception("could not record the import")
        self.worker.poke()   # new photos need thumbnails' faces, places and so on
        self.worker.faces.invalidate()


def progress_every(seconds: float = 0.5):
    """A little helper: True at most every `seconds`."""
    last = [0.0]

    def due() -> bool:
        now = time.monotonic()
        if now - last[0] >= seconds:
            last[0] = now
            return True
        return False
    return due
