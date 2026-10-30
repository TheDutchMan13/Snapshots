"""Offsite copies of the photos themselves (the database backups only hold what Snapshots built around them).

Three ways, set with offsite_method:

* folder: a plain copy in another folder, like an external drive, a NAS share, or a folder that OneDrive,
  Google Drive or Dropbox keeps in the cloud. New and changed files are copied; files you delete stay in
  the copy, so a mistake in the library can't empty the copy too. The newest database backup goes along.
* rclone: the same, to anywhere rclone can reach (Backblaze B2, S3, Google Drive and many more); use an
  rclone "crypt" remote to have it encrypted. Needs rclone (https://rclone.org), set up with `rclone config`.
* restic: encrypted and versioned snapshots in a restic repository (a folder, or cloud storage). Needs
  restic (https://restic.net) and a password, in the file named by offsite_password_file (or the
  RESTIC_PASSWORD_FILE / RESTIC_PASSWORD environment variables). A new repository is made if needed.

The copy is brought up to date every offsite_hours while Snapshots runs, in a thread of its own, so it never
holds up anything else. It only reads your library.
"""

import json
import logging
import os
import shutil
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path

from . import backup, libraries
from .scanner import _SKIP_DIRS

log = logging.getLogger("snapshots.offsite")

STATE_FILE = "offsite.json"
README = """This folder is a copy of your Snapshots photo library, made by Snapshots.

"Library" holds the main library folder; other folders hold the extra library folders.
"Database" holds the newest copy of the Snapshots database (albums, people, favourites and so on).
To use it: restore the database with  Snapshots.exe --restore-backup "Database\\snapshots.db"
and point Snapshots at the library copy.

Files deleted from the library are kept here. Delete them here yourself if you want them gone.
"""


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _skip_dir(name: str) -> bool:
    return name.startswith(".") or name.lower() in _SKIP_DIRS


def _norm(p: Path) -> str:
    return os.path.normcase(os.path.abspath(p))


def _inside(child: Path, parent: Path) -> bool:
    c, p = _norm(child), _norm(parent)
    return c == p or c.startswith(p.rstrip("\\/") + os.sep)


def folder_names(roots) -> dict:
    """Where each library folder goes in the copy: "Library", then the extra folders by name."""
    out, used = {}, {"library", "database"}
    for r in roots:
        if r.main:
            out[r.prefix] = "Library"
            continue
        base = r.name or "Folder"
        name, n = base, 2
        while name.lower() in used:
            name, n = f"{base} ({n})", n + 1
        used.add(name.lower())
        out[r.prefix] = name
    return out


def problem(cfg, method: str | None = None, target: str | None = None) -> str | None:
    """Why an offsite copy can't be made with these settings, or None."""
    method = cfg.offsite_method if method is None else method
    target = (cfg.offsite_target if target is None else target).strip()
    if not method:
        return None
    if not target:
        return "Say where the copy goes."
    if method == "folder":
        dest = Path(target)
        if not dest.is_absolute():
            return "Give the whole path of the folder, like E:\\Photo copy."
        for r in libraries.roots(cfg):
            if _inside(dest, r.path) or _inside(r.path, dest):
                return f"The copy can't go inside the library ({r.path}), or the library inside it."
        if _inside(dest, cfg.data_dir):
            return "The copy can't go inside Snapshots' own data folder."
        return None
    if method in ("rclone", "restic"):
        if not shutil.which(method):
            return f"{method} isn't installed (or isn't on the PATH)."
        if method == "restic" and not (cfg.offsite_password_file or os.environ.get("RESTIC_PASSWORD_FILE")
                                       or os.environ.get("RESTIC_PASSWORD")):
            return "restic needs a password: set offsite_password_file in config.toml to a file holding it."
        return None
    return "Unknown method."


def _same(src_stat, dest: Path) -> bool:
    try:
        d = dest.stat()
    except OSError:
        return False
    return d.st_size == src_stat.st_size and abs(d.st_mtime - src_stat.st_mtime) < 2.5   # FAT keeps 2 seconds


