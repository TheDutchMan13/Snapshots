"""Copies of the database: albums, tags, people's names, favourites, accounts, edits and comments.

Your photos and videos are your own files and are not part of these backups. The database is what
Snapshots built around them, and a backup lets you get it back after a broken disk or a mistake.

A copy is made every `backup_hours` while Snapshots runs, into `backup_dir` (the "backups" folder in
the data folder unless set; another drive is safer). The newest `backup_keep` copies are kept.
To restore one, close Snapshots and run:  Snapshots.exe --restore-backup "<the backup file>"
"""

import logging
import os
import re
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

from . import db

log = logging.getLogger("snapshots.backup")

NAME = re.compile(r"^snapshots-(\d{8}-\d{6})\.db$")


def list_backups(cfg) -> list[dict]:
    folder = cfg.backups_path
    out = []
    try:
        for p in folder.iterdir():
            m = NAME.match(p.name)
            if m and p.is_file():
                made = datetime.strptime(m.group(1), "%Y%m%d-%H%M%S")
                out.append({"name": p.name, "path": str(p), "size": p.stat().st_size,
                            "made_at": made.isoformat(timespec="seconds")})
    except OSError:
        return []
    out.sort(key=lambda b: b["name"], reverse=True)
    return out


def backup_now(cfg) -> Path:
    """Make a consistent copy of the database (safe while Snapshots is running) and remove old ones."""
    folder = cfg.backups_path
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    dest = folder / f"snapshots-{stamp}.db"
    tmp = dest.with_name(dest.name + ".part")
    src = db.connect(cfg.db_path)
    try:
        out = sqlite3.connect(str(tmp))
        try:
            src.backup(out)
            out.execute("PRAGMA journal_mode=DELETE")  # one self-contained file
        finally:
            out.close()
    finally:
        src.close()
    os.replace(tmp, dest)
    prune(cfg)
    return dest


def prune(cfg) -> None:
    keep = max(1, cfg.backup_keep)
    for b in list_backups(cfg)[keep:]:
        try:
            Path(b["path"]).unlink()
        except OSError:
            pass


def check(path: Path) -> str | None:
    """Why a file can't be restored, or None if it looks like a good Snapshots database."""
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    except sqlite3.Error as exc:
        return f"It can't be opened: {exc}"
    try:
        if conn.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
            return "The file is damaged."
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version > db.SCHEMA_VERSION:
            return "It was made by a newer version of Snapshots."
        if not conn.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'assets'").fetchone():
            return "It isn't a Snapshots database."
    except sqlite3.Error as exc:
        return f"It can't be read: {exc}"
    finally:
        conn.close()
    return None


def restore(cfg, backup: Path) -> Path:
    """Put a backup in place of the database. Snapshots must not be running. The current database is
    kept beside it as snapshots.before-restore.db. Returns that path."""
    problem = check(backup)
    if problem:
        raise ValueError(problem)
    target = cfg.db_path
    kept = target.with_name("snapshots.before-restore.db")
    if target.exists():
        conn = sqlite3.connect(str(target))
        try:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        finally:
            conn.close()
        shutil.copy2(target, kept)
    for extra in (target.with_name(target.name + "-wal"), target.with_name(target.name + "-shm")):
        extra.unlink(missing_ok=True)
    tmp = target.with_name(target.name + ".restoring")
    shutil.copy2(backup, tmp)
    os.replace(tmp, target)
    db.init(target)  # an older backup is brought up to date
    return kept


class BackupJob:
    """Makes a backup every backup_hours. Run by the background worker."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None, "last": None}

    def due(self, now: float) -> bool:
        hours = self.cfg.backup_hours
        if not hours or hours <= 0 or now < self.retry_at:
            return False
        latest = list_backups(self.cfg)
        if latest:
            self.state["last"] = latest[0]["made_at"]
            age = time.time() - datetime.fromisoformat(latest[0]["made_at"]).timestamp()
            if age < hours * 3600:
                self.retry_at = now + hours * 3600 - age
                return False
        return True

    def run_batch(self, conn, now: float, should_stop, batch: int = 0) -> bool:
        if not self.due(now):
            return False
        try:
            path = backup_now(self.cfg)
            self.state.update(message=None, last=datetime.now().isoformat(timespec="seconds"))
            log.info("database backed up to %s", path)
            self.retry_at = now + self.cfg.backup_hours * 3600
        except Exception as exc:
            log.warning("database backup failed: %s", exc)
            self.state["message"] = f"The database backup failed: {exc}"
            self.retry_at = now + 3600
        return False  # never holds up other work
