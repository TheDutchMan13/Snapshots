"""Walks the library folder and keeps the database in step with it.

Safety rules:
* Originals are only read here, never changed, moved or deleted.
* If the library folder is missing (an unplugged drive), nothing is removed.
* If the folder walk hit errors or came back empty, nothing is removed.
* A file that was moved or renamed keeps its albums, favourites and faces
  (it is recognised by its content).
"""

import hashlib
import logging
import os
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from . import db, media
from .config import Config

log = logging.getLogger("snapshots.scanner")

_SKIP_DIRS = {"$recycle.bin", "system volume information", "@eadir", "#recycle", "lost+found"}
_COMMIT_EVERY = 25
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
    removed: int = 0
    skipped_removals: int = 0   # missing entries kept because removing them looked like a mistake
    errors: int = 0
    message: str | None = None
    started_at: str | None = None
    finished_at: str | None = None
    automatic: bool = False


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def sha1_of(path: Path) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


def list_media(root: Path, skip_dir: Path | None = None):
    """Return ([(rel_path, abs_path, size, mtime_ns)], had_errors)."""
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
            found.append((rel, full, st.st_size, st.st_mtime_ns))
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


def index_file(cfg: Config, conn, rel: str, full: Path, digest: str | None = None) -> int:
    """Read one file and insert or update its row. Returns the asset id."""
    st = full.stat()
    kind = media.kind_of(full)
    if kind is None:
        raise ValueError(f"unsupported file type: {full.name}")
    digest = digest or sha1_of(full)

    prev = conn.execute("SELECT id, sha1, has_thumb FROM assets WHERE path = ?", (rel,)).fetchone()
    if prev is not None and prev["sha1"] == digest:
        # Same content, only the timestamp moved: keep everything already known.
        conn.execute("UPDATE assets SET size = ?, mtime_ns = ? WHERE id = ?",
                     (st.st_size, st.st_mtime_ns, prev["id"]))
        if not cfg.thumb_path(prev["id"]).exists():
            _make_thumb(cfg, conn, prev["id"], kind, full)
        return prev["id"]

    info = media.read_photo_info(full) if kind == "photo" else media.read_video_info(full)
    taken = info["taken"] or media.date_from_filename(full.name) or datetime.fromtimestamp(st.st_mtime)
    has_position = info["lat"] is not None
    values = (
        full.name, kind, st.st_size, st.st_mtime_ns, digest, taken.isoformat(timespec="seconds"),
        info["width"], info["height"], info["camera"], info["lat"], info["lon"],
        0 if has_position else 1,           # geo_done: nothing to look up without a position
        0 if kind == "photo" else 1,        # faces_done: faces are found in photos only
    )
    if prev is None:
        cur = conn.execute(
            """
            INSERT INTO assets (filename, kind, size, mtime_ns, sha1, taken_at, width, height, camera,
                                lat, lon, geo_done, faces_done, path, has_thumb, added_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
            """,
            values + (rel, now()),
        )
        asset_id = cur.lastrowid
    else:
        # The content changed: everything worked out from the old picture is stale.
        asset_id = prev["id"]
        forget_faces(cfg, conn, asset_id)
        _unlink(cfg.preview_path(asset_id))
        conn.execute("DELETE FROM clip_vectors WHERE asset_id = ?", (asset_id,))
        conn.execute("DELETE FROM not_duplicates WHERE a = ? OR b = ?", (asset_id, asset_id))
        conn.execute(
            """
            UPDATE assets SET filename = ?, kind = ?, size = ?, mtime_ns = ?, sha1 = ?, taken_at = ?,
                width = ?, height = ?, camera = ?, lat = ?, lon = ?, geo_done = ?, faces_done = ?,
                city = NULL, state = NULL, country = NULL, has_thumb = 0,
                dhash = NULL, sharpness = NULL, brightness = NULL, highlights = NULL, quality_done = 0, keep_quality = 0,
                clip_done = 0, ocr_done = 0, ocr_text = NULL,
                trashed_at = NULL   -- a different picture now: never inherit the old one's trash state
            WHERE id = ?
            """,
            values + (asset_id,),
        )
    _make_thumb(cfg, conn, asset_id, kind, full)
    return asset_id