def copy_folder(cfg, dest: Path, progress, should_stop) -> dict:
    """Bring a plain folder copy up to date. Returns counts."""
    if not dest.parent.exists() and not dest.exists():
        raise OSError(f"{dest} can't be reached. Is the drive connected?")
    dest.mkdir(parents=True, exist_ok=True)
    roots = [r for r in libraries.roots(cfg) if r.available()]
    names = folder_names(libraries.roots(cfg))
    work = []
    for r in roots:
        for dirpath, dirnames, filenames in os.walk(r.path):
            dirnames[:] = [d for d in dirnames if not _skip_dir(d) and not _inside(Path(dirpath) / d, cfg.data_dir)
                           and not _inside(Path(dirpath) / d, dest)]
            for name in filenames:
                if name.startswith(".") or name.endswith(".part"):
                    continue
                full = Path(dirpath) / name
                work.append((full, dest / names[r.prefix] / full.relative_to(r.path)))
    counts = {"files": len(work), "copied": 0, "unchanged": 0, "failed": 0, "bytes": 0, "errors": []}
    for n, (src, out) in enumerate(work):
        if should_stop():
            raise InterruptedError("stopped")
        if n % 50 == 0:
            progress(n, len(work))
        try:
            st = src.stat()
            if _same(st, out):
                counts["unchanged"] += 1
                continue
            out.parent.mkdir(parents=True, exist_ok=True)
            tmp = out.with_name(out.name + ".snapshots-part")
            shutil.copy2(src, tmp)
            os.replace(tmp, out)
            counts["copied"] += 1
            counts["bytes"] += st.st_size
        except OSError as exc:
            counts["failed"] += 1
            if len(counts["errors"]) < 10:
                counts["errors"].append(f"{src}: {exc.strerror or exc}")
    db_copy = backup.backup_now(cfg)
    (dest / "Database").mkdir(exist_ok=True)
    shutil.copy2(db_copy, dest / "Database" / "snapshots.db.part")
    os.replace(dest / "Database" / "snapshots.db.part", dest / "Database" / "snapshots.db")
    (dest / "README.txt").write_text(README, encoding="utf-8")
    progress(len(work), len(work))
    return counts


def _run_tool(cmd: list[str], env=None, should_stop=None, timeout: float = 24 * 3600) -> tuple[int, str]:
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, env=env)
    lines: list[str] = []
    started = time.time()

    def read():
        for raw in proc.stdout:
            lines.append(raw.decode("utf-8", "replace").rstrip())
            del lines[:-40]
    reader = threading.Thread(target=read, daemon=True)
    reader.start()
    while proc.poll() is None:
        if (should_stop and should_stop()) or time.time() - started > timeout:
            proc.kill()
            proc.wait()
            raise InterruptedError("stopped")
        time.sleep(0.2)
    reader.join(5)
    return proc.returncode, "\n".join(lines[-10:])


EXCLUDES = ["$RECYCLE.BIN/**", "System Volume Information/**", "@eaDir/**", "#recycle/**", ".*/**", ".*"]


def copy_rclone(cfg, target: str, progress, should_stop) -> dict:
    exe = shutil.which("rclone")
    roots = [r for r in libraries.roots(cfg) if r.available()]
    names = folder_names(libraries.roots(cfg))
    target = target.rstrip("/\\")
    for n, r in enumerate(roots):
        progress(n, len(roots) + 1)
        cmd = [exe, "copy", str(r.path), f"{target}/{names[r.prefix]}", "--modify-window", "2s"]
        for pattern in EXCLUDES:
            cmd += ["--exclude", pattern]
        code, out = _run_tool(cmd, should_stop=should_stop)
        if code != 0:
            raise RuntimeError(f"rclone failed ({code}): {out[-500:]}")
    db_copy = backup.backup_now(cfg)
    code, out = _run_tool([exe, "copyto", str(db_copy), f"{target}/Database/snapshots.db"], should_stop=should_stop)
    if code != 0:
        raise RuntimeError(f"rclone failed ({code}): {out[-500:]}")
    progress(1, 1)
    return {"folders": len(roots)}


