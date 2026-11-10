"""Walks the library folder and keeps the database in step with it.

Safety rules:
* Originals are only read here, never changed, moved or deleted.
* If a library folder is missing (an unplugged drive), nothing in it is removed.
* If the walk of a folder hit errors or came back empty, nothing in it is removed.
* A file that was moved or renamed keeps its albums, favourites and faces
  (it is recognised by its content).
* A file that is gone from a folder that is there is set aside, not forgotten: its record (albums,
  favourites, faces, description, edits) is hidden everywhere and kept for keep_missing_days. If the
  file comes back in that time, at the same place or another, it carries on as if it never left.
"""

import hashlib
import logging
import os
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import db, libraries, linking, media, webhooks, xmp
from .config import Config

log = logging.getLogger("snapshots.scanner")

_SKIP_DIRS = {"$recycle.bin", "system volume information", "@eadir", "#recycle", "lost+found"}
_video_retried: set[int] = set()
_COMMIT_EVERY = 50          # rows stored together when their files are already read ...
_COMMIT_SECONDS = 0.1       # ... and for no longer than this
MAX_AUTO_THREADS = 6
MASS_REMOVAL_MIN = 20  # below this many, missing files are always tidied away


@dataclass
class ScanState:
    running: bool = False
    phase: str = "idle"  # idle | queued | listing | indexing | cleanup
    done: int = 0
    total: int = 0
    added: int = 0
    updated: int = 0
    moved: int = 0
    removed: int = 0            # files that were gone: taken out of the library (set aside, or forgotten at once)
    restored: int = 0           # set-aside items whose file came back
    forgotten: int = 0          # set-aside items whose time ran out: their records were deleted
    skipped_removals: int = 0   # missing entries kept because removing them looked like a mistake
    errors: int = 0
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    automatic: bool = False


_adding: set[str] = set()      # files Snapshots is putting into the library itself (trim, still, import)
_adding_lock = threading.Lock()


def hold(path: Path) -> None:
    """A scan leaves this file alone until release(): its row is being made by whoever holds it."""
    with _adding_lock:
        _adding.add(_norm(path))


def release(path: Path) -> None:
    with _adding_lock:
        _adding.discard(_norm(path))


def _held(path: Path) -> bool:
    with _adding_lock:
        return bool(_adding) and _norm(path) in _adding


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def scan_threads(cfg: Config) -> int:
    """How many threads read files during a scan: the scan_threads setting, or (0) one fewer than the
    CPU has cores, between 2 and MAX_AUTO_THREADS (a single-core machine gets 1)."""
    wanted = int(getattr(cfg, "scan_threads", 0) or 0)
    if wanted > 0:
        return min(wanted, 32)
    cores = os.cpu_count() or 1
    return 1 if cores <= 1 else max(2, min(MAX_AUTO_THREADS, cores - 1))


