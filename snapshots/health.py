"""The library health check: read every file again and compare it with what Snapshots stored when it was
indexed (its SHA-1 checksum, size and modification time).

What it can find:
* damaged     the content changed, but the file's size and date did not: something changed the bytes without
              any program saving the file. That is how disk damage ("bit rot") and bad copies look.
* unreadable  the file can't be read (a disk error, or Windows refusing access).
* missing     the file is gone, although its folder is there.
* changed     the file was saved again by another program since the last scan (an edit, new metadata).
              Not a problem: the next scan reads it again.
* unavailable the folder's drive isn't connected, so its files couldn't be checked.

For a damaged, unreadable or missing file, a good copy is looked for in the offsite folder copy (if there is
one): a file at the same place whose checksum matches. "Put back the good copy" copies it over.

The check runs in a thread of its own, one file at a time, and can be stopped. It can also run by itself every
few days (health_check_days). It only reads: nothing is changed unless someone restores a file.
"""

import hashlib
import logging
import os
import shutil
import threading
import time
from datetime import datetime
from pathlib import Path

from . import db, libraries, webhooks

log = logging.getLogger("snapshots.health")

PROBLEMS = ("damaged", "unreadable", "missing")
STATUSES = PROBLEMS + ("changed", "unavailable")
CHUNK = 1 << 20


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def check_file(full: Path | None, row, root_available: bool) -> tuple[str, str | None, int]:
    """(status, detail, bytes read) for one item. status is "ok" or one of STATUSES."""
    if full is None or not root_available:
        return "unavailable", None, 0
    try:
        st = full.stat()
    except FileNotFoundError:
        return "missing", None, 0
    except OSError as exc:
        return "unreadable", exc.strerror or str(exc), 0
    digest = hashlib.sha1(usedforsecurity=False)
    read = 0
    try:
        with open(full, "rb") as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
                read += len(chunk)
    except OSError as exc:
        return "unreadable", exc.strerror or str(exc), read
    if digest.hexdigest() == row["sha1"]:
        return "ok", None, read
    if st.st_size == row["size"] and st.st_mtime_ns == row["mtime_ns"]:
        return "damaged", "The content changed, but the file's size and date did not.", read
    return "changed", "Saved again by another program since the last scan.", read


def offsite_copy(cfg, stored: str) -> Path | None:
    """Where the offsite folder copy keeps this item's file (it may not exist)."""
    if cfg.offsite_method != "folder" or not cfg.offsite_target.strip():
        return None
    from .offsite import folder_names

    root = libraries.root_of(cfg, stored)
    if root is None:
        return None
    names = folder_names(libraries.roots(cfg))
    rel = stored[len(root.prefix):]
    base = Path(cfg.offsite_target.strip()) / names[root.prefix]
    p = Path(os.path.normpath(base / rel))
    return p if p.is_relative_to(Path(os.path.normpath(base))) else None


def sha1_of(path: Path) -> str | None:
    digest = hashlib.sha1(usedforsecurity=False)
    try:
        with open(path, "rb") as fh:
            while chunk := fh.read(CHUNK):
                digest.update(chunk)
    except OSError:
        return None
    return digest.hexdigest()


def good_copy(cfg, row) -> Path | None:
    """A copy in the offsite folder whose content is what Snapshots stored for this item."""
    p = offsite_copy(cfg, row["path"])
    if p is None or not p.is_file():
        return None
    try:
        if p.stat().st_size != row["size"]:
            return None
    except OSError:
        return None
    return p if sha1_of(p) == row["sha1"] else None


def _free_name(path: Path) -> Path:
    if not path.exists():
        return path
    n = 2
    while path.with_name(f"{path.name}-{n}").exists():
        n += 1
    return path.with_name(f"{path.name}-{n}")