def copy_restic(cfg, target: str, progress, should_stop) -> dict:
    exe = shutil.which("restic")
    env = dict(os.environ)
    if cfg.offsite_password_file:
        env["RESTIC_PASSWORD_FILE"] = str(cfg.offsite_password_file)
    repo = ["-r", target]
    code, _ = _run_tool([exe, *repo, "cat", "config"], env, should_stop, timeout=600)
    if code != 0:
        code, out = _run_tool([exe, *repo, "init"], env, should_stop, timeout=600)
        if code != 0:
            raise RuntimeError(f"restic couldn't open or make the repository ({code}): {out[-500:]}")
    progress(0, 1)
    db_copy = backup.backup_now(cfg)
    roots = [str(r.path) for r in libraries.roots(cfg) if r.available()]
    cmd = [exe, *repo, "backup", *roots, str(db_copy), "--tag", "snapshots"]
    for pattern in ("$RECYCLE.BIN", "System Volume Information", "@eaDir", "#recycle", ".*"):
        cmd += ["--exclude", pattern]
    code, out = _run_tool(cmd, env, should_stop)
    if code not in (0, 3):   # 3: some files couldn't be read, the rest was saved
        raise RuntimeError(f"restic failed ({code}): {out[-500:]}")
    progress(1, 1)
    return {"folders": len(roots), "incomplete": code == 3}


class OffsiteJob:
    """Keeps the offsite copy up to date. The worker asks it now and then; the copy runs in its own thread."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None, "last": None, "result": None,
                      "done": 0, "total": 0}
        self._thread: threading.Thread | None = None
        self._stop = False
        self._load()

    def _state_path(self) -> Path:
        return self.cfg.data_dir / STATE_FILE

    def _load(self) -> None:
        try:
            saved = json.loads(self._state_path().read_text(encoding="utf-8"))
            self.state.update(last=saved.get("last"), result=saved.get("result"), message=saved.get("message"))
        except (OSError, ValueError):
            pass

    def _save(self) -> None:
        try:
            self._state_path().write_text(json.dumps({k: self.state[k] for k in ("last", "result", "message")}),
                                          encoding="utf-8")
        except OSError:
            pass

    def enabled(self) -> bool:
        return bool(self.cfg.offsite_method and self.cfg.offsite_target)

    def due(self, now: float) -> bool:
        if not self.enabled() or not self.cfg.offsite_hours or self.cfg.offsite_hours <= 0 or now < self.retry_at:
            return False
        if self.running():
            return False
        last = self.state.get("last")
        if last:
            age = time.time() - datetime.fromisoformat(last).timestamp()
            if age < self.cfg.offsite_hours * 3600:
                self.retry_at = now + self.cfg.offsite_hours * 3600 - age
                return False
        return True

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def run_batch(self, conn, now: float, should_stop, batch: int = 0) -> bool:
        if self.due(now):
            self.start()
        return False   # never holds up other work

    def start(self) -> bool:
        if self.running():
            return False
        self._stop = False
        self.state.update(running=True, done=0, total=0, message=None)
        self._thread = threading.Thread(target=self._run, name="snapshots-offsite", daemon=True)
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop = True

    def _run(self) -> None:
        cfg = self.cfg
        method, target = cfg.offsite_method, cfg.offsite_target.strip()

        def progress(done, total):
            self.state.update(done=done, total=total)

        result, message = None, None
        try:
            why = problem(cfg)
            if why:
                raise RuntimeError(why)
            if method == "folder":
                result = copy_folder(cfg, Path(target), progress, lambda: self._stop)
            elif method == "rclone":
                result = copy_rclone(cfg, target, progress, lambda: self._stop)
            elif method == "restic":
                result = copy_restic(cfg, target, progress, lambda: self._stop)
            result["method"] = method
            if result.get("failed"):
                message = f"{result['failed']} files could not be copied."
        except InterruptedError:
            message = "The offsite copy was stopped."
        except Exception as exc:
            log.warning("offsite copy failed: %s", exc)
            message = f"The offsite copy failed: {exc}"
        finished = _now()
        self.state.update(running=False, message=message)
        if result is not None:
            self.state.update(last=finished, result=dict(result, finished_at=finished))
            self.retry_at = time.time() + max(0.1, cfg.offsite_hours or 24) * 3600
        else:
            self.retry_at = time.time() + 3600   # try again in an hour (a drive may be unplugged)
        self._save()
        from .webhooks import emit_safely
        emit_safely(cfg, "offsite.finished", {"ok": result is not None and not message, "method": method,
                                              "message": message, "result": result})