def sha1_of(path: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


def list_media(root: Path, skip_dir: Path | None = None, prefix: str = ""):
    """Return ([(stored_path, abs_path, size, mtime_ns)], had_errors)."""
    found = []
    errors = []
    skip = _norm(skip_dir) if skip_dir else None
    for dirpath, dirnames, filenames in os.walk(root, onerror=errors.append):
        dirnames[:] = [
            d
            for d in dirnames
            if not d.startswith(".")
            and d.lower() not in _SKIP_DIRS
            and _norm(Path(dirpath) / d) != skip
        ]
        for name in filenames:
            if name.startswith(".") or media.kind_of(name) is None:
                continue
            full = Path(dirpath) / name
            try:
                st = full.stat()
            except OSError:
                errors.append(OSError(f"cannot stat {full}"))
                continue
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            found.append((prefix + rel, full, st.st_size, st.st_mtime_ns))
    return found, bool(errors)


def _unlink(p: Path) -> None:
    try:
        p.unlink()
    except OSError:
        pass


def forget_faces(cfg: Config, conn, asset_id: int) -> None:
    """Remove an asset's faces and their little pictures."""
    for (face_id,) in conn.execute("SELECT id FROM faces WHERE asset_id = ?", (asset_id,)).fetchall():
        _unlink(cfg.face_path(face_id))
    conn.execute("DELETE FROM faces WHERE asset_id = ?", (asset_id,))


def delete_asset_row(cfg: Config, conn, asset_id: int) -> None:
    """Forget an asset: its row, thumbnails and faces. Does not touch the original file."""
    forget_faces(cfg, conn, asset_id)
    conn.execute("DELETE FROM assets WHERE id = ?", (asset_id,))
    _unlink(cfg.thumb_path(asset_id))
    _unlink(cfg.preview_path(asset_id))
    _unlink(cfg.motion_path(asset_id))
    _unlink(cfg.edited_path(asset_id))
    _unlink(cfg.video_path(asset_id))


class Incomplete(Exception):
    """store() needs a file's details, and prepare() had only read its checksum."""


@dataclass
class Hint:
    """What the database is believed to hold for a path, so prepare() can skip work that isn't needed."""
    sha1: str
    edits: str | None
    thumb_ok: bool


@dataclass
class Prepared:
    """What prepare() read from one file. Nothing in it came from, or went to, the database."""
    rel: str
    full: Path
    size: int
    mtime_ns: int
    kind: str
    digest: str | None
    complete: bool = False          # the details below were read (a new or changed file)
    info: dict | None = None
    side: dict | None = None
    motion: tuple | None = None
    projection: str | None = None
    thumb: Path | None = None       # a thumbnail waiting in a temporary file
    thumb_made: bool = False        # a thumbnail was attempted (thumb None: the file gave no picture)
    thumb_edits: str | None = None  # the edits it was made with

    def discard(self) -> None:
        if self.thumb is not None:
            _unlink(self.thumb)
            self.thumb = None


def _new_thumb_dir(cfg: Config) -> Path:
    return cfg.thumb_dir / "new"


def _thumb_into(cfg: Config, p: Prepared, edits: str | None) -> None:
    """Make the file's thumbnail in a temporary file (it gets its real name once the item has an id)."""
    p.discard()
    folder = _new_thumb_dir(cfg)
    folder.mkdir(parents=True, exist_ok=True)
    tmp = folder / f"{uuid.uuid4().hex}.jpg"
    if p.kind == "photo":
        ok = media.make_thumb(p.full, tmp, media.parse_edits(edits))
    else:
        ok = media.make_video_thumb(p.full, tmp)
    if ok:
        p.thumb = tmp
    else:
        _unlink(tmp)
    p.thumb_made, p.thumb_edits = True, edits


def prepare(cfg: Config, rel: str, full: Path, digest: str | None = None, hint: Hint | None = None,
            maybe_moved=frozenset()) -> Prepared:
    """Read everything a file's row needs: checksum, date, size, position, sidecar and thumbnail.

    This is the slow part of indexing. It never touches the database, so it can run while nothing is
    locked, and on any thread. `hint` is what the database is believed to hold for this path: a file whose
    content matches it needs no more than its checksum (and a thumbnail if that is gone). A new file whose
    checksum is in `maybe_moved` is probably a known photo at a new place, and is not read further either.
    store() says so (Incomplete) if it turns out to need more.
    """
    st = full.stat()        # before the content is read: a file changed meanwhile is seen as changed next time
    kind = media.kind_of(full)
    if kind is None:
        raise ValueError(f"unsupported file type: {full.name}")
    p = Prepared(rel, full, st.st_size, st.st_mtime_ns, kind, digest or sha1_of(full))
    if hint is not None and hint.sha1 == p.digest:
        if not hint.thumb_ok:
            _thumb_into(cfg, p, hint.edits)
        return p
    if hint is None and p.digest in maybe_moved:
        return p
    p.info = media.read_photo_info(full) if kind == "photo" else media.read_video_info(full)
    try:
        p.side = xmp.read_for(full)    # a sidecar from another program (or from Snapshots, before a move)
    except Exception:
        p.side = None
    if kind == "photo":
        p.motion = media.find_motion_video(full)
        p.projection = media.read_projection(full)
    _thumb_into(cfg, p, hint.edits if hint is not None else None)
    p.complete = True
    return p


def _place_thumb(cfg: Config, conn, asset_id: int, p: Prepared, edits: str | None) -> None:
    """Give an item its thumbnail: the one prepare() made if it fits, else a new one (rare)."""
    if not (p.thumb_made and p.thumb_edits == edits):
        _thumb_into(cfg, p, edits)
    ok = p.thumb is not None
    if ok:
        try:
            os.replace(p.thumb, cfg.thumb_path(asset_id))
            p.thumb = None
        except OSError:
            ok = False
            p.discard()
    conn.execute("UPDATE assets SET has_thumb = ? WHERE id = ?", (1 if ok else 0, asset_id))


def store(cfg: Config, conn, p: Prepared, replaces: int | None = None) -> tuple[int, str]:
    """Insert or update the row for a file prepare() has read. Returns (asset id, what happened):
    "added", "updated" or "restored" (a set-aside item whose file is back).

    Only quick database work happens here, so the database is locked for a moment, not for as long as it
    takes to read a photo. Raises Incomplete if the file's details are needed and were not read.
    """
    rel, full, digest = p.rel, p.full, p.digest
    prev = conn.execute(
        "SELECT id, sha1, has_thumb, taken_at, taken_at_file, position_edited, lat, lon, edits, missing_since"
        " FROM assets WHERE path = ?", (rel,)).fetchone()
    if prev is not None and prev["missing_since"] and prev["sha1"] != digest:
        # The file that was here went missing, and a different one now has its name (an upload or a
        # camera reusing IMG_0001, say). It is a new item, and must not inherit the old one's albums.
        delete_asset_row(cfg, conn, prev["id"])
        prev = None
    if prev is not None and prev["sha1"] == digest:
        # Same content, only the timestamp moved (or the file is back): keep everything already known.
        conn.execute("UPDATE assets SET size = ?, mtime_ns = ?, missing_since = NULL WHERE id = ?",
                     (p.size, p.mtime_ns, prev["id"]))
        if not cfg.thumb_path(prev["id"]).exists():
            _place_thumb(cfg, conn, prev["id"], p, prev["edits"])
        return prev["id"], "restored" if prev["missing_since"] else "updated"
    if prev is None:
        back = set_aside_copy(conn, digest)
        if back is None and replaces is not None:
            # The caller found this item's file gone from a folder that is there (an upload of the same
            # photo, see routes_library.save_upload). The same rules as for a set-aside item.
            back = conn.execute(
                "SELECT id, kind, edits FROM assets WHERE id = ? AND sha1 = ? AND trashed_at IS NULL"
                " AND locked_by IS NULL", (replaces, digest)).fetchone()
        if back is not None:
            # A file that went missing is here again under another name (uploaded again, say):
            # it carries on with everything it had.
            conn.execute(
                "UPDATE assets SET path = ?, filename = ?, size = ?, mtime_ns = ?, missing_since = NULL"
                " WHERE id = ?", (rel, full.name, p.size, p.mtime_ns, back["id"]))
            if not cfg.thumb_path(back["id"]).exists():
                _place_thumb(cfg, conn, back["id"], p, back["edits"])
            return back["id"], "restored"
    if not p.complete:
        raise Incomplete(rel)

    info, kind = p.info, p.kind
    side = p.side if prev is None else None
    lat, lon = info["lat"], info["lon"]
    if lat is None and side and side.get("lat") is not None:
        lat, lon = side["lat"], side["lon"]
    # The date: the file's own, else a sidecar's, else (if the settings allow guessing) the GPS fix's time,
    # else one in the file name, else when the file was last changed. date_guess says which guess it is.
    guess = None
    taken = info["taken"] or (side or {}).get("taken")
    if taken is None and cfg.guess_dates and info.get("gps_taken"):
        taken, guess = media.local_from_gps(info["gps_taken"], lon), "gps"
    if taken is None:
        taken = media.date_from_filename(full.name)
    if taken is None:
        taken, guess = datetime.fromtimestamp(p.mtime_ns / 1e9), "file"
    taken = taken.isoformat(timespec="seconds")
    taken_file = None
    if prev is not None and prev["taken_at_file"]:
        # Someone set this item's date by hand: that stays, whatever the changed file says.
        taken, taken_file, guess = prev["taken_at"], taken, None
    if prev is not None and prev["position_edited"]:
        lat, lon = prev["lat"], prev["lon"]
    has_position = lat is not None
    motion, projection = p.motion, p.projection
    values = (
        full.name, kind, p.size, p.mtime_ns, digest, taken,
        info["width"], info["height"], info["camera"], lat, lon,
        0 if has_position else 1,           # geo_done: nothing to look up without a position
        0 if kind == "photo" else 1,        # faces_done: faces are found in photos only
        motion[0] if motion else None, motion[1] if motion else None, projection, taken_file, guess,
    )
    if prev is None:
        cur = conn.execute(
            """
            INSERT INTO assets (filename, kind, size, mtime_ns, sha1, taken_at, width, height, camera,
                                lat, lon, geo_done, faces_done, motion_offset, motion_length, projection,
                                taken_at_file, date_guess, motion_done, pano_done, path, has_thumb, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, 1, ?, 0, ?)
            """,
            values + (rel, now()),
        )
        asset_id = cur.lastrowid
        if has_position:
            # Photos without a place taken around then may now get one (placeguess.py).
            conn.execute("UPDATE assets SET place_checked = 0 WHERE place_checked = 1 AND lat IS NULL"
                         " AND position_edited = 0 AND taken_at BETWEEN ? AND ?",
                         ((datetime.fromisoformat(taken) - timedelta(hours=2)).isoformat(timespec="seconds"),
                          (datetime.fromisoformat(taken) + timedelta(hours=2)).isoformat(timespec="seconds")))
    else:
        # The content changed: everything worked out from the old picture is stale. What people
        # added by hand (date, position, description, edits, albums, tags) stays.
        asset_id = prev["id"]
        forget_faces(cfg, conn, asset_id)
        _unlink(cfg.preview_path(asset_id))
        _unlink(cfg.edited_path(asset_id))
        _unlink(cfg.video_path(asset_id))
        _unlink(cfg.motion_path(asset_id))
        for (owner,) in conn.execute("SELECT id FROM assets WHERE live_video = ?", (asset_id,)).fetchall():
            _unlink(cfg.motion_path(owner))  # a Live Photo's clip changed: its playable copy is stale
        conn.execute("DELETE FROM clip_vectors WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM pet_sightings WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM not_duplicates WHERE a = ? OR b = ?", (asset_id, asset_id))
        conn.execute(
            """
            UPDATE assets SET filename = ?, kind = ?, size = ?, mtime_ns = ?, sha1 = ?, taken_at = ?,
                width = ?, height = ?, camera = ?, lat = ?, lon = ?, geo_done = ?, faces_done = ?,
                motion_offset = ?, motion_length = ?, projection = ?, taken_at_file = ?, date_guess = ?,
                date_checked = 0, motion_done = 1, pano_done = 1, vcodec = NULL,
                city = NULL, state = NULL, country = NULL, has_thumb = 0,
                dhash = NULL, sharpness = NULL, brightness = NULL, highlights = NULL, quality_done = 0, keep_quality = 0,
                clip_done = 0, ocr_done = 0, ocr_text = NULL, sort_done = 0, beauty = NULL, faceq_done = 0,
                colors = NULL, colors_done = 0, turn = NULL, turn_done = 0, pets_done = 0,
                place_guess = NULL, place_checked = 0, caption = NULL, caption_done = 0,
                landmark = NULL, landmark_done = 0, eyes_closed = 0,
                trashed_at = NULL,  -- a different picture now: never inherit the old one's trash state
                missing_since = NULL
            WHERE id = ?
            """,
            values + (asset_id,),
        )
    if info.get("keywords"):
        linking.add_keywords(conn, asset_id, info["keywords"])
    if side:
        xmp.apply_on_index(conn, asset_id, side)
    _place_thumb(cfg, conn, asset_id, p, prev["edits"] if prev is not None else None)
    return asset_id, "added" if prev is None else "updated"


def index_file(cfg: Config, conn, rel: str, full: Path, digest: str | None = None,
               replaces: int | None = None) -> int:
    """Read one file and insert or update its row. Returns the asset id.

    `replaces`: an item with the same content whose own file is gone; the new file takes its place.

    The file is read, and its thumbnail made, before anything is written (see prepare and store), so a
    caller that has written nothing yet locks the database only for a moment.
    """
    row = conn.execute("SELECT id, sha1, edits FROM assets WHERE path = ?", (rel,)).fetchone()
    hint = Hint(row["sha1"], row["edits"], cfg.thumb_path(row["id"]).exists()) if row is not None else None
    p = prepare(cfg, rel, full, digest, hint)
    try:
        try:
            return store(cfg, conn, p, replaces)[0]
        except Incomplete:      # the row changed between the look above and the write (very rare)
            digest = p.digest
            p.discard()
            p = prepare(cfg, rel, full, digest)
            return store(cfg, conn, p, replaces)[0]
    finally:
        p.discard()


def set_aside_copy(conn, digest: str):
    """A set-aside item with this content that a new file may take over, or None.

    Never one in the trash (a kept file must not inherit "in the trash") and never one in someone's
    locked folder (an upload must not disappear into it, or show what is locked).
    """
    return conn.execute(
        "SELECT id, kind, edits FROM assets WHERE sha1 = ? AND missing_since IS NOT NULL"
        " AND trashed_at IS NULL AND locked_by IS NULL ORDER BY id LIMIT 1", (digest,)).fetchone()


def _make_thumb(cfg: Config, conn, asset_id: int, kind: str, full: Path, edits: str | None = None) -> None:
    if kind == "photo":
        ok = media.make_thumb(full, cfg.thumb_path(asset_id), media.parse_edits(edits))
    else:
        ok = media.make_video_thumb(full, cfg.thumb_path(asset_id))
    conn.execute("UPDATE assets SET has_thumb = ? WHERE id = ?", (1 if ok else 0, asset_id))


@dataclass
class _Job:
    """One file a scan has work for."""
    number: int                 # its place in the list of files found (for the progress count)
    rel: str
    full: Path
    row: object | None          # what the database knew about this path before the folders were walked
    mode: str                   # "index": read it and store it; "thumb": a video's thumbnail, tried again;
                                # "back": a set-aside item whose file is here again, unchanged
    thumb_ok: bool = False


class Scanner:
    """Brings the database up to date with the folder. Run by the background worker."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = ScanState()
        self.stopping = threading.Event()   # set when Snapshots is closing: a scan under way ends early

    def snapshot(self) -> dict:
        return asdict(self.state)

    def mark_queued(self, automatic: bool = False) -> None:
        self.state = ScanState(running=True, phase="queued", automatic=automatic, started_at=now())

    def run(self, automatic: bool = False, remove_missing: bool = False) -> ScanState:
        if not self.state.running:
            self.mark_queued(automatic)
        state = self.state
        state.phase = "listing"
        try:
            self._scan(state, remove_missing)
        except Exception as exc:  # keep the server alive whatever happens
            log.exception("scan failed")
            state.message = f"Scan failed: {exc}"
        finally:
            state.running = False
            state.phase = "idle"
            state.finished_at = now()
        return state

    def _clear_old_thumbs(self) -> None:
        """Thumbnails that never got their name (Snapshots was stopped mid-scan): remove those over an hour old."""
        try:
            limit = time.time() - 3600
            for entry in os.scandir(_new_thumb_dir(self.cfg)):
                if entry.is_file() and entry.stat().st_mtime < limit:
                    _unlink(Path(entry.path))
        except OSError:
            pass

    def _read(self, job: _Job, maybe_moved):
        """The slow, database-free part of one job. Returns a Prepared (None for "back"), or the exception."""
        try:
            if job.mode == "back":
                return None
            if job.mode == "thumb":
                st = job.full.stat()
                p = Prepared(job.rel, job.full, st.st_size, st.st_mtime_ns, "video", None)
                try:
                    _thumb_into(self.cfg, p, None)
                except Exception as exc:
                    log.info("no thumbnail for %s: %s", job.full, exc)
                return p
            row = job.row
            hint = Hint(row["sha1"], row["edits"], job.thumb_ok) if row is not None else None
            return prepare(self.cfg, job.rel, job.full, None, hint, maybe_moved)
        except Exception as exc:
            return exc

    def _read_all(self, jobs, maybe_moved):
        """Yield (job, what was read, whether the next one is ready too) for each job, in order.

        Files are read on several threads (scan_threads), a few ahead of the one being stored. Reading is
        mostly decoding pictures and checksums, which Python does outside its lock, so the threads really
        run side by side. Only reading happens there: every database write stays on the calling thread.
        """
        threads = scan_threads(self.cfg)
        if threads <= 1:
            for job in jobs:
                yield job, self._read(job, maybe_moved), False
            return
        ahead: deque = deque()      # (job, future or None), in order
        jobs = iter(jobs)
        pool = ThreadPoolExecutor(max_workers=threads, thread_name_prefix="scan")

        def fill() -> None:
            while len(ahead) < threads * 3:
                job = next(jobs, None)
                if job is None:
                    return
                ahead.append((job, None if job.mode == "back" else pool.submit(self._read, job, maybe_moved)))

        try:
            fill()
            while ahead:
                job, future = ahead.popleft()
                result = future.result() if future is not None else None
                fill()
                ready = bool(ahead) and (ahead[0][1] is None or ahead[0][1].done())
                yield job, result, ready
        finally:
            # Stopped early (an error, or Snapshots closing): nothing that was read ahead is left behind.
            for _, future in ahead:
                if future is not None:
                    future.cancel()
            pool.shutdown(wait=True)
            for _, future in ahead:
                if future is not None and not future.cancelled():
                    left = future.result()
                    if isinstance(left, Prepared):
                        left.discard()

    def _scan(self, state: ScanState, remove_missing: bool = False) -> None:
        cfg = self.cfg
        all_roots = libraries.roots(cfg)
        main = next((r for r in all_roots if r.main), None)
        if main is None or not main.available():
            state.message = f"Library folder not found: {cfg.library_path}"
            return

        conn = db.connect(cfg.db_path)
        try:
            # Read what we know BEFORE walking the folders. Anything added while the walk runs
            # (an upload, say) is then never mistaken for a file that has disappeared.
            existing = {
                r["path"]: r
                for r in conn.execute(
                    "SELECT a.id, a.path, a.kind, a.size, a.mtime_ns, a.has_thumb, a.sha1, a.trashed_at, a.edits,"
                    " a.missing_since,"
                    " (SELECT COUNT(*) FROM album_assets WHERE asset_id = a.id)"
                    " + (SELECT COUNT(*) FROM favorites WHERE asset_id = a.id)"
                    " + (SELECT COUNT(*) FROM faces WHERE asset_id = a.id) AS links"
                    " FROM assets a"
                )
            }

            found = []
            protected: list[str] = []       # folders whose missing files must not be forgotten this time
            unavailable = []
            for root in all_roots:
                if not root.available():
                    unavailable.append(root)
                    protected.append(root.prefix)
                    continue
                files, errors = list_media(root.path, skip_dir=cfg.data_dir, prefix=root.prefix)
                if errors or not files:
                    protected.append(root.prefix)
                found += files
            state.total = len(found)
            state.phase = "indexing"
            seen = {f[0] for f in found}

            def belongs_to_protected(path: str) -> bool:
                r = libraries.root_of(cfg, path)
                return r is not None and r.prefix in protected

            # Rows whose file is no longer where we last saw it. A new file with the same
            # content is the same photo moved or renamed, so it keeps its row. Trashed rows are
            # never reused (a kept file must not inherit "in the trash"); among the rest, the
            # one with the most albums, favourites and faces wins.
            missing_by_sha: dict[str, list] = {}
            for path, row in existing.items():
                if path not in seen and row["trashed_at"] is None and not belongs_to_protected(path):
                    missing_by_sha.setdefault(row["sha1"], []).append(row)
            for candidates in missing_by_sha.values():
                candidates.sort(key=lambda r: r["links"])  # pop() takes the best
            moved_away: set[str] = set()

            added_ids: list[int] = []
            maybe_moved = frozenset(missing_by_sha)
            self._clear_old_thumbs()

            def jobs():
                """The files that need something done, in the order they were found."""
                for number, (rel, full, size, mtime_ns) in enumerate(found, 1):
                    if _held(full):
                        continue
                    row = existing.get(rel)
                    if row is None:
                        yield _Job(number, rel, full, None, "index")
                        continue
                    unchanged = row["size"] == size and row["mtime_ns"] == mtime_ns
                    if unchanged and row["kind"] == "video" and not row["has_thumb"] and row["id"] not in _video_retried:
                        # Videos left without a picture (no ffmpeg then): try once per run, now OpenCV can help.
                        _video_retried.add(row["id"])
                        yield _Job(number, rel, full, row, "thumb")
                        continue
                    thumb_ok = cfg.thumb_path(row["id"]).exists()
                    if unchanged and (not row["has_thumb"] or thumb_ok):
                        if row["missing_since"]:
                            yield _Job(number, rel, full, row, "back")
                        continue
                    yield _Job(number, rel, full, row, "index", thumb_ok)

            def write(job: _Job, p: Prepared | None) -> None:
                """Store one file's result. Quick: the slow reading was done before."""
                row = job.row
                if job.mode == "back":
                    # Set aside by an earlier scan, and here again: it carries on with everything it had.
                    conn.execute("UPDATE assets SET missing_since = NULL WHERE id = ?", (row["id"],))
                    state.restored += 1
                    return
                if job.mode == "thumb":
                    ok = p.thumb is not None
                    if ok:
                        try:
                            os.replace(p.thumb, cfg.thumb_path(row["id"]))
                            p.thumb = None
                        except OSError:
                            ok = False
                    conn.execute("UPDATE assets SET has_thumb = ?, missing_since = NULL WHERE id = ?",
                                 (1 if ok else 0, row["id"]))
                    if row["missing_since"]:
                        state.restored += 1
                    return
                if row is None and missing_by_sha:
                    candidates = missing_by_sha.get(p.digest)
                    taken = conn.execute("SELECT 1 FROM assets WHERE path = ?", (job.rel,)).fetchone()
                    if candidates and not taken:
                        old = candidates.pop()
                        conn.execute(
                            "UPDATE assets SET path = ?, filename = ?, size = ?, mtime_ns = ?,"
                            " missing_since = NULL WHERE id = ?",
                            (job.rel, job.full.name, p.size, p.mtime_ns, old["id"]),
                        )
                        moved_away.add(old["path"])
                        if old["missing_since"]:
                            state.restored += 1
                        else:
                            state.moved += 1
                        return
                try:
                    asset_id, what = store(cfg, conn, p)
                except Incomplete:
                    # It looked like a photo that had moved (or one already known), and is not: read the
                    # rest of it now. Nothing stays locked while that happens.
                    conn.commit()
                    again = prepare(cfg, job.rel, job.full, p.digest)
                    try:
                        asset_id, what = store(cfg, conn, again)
                    finally:
                        again.discard()
                if what == "added":
                    state.added += 1
                    added_ids.append(asset_id)
                elif what == "restored":
                    state.restored += 1
                else:
                    state.updated += 1

            # Each file is read (checksum, date, thumbnail) with nothing written and nothing locked.
            # Its row is then stored, and committed before the scan next has to wait for a file to be
            # read, so the rest of Snapshots (favourites, uploads, logging in) never waits for that.
            # Rows whose files are already read are stored together, for a moment at most.
            pending, since = 0, time.monotonic()
            reader = self._read_all(jobs(), maybe_moved)
            for job, result, more_ready in reader:
                if self.stopping.is_set():
                    if isinstance(result, Prepared):
                        result.discard()
                    break
                state.done = job.number
                try:
                    if isinstance(result, Exception):
                        raise result
                    write(job, result)
                except Exception as exc:
                    state.errors += 1
                    log.warning("could not index %s: %s", job.full, exc)
                finally:
                    if isinstance(result, Prepared):
                        result.discard()
                pending += 1
                if not more_ready or pending >= _COMMIT_EVERY or time.monotonic() - since > _COMMIT_SECONDS:
                    conn.commit()
                    pending, since = 0, time.monotonic()
            conn.commit()
            reader.close()
            if self.stopping.is_set():
                # Snapshots is closing. What was stored so far is kept; nothing is tidied away on
                # half a scan, and the next scan carries on from here.
                return
            state.done = len(found)

            state.phase = "cleanup"
            gone = [p for p in existing if p not in seen and p not in moved_away]
            kept = [p for p in gone if belongs_to_protected(p)]
            gone = [p for p in gone if not belongs_to_protected(p)]
            if kept:
                where = ", ".join(str(r.path) for r in unavailable) or "the folder"
                state.message = (
                    f"Skipped removing {len(kept)} missing entries because {where} "
                    "could not be read completely."
                )
                state.skipped_removals = len(kept)
            # Files an earlier scan already set aside, and files that are newly gone.
            waiting = [p for p in gone if existing[p]["missing_since"]]
            gone = [p for p in gone if not existing[p]["missing_since"]]
            present = sum(1 for r in existing.values() if not r["missing_since"])
            if gone and not remove_missing and len(gone) >= MASS_REMOVAL_MIN and len(gone) * 2 > present:
                # More than half the library vanished at once: far more likely a wrong folder or a
                # half-connected drive than a real clear-out. Keep everything until someone confirms.
                state.message = (
                    f"{len(gone)} photos were not found, more than half the library. They were kept "
                    "in case the wrong folder is selected or a drive is not fully connected."
                )
                state.skipped_removals = len(gone) + len(kept)
                gone = []
            if gone:
                from .health import awaiting_restore
                restorable = awaiting_restore(cfg, conn)
                gone = [p for p in gone if existing[p]["id"] not in restorable]
            keep_days = max(0.0, float(cfg.keep_missing_days))
            stamp = now()
            for path in gone:
                if keep_days > 0:
                    # Set aside: hidden everywhere, with its albums, favourites and faces kept.
                    conn.execute("UPDATE assets SET missing_since = ? WHERE id = ? AND missing_since IS NULL",
                                 (stamp, existing[path]["id"]))
                else:
                    delete_asset_row(cfg, conn, existing[path]["id"])
                state.removed += 1
            limit = (datetime.now() - timedelta(days=keep_days)).isoformat(timespec="seconds")
            for path in waiting:
                if existing[path]["missing_since"] <= limit:
                    delete_asset_row(cfg, conn, existing[path]["id"])
                    state.forgotten += 1
            if state.removed and keep_days > 0 and not state.message:
                days = int(keep_days) if keep_days == int(keep_days) else round(keep_days, 1)
                state.message = (
                    f"{state.removed} {'file was' if state.removed == 1 else 'files were'} not found. "
                    f"Their albums, favourites and people are kept for {days} "
                    f"{'day' if days == 1 else 'days'} in case the files come back."
                )
            conn.commit()
            try:
                linking.link_pairs(conn, cfg)
                conn.commit()
            except Exception:
                log.exception("could not link Live Photos and RAW + JPEG pairs")
                conn.rollback()
            try:
                webhooks.emit_added(conn, added_ids, "scan")
                conn.commit()
            except Exception:
                log.exception("could not queue webhooks")
                conn.rollback()
        finally:
            conn.close()