def restore(cfg, conn, asset_id: int) -> str:
    """Copy the good offsite copy over a damaged, unreadable or missing file. Returns what happened, for people.

    Only for files the last check found a problem with, and only if the file still looks the way it did then:
    a file saved again since (by an editor that keeps the file's date, say) is not overwritten."""
    from . import scanner

    row = conn.execute("SELECT id, path, sha1, size, mtime_ns, locked_by FROM assets WHERE id = ?",
                       (asset_id,)).fetchone()
    if row is None or row["locked_by"] is not None:
        raise LookupError("No such item.")
    run = conn.execute("SELECT id FROM health_runs ORDER BY id DESC LIMIT 1").fetchone()
    issue = conn.execute("SELECT status FROM health_issues WHERE run_id = ? AND asset_id = ?",
                         (run[0] if run else -1, asset_id)).fetchone()
    if issue is None or issue[0] not in PROBLEMS:
        raise LookupError("The last check found nothing wrong with this file.")
    full = libraries.full_path(cfg, row["path"])
    if full is None:
        raise LookupError("The item's folder isn't in the library any more.")
    try:
        st = full.stat()
        exists = True
    except FileNotFoundError:
        exists = False
    if exists:
        if st.st_size != row["size"] or st.st_mtime_ns != row["mtime_ns"]:
            raise ValueError("The file was saved again since the check, so it is left alone. Rescan, then check again.")
        if sha1_of(full) == row["sha1"]:
            conn.execute("DELETE FROM health_issues WHERE asset_id = ?", (asset_id,))
            return "The file is fine now: nothing needed putting back."
    src = good_copy(cfg, row)
    if src is None:
        raise LookupError("There is no good copy of this file in the offsite copy.")
    full.parent.mkdir(parents=True, exist_ok=True)
    tmp = full.with_name(f".{full.name}.snapshots-restore")
    kept = None
    scanner.hold(full)
    try:
        shutil.copy2(src, tmp)
        if sha1_of(tmp) != row["sha1"]:
            raise OSError("The copy changed while it was being put back. Nothing was replaced.")
        if exists:
            # Keep a copy of the damaged file beside it, in case someone wants it after all.
            kept = _free_name(full.with_name(full.name + ".damaged"))
            try:
                shutil.copyfile(full, kept)
            except OSError:
                kept = None      # unreadable: nothing worth keeping
        for attempt in range(10):   # antivirus or the search indexer may hold the new file for a moment
            try:
                os.replace(tmp, full)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.3)
    finally:
        tmp.unlink(missing_ok=True)
        scanner.release(full)
    st = full.stat()
    conn.execute("UPDATE assets SET mtime_ns = ?, size = ? WHERE id = ?", (st.st_mtime_ns, st.st_size, asset_id))
    conn.execute("DELETE FROM health_issues WHERE asset_id = ? AND status IN ('damaged', 'unreadable', 'missing')",
                 (asset_id,))
    return "The good copy was put back." + (f" The damaged file was kept beside it, as {kept.name}." if kept else "")


KEEP_MISSING_DAYS = 7


def awaiting_restore(cfg, conn) -> set[int]:
    """Items the last check found missing that the offsite copy still has. Scans keep them for a few days,
    so the file can be put back instead of the item (with its albums, faces and favourites) being forgotten."""
    row = conn.execute("SELECT id, finished_at FROM health_runs WHERE finished_at IS NOT NULL"
                       " ORDER BY id DESC LIMIT 1").fetchone()
    if row is None or cfg.offsite_method != "folder":
        return set()
    try:
        if time.time() - datetime.fromisoformat(row["finished_at"]).timestamp() > KEEP_MISSING_DAYS * 86400:
            return set()
    except ValueError:
        return set()
    out = set()
    for asset_id, path in conn.execute(
            "SELECT a.id, a.path FROM health_issues h JOIN assets a ON a.id = h.asset_id"
            " WHERE h.run_id = ? AND h.status = 'missing'", (row["id"],)):
        p = offsite_copy(cfg, path)
        if p is not None and p.is_file():
            out.add(asset_id)
    return out


def last_run(conn) -> dict | None:
    row = conn.execute("SELECT * FROM health_runs ORDER BY id DESC LIMIT 1").fetchone()
    if row is None:
        return None
    counts = {s: 0 for s in STATUSES}
    for status, n in conn.execute(
            "SELECT h.status, COUNT(*) FROM health_issues h JOIN assets a ON a.id = h.asset_id"
            " WHERE h.run_id = ? AND a.locked_by IS NULL GROUP BY h.status", (row["id"],)):
        counts[status] = n
    locked = conn.execute(
        "SELECT COUNT(*) FROM health_issues h JOIN assets a ON a.id = h.asset_id"
        " WHERE h.run_id = ? AND a.locked_by IS NOT NULL AND h.status IN ('damaged', 'unreadable', 'missing')",
        (row["id"],)).fetchone()[0]
    return {"id": row["id"], "started_at": row["started_at"], "finished_at": row["finished_at"],
            "stopped": bool(row["stopped"]), "total": row["total"], "checked": row["checked"], "bytes": row["bytes"],
            "counts": counts, "problems": sum(counts[s] for s in PROBLEMS), "locked_problems": locked}