def _make_thumb(cfg: Config, conn, asset_id: int, kind: str, full: Path) -> None:
    make = media.make_thumb if kind == "photo" else media.make_video_thumb
    ok = make(full, cfg.thumb_path(asset_id))
    conn.execute("UPDATE assets SET has_thumb = ? WHERE id = ?", (1 if ok else 0, asset_id))


class Scanner:
    """Brings the database up to date with the folder. Run by the background worker."""

    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.state = ScanState()

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

    def _scan(self, state: ScanState, remove_missing: bool = False) -> None:
        cfg = self.cfg
        root = cfg.library_path
        if root is None or not root.is_dir():
            state.message = f"Library folder not found: {root}"
            return

        conn = db.connect(cfg.db_path)
        try:
            # Read what we know BEFORE walking the folder. Anything added while the walk runs
            # (an upload, say) is then never mistaken for a file that has disappeared.
            existing = {
                r["path"]: r
                for r in conn.execute(
                    "SELECT a.id, a.path, a.size, a.mtime_ns, a.has_thumb, a.sha1, a.trashed_at,"
                    " (SELECT COUNT(*) FROM album_assets WHERE asset_id = a.id)"
                    " + (SELECT COUNT(*) FROM favorites WHERE asset_id = a.id)"
                    " + (SELECT COUNT(*) FROM faces WHERE asset_id = a.id) AS links"
                    " FROM assets a"
                )
            }

            found, had_errors = list_media(root, skip_dir=cfg.data_dir)
            state.total = len(found)
            state.phase = "indexing"
            seen = {f[0] for f in found}

            # Rows whose file is no longer where we last saw it. A new file with the same
            # content is the same photo moved or renamed, so it keeps its row. Trashed rows are
            # never reused (a kept file must not inherit "in the trash"); among the rest, the
            # one with the most albums, favourites and faces wins.
            missing_by_sha: dict[str, list] = {}
            for path, row in existing.items():
                if path not in seen and row["trashed_at"] is None:
                    missing_by_sha.setdefault(row["sha1"], []).append(row)
            for candidates in missing_by_sha.values():
                candidates.sort(key=lambda r: r["links"])  # pop() takes the best
            moved_away: set[str] = set()

            pending = 0
            for rel, full, size, mtime_ns in found:
                state.done += 1
                row = existing.get(rel)
                unchanged = row is not None and row["size"] == size and row["mtime_ns"] == mtime_ns
                if unchanged and (not row["has_thumb"] or cfg.thumb_path(row["id"]).exists()):
                    continue
                try:
                    digest = None
                    if row is None and missing_by_sha:
                        digest = sha1_of(full)
                        candidates = missing_by_sha.get(digest)
                        taken = conn.execute("SELECT 1 FROM assets WHERE path = ?", (rel,)).fetchone()
                        if candidates and not taken:
                            old = candidates.pop()
                            conn.execute(
                                "UPDATE assets SET path = ?, filename = ?, size = ?, mtime_ns = ? WHERE id = ?",
                                (rel, full.name, size, mtime_ns, old["id"]),
                            )
                            moved_away.add(old["path"])
                            state.moved += 1
                            pending += 1
                            continue
                    index_file(cfg, conn, rel, full, digest)
                    if row is None:
                        state.added += 1
                    else:
                        state.updated += 1
                except Exception as exc:
                    state.errors += 1
                    log.warning("could not index %s: %s", full, exc)
                pending += 1
                if pending >= _COMMIT_EVERY:
                    conn.commit()
                    pending = 0
            conn.commit()

            state.phase = "cleanup"
            gone = [p for p in existing if p not in seen and p not in moved_away]
            if gone and (had_errors or not found):
                state.message = (
                    f"Skipped removing {len(gone)} missing entries because the folder "
                    "could not be read completely."
                )
                state.skipped_removals = len(gone)
                gone = []
            elif gone and not remove_missing and len(gone) >= MASS_REMOVAL_MIN and len(gone) * 2 > len(existing):
                # More than half the library vanished at once: far more likely a wrong folder or a
                # half-connected drive than a real clear-out. Keep everything until someone confirms.
                state.message = (
                    f"{len(gone)} photos were not found, more than half the library. They were kept "
                    "in case the wrong folder is selected or a drive is not fully connected."
                )
                state.skipped_removals = len(gone)
                gone = []
            for path in gone:
                delete_asset_row(cfg, conn, existing[path]["id"])
                state.removed += 1
            conn.commit()
        finally:
            conn.close()