class HealthJob:
    """Runs the check in its own thread; the worker asks it now and then whether a timed check is due."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "done": 0, "total": 0, "bytes": 0, "message": None}
        self._thread: threading.Thread | None = None
        self._stop = False
        self._start_lock = threading.Lock()

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def due(self, conn, now_ts: float) -> bool:
        days = self.cfg.health_check_days
        if not days or days <= 0 or now_ts < self.retry_at or self.running():
            return False
        days = max(1.0, days)
        row = conn.execute("SELECT started_at FROM health_runs WHERE finished_at IS NOT NULL AND stopped = 0"
                           " ORDER BY id DESC LIMIT 1").fetchone()
        if row:
            age = time.time() - datetime.fromisoformat(row[0]).timestamp()
            if age < days * 86400:
                self.retry_at = now_ts + days * 86400 - age
                return False
        return True

    def run_batch(self, conn, now_ts: float, should_stop, batch: int = 0) -> bool:
        if self.due(conn, now_ts):
            self.start(None)
        return False   # never holds up other work

    def start(self, user_id: int | None) -> bool:
        with self._start_lock:
            if self.running():
                return False
            self._stop = False
            self.state.update(running=True, done=0, total=0, bytes=0, message=None)
            self._thread = threading.Thread(target=self._run, args=(user_id,), name="snapshots-health", daemon=True)
            self._thread.start()
            return True

    def stop(self) -> None:
        self._stop = True

    def _run(self, user_id: int | None) -> None:
        cfg = self.cfg
        conn = db.connect(cfg.db_path)
        run_id = None
        try:
            rows = conn.execute("SELECT id, path, sha1, size, mtime_ns FROM assets ORDER BY path").fetchall()
            run_id = conn.execute("INSERT INTO health_runs (started_by, started_at, total) VALUES (?, ?, ?)",
                                  (user_id, now(), len(rows))).lastrowid
            conn.commit()
            self.state["total"] = len(rows)
            roots = {r.prefix: r.available() for r in libraries.roots(cfg)}
            checked = read_total = 0
            last_save = time.time()
            for n, row in enumerate(rows):
                if self._stop:
                    break
                root = libraries.root_of(cfg, row["path"])
                status, detail, read = check_file(libraries.full_path(cfg, row["path"]),
                                                  row, root is not None and roots.get(root.prefix, False))
                checked += 1
                read_total += read
                if status != "ok":
                    # Written at once: the database is never kept locked while the next file is read.
                    conn.execute("INSERT OR REPLACE INTO health_issues (run_id, asset_id, status, detail)"
                                 " VALUES (?, ?, ?, ?)", (run_id, row["id"], status, detail))
                    conn.commit()
                self.state.update(done=n + 1, bytes=read_total)
                if time.time() - last_save > 2:
                    conn.execute("UPDATE health_runs SET checked = ?, bytes = ? WHERE id = ?",
                                 (checked, read_total, run_id))
                    conn.commit()
                    last_save = time.time()
            conn.execute("UPDATE health_runs SET checked = ?, bytes = ?, finished_at = ?, stopped = ? WHERE id = ?",
                         (checked, read_total, now(), int(self._stop), run_id))
            conn.commit()
            summary = last_run(conn)
            if not self._stop:
                webhooks.emit_safely(cfg, "health.finished", {
                    "checked": summary["checked"], "problems": summary["problems"],
                    "counts": {k: v for k, v in summary["counts"].items()}})
        except Exception as exc:   # never let the check die quietly
            log.exception("library health check failed")
            self.state["message"] = f"The check stopped: {exc}"
            try:
                conn.rollback()
                if run_id:
                    conn.execute("UPDATE health_runs SET finished_at = ?, stopped = 1 WHERE id = ?", (now(), run_id))
                    conn.commit()
            except Exception:
                pass
        finally:
            conn.close()
            self.state["running"] = False
            # After a failure, a timed check waits an hour rather than starting again at once.
            self.retry_at = time.time() + 3600 if self.state.get("message") else 0.0


def issues(conn, status: str | None, offset: int, limit: int) -> tuple[list, int]:
    """The last run's problems (not items in locked folders), worst first."""
    run = conn.execute("SELECT id FROM health_runs ORDER BY id DESC LIMIT 1").fetchone()
    if run is None:
        return [], 0
    where = "h.run_id = ? AND a.locked_by IS NULL"
    params: list = [run[0]]
    if status:
        where += " AND h.status = ?"
        params.append(status)
    total = conn.execute(f"SELECT COUNT(*) FROM health_issues h JOIN assets a ON a.id = h.asset_id WHERE {where}",
                         params).fetchone()[0]
    order = "CASE h.status WHEN 'damaged' THEN 0 WHEN 'unreadable' THEN 1 WHEN 'missing' THEN 2" \
            " WHEN 'changed' THEN 3 ELSE 4 END, a.path"
    rows = conn.execute(f"SELECT h.asset_id, h.status, h.detail FROM health_issues h JOIN assets a ON a.id = h.asset_id"
                        f" WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?", params + [limit, offset]).fetchall()
    return [dict(r) for r in rows], total

