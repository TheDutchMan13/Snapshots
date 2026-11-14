"""Writing an edit into a photo's own file, and putting the old file back.

Edits are normally only a recipe (media.py) and the original is never touched. This is the one place that
changes an original, and only when an administrator asks. What keeps it safe:

* The old file is copied to <data>/original-files/<asset id>/ first, and the copy is checked, before
  anything is replaced. Nothing here ever deletes one (only emptying the trash does, with its photo).
* Only a file that is exactly the one Snapshots indexed (same size and time, or same content) is changed:
  never one somebody saved over, or that a sync program changed, since.
* Only formats that can be written back without losing anything: ordinary JPEG and opaque PNG. Not
  RAW, HEIC, photos with a video inside (Live and Motion Photos), 360° photos, pictures with several
  frames or with transparency, and nothing bigger than 12,000 pixels (the editor's limit).
* The new file is written next to the old one under another name, read back, and swapped in with one
  rename: a power cut leaves the old file or the new one, never half of either.
* The item stays the same item (albums, favourites, tags, comments, date, place). What was worked out
  from the old picture (faces, search words, colours) is looked at again, as for any changed file.
"""

from __future__ import annotations

import os
import shutil
import time
from datetime import datetime
from pathlib import Path

from PIL import Image

from . import media, scanner
from .config import Config

MAX_SIDE = 12000        # render_edited() never goes beyond this: a bigger picture would be shrunk
SUFFIXES = {".jpg", ".jpeg", ".png"}


class Refused(Exception):
    """Nothing was changed. `status` is the HTTP status, `message` a sentence for the person."""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status
        self.message = message


def row_of(conn, asset_id: int):
    """The columns every check here needs (the usual asset row leaves some out)."""
    return conn.execute(
        "SELECT id, path, kind, size, mtime_ns, sha1, width, height, edits, locked_by, trashed_at, missing_since,"
        " motion_offset, live_video, projection FROM assets WHERE id = ?", (asset_id,)).fetchone()


# ------------------------------------------------------------------ checks

def _format_of(full: Path) -> str:
    """"JPEG" or "PNG" when the file can be rewritten without loss, else Refused."""
    if full.suffix.lower() not in SUFFIXES:
        raise Refused(415, "Only JPEG and PNG photos can be changed in the file. "
                           "RAW, HEIC and other formats keep the edit as a recipe; Download edited gives a copy.")
    try:
        with Image.open(full) as im:
            fmt, mode, frames = im.format, im.mode, getattr(im, "n_frames", 1)
    except Exception:
        raise Refused(415, "Snapshots could not read this file, so it was not changed.") from None
    if fmt == "JPEG" and mode in ("RGB", "L"):
        return fmt
    if fmt == "PNG" and mode in ("RGB", "L") and frames == 1:
        return fmt
    raise Refused(415, "This file has something a rewrite would lose (transparency, several pictures, "
                       "or unusual colours), so it was not changed.")


def _check(cfg: Config, row, full: Path) -> str:
    """Everything that must be true before this photo's file may be replaced. Returns the file format."""
    if row["locked_by"] is not None:
        raise Refused(403, "Move it out of the locked folder first.")
    if row["kind"] != "photo":
        raise Refused(400, "Only photos can be changed.")
    if row["trashed_at"] is not None:
        raise Refused(400, "Take it out of the trash first.")
    if row["missing_since"]:
        raise Refused(404, "The file is no longer on disk.")
    if row["motion_offset"] is not None or row["live_video"] is not None:
        raise Refused(409, "This is a Live or Motion Photo: rewriting the file would lose its video.")
    if row["projection"]:
        raise Refused(409, "This is a 360° photo or panorama: rewriting the file would lose how it is shown.")
    if max(row["width"] or 0, row["height"] or 0) > MAX_SIDE:
        raise Refused(413, f"This photo is bigger than {MAX_SIDE} pixels on a side. It would be made smaller, so it was not changed.")
    fmt = _format_of(full)
    st = full.stat()
    if not (st.st_size == row["size"] and st.st_mtime_ns == row["mtime_ns"]):
        try:
            same = scanner.sha1_of(full) == row["sha1"]
        except OSError:
            same = False
        if not same:
            raise Refused(409, "The file was changed outside Snapshots since it was last looked at. "
                               "Scan the library, then try again.")
    if not os.access(full, os.W_OK) or not os.access(full.parent, os.W_OK):
        raise Refused(403, "Snapshots is not allowed to change this file or its folder.")
    return fmt


def _room(path: Path, need: int) -> None:
    """Is there room for `need` more bytes where `path` is? (Refused if not, before anything is written.)"""
    probe = path
    while not probe.exists() and probe != probe.parent:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return
    if free < need + 50 * 1024 * 1024:
        raise Refused(507, "There is not enough free room on the disk for this. Nothing was changed.")


# ------------------------------------------------------------------ the old file is kept

def _keep_old_file(cfg: Config, conn, asset_id: int, full: Path, sha1: str, user_id: int | None) -> int:
    """Copy `full` into the original-files folder and check the copy. Returns the new file_backups id."""
    folder = cfg.original_files_dir / str(asset_id)
    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    name = f"{stamp}-{sha1[:12]}{full.suffix.lower()}"
    n = 1
    while (folder / name).exists():
        n += 1
        name = f"{stamp}-{sha1[:12]}-{n}{full.suffix.lower()}"
    dest = folder / name
    try:
        shutil.copy2(full, dest)
        if scanner.sha1_of(dest) != sha1:
            raise OSError("the copy does not match the file")
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise Refused(500, f"The old file could not be kept safely ({exc}). Nothing was changed.") from None
    cur = conn.execute("INSERT INTO file_backups (asset_id, name, sha1, size, created_at, created_by)"
                       " VALUES (?, ?, ?, ?, ?, ?)",
                       (asset_id, name, sha1, dest.stat().st_size, scanner.now(), user_id))
    conn.commit()           # recorded before the file is replaced: whatever happens next, the copy is known
    return cur.lastrowid


def _drop_keep(cfg: Config, conn, backup_id: int, asset_id: int, name: str) -> None:
    """The change did not happen: the copy made for it is not needed (it only ever drops one made a moment ago)."""
    conn.execute("DELETE FROM file_backups WHERE id = ?", (backup_id,))
    conn.commit()
    (cfg.original_files_dir / str(asset_id) / name).unlink(missing_ok=True)


def backups_of(conn, asset_id: int) -> list[dict]:
    rows = conn.execute("SELECT id, created_at, size, restored_at FROM file_backups WHERE asset_id = ?"
                        " ORDER BY id DESC", (asset_id,)).fetchall()
    return [{"id": r["id"], "created_at": r["created_at"], "size": r["size"], "restored": bool(r["restored_at"])}
            for r in rows]


def forget(cfg: Config, conn, asset_id: int) -> int:
    """The photo itself was deleted (the administrator emptied the trash): so are the old files kept for it."""
    names = [r["name"] for r in conn.execute("SELECT name FROM file_backups WHERE asset_id = ?", (asset_id,))]
    conn.execute("DELETE FROM file_backups WHERE asset_id = ?", (asset_id,))
    folder = cfg.original_files_dir / str(asset_id)
    for name in names:
        (folder / name).unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass
    return len(names)


def kept_bytes(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(size), 0) FROM file_backups").fetchone()[0]


# ------------------------------------------------------------------ swapping a file in

def _swap_in(full: Path, write) -> None:
    """write(tmp) makes the new file; it is swapped in with one rename. The scanner leaves the file alone meanwhile."""
    tmp = full.with_name(f".{full.name}.snapshots-edit")
    scanner.hold(full)
    try:
        write(tmp)
        for attempt in range(10):       # antivirus or the search indexer may hold the file for a moment
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


def _exif_and_profile(full: Path):
    with Image.open(full) as im:
        exif = im.getexif()
        icc = im.info.get("icc_profile")
    if exif:
        if 274 in exif:
            exif[274] = 1                       # the picture is drawn the right way up now
        ifd = exif.get_ifd(0x8769)
        for tag in (0xA002, 0xA003):            # the size the file used to have
            ifd.pop(tag, None)
    return exif, icc


def _write_picture(img: Image.Image, fmt: str, exif, icc, dest: Path) -> None:
    kw = {}
    if exif:
        kw["exif"] = exif
    if icc:
        kw["icc_profile"] = icc
    if fmt == "JPEG":
        img.save(dest, "JPEG", quality=95, optimize=True, **kw)
    else:
        img.save(dest, "PNG", optimize=True, **kw)
    with Image.open(dest) as back:               # read it back before it is trusted
        if back.format != fmt or back.size != img.size:
            raise OSError("the new file did not come out as planned")
        back.load()


def _reindex(cfg: Config, conn, row, full: Path) -> None:
    """The file changed: the same item now describes the new picture, and carries no edit recipe (it is in the file)."""
    asset_id = row["id"]
    try:
        conn.execute("UPDATE assets SET edits = NULL, edit_v = NULL WHERE id = ?", (asset_id,))
        scanner.index_file(cfg, conn, row["path"], full)
        conn.execute("UPDATE assets SET edits = NULL, edit_v = NULL WHERE id = ?", (asset_id,))
        for p in (cfg.edited_path(asset_id), cfg.edited_dir / f"{asset_id}.pano.jpg",
                  cfg.edited_dir / f"{asset_id}.full.jpg"):
            p.unlink(missing_ok=True)
        conn.commit()
    except Exception:
        # The file has changed but its row could not follow. The recipe must not be applied a second time
        # to the new picture, and the next scan will read the file again.
        conn.rollback()
        conn.execute("UPDATE assets SET edits = NULL, edit_v = NULL WHERE id = ?", (asset_id,))
        conn.commit()
        raise Refused(500, "The file was changed, but Snapshots could not read it again just now. "
                           "A scan will pick it up.") from None


# ------------------------------------------------------------------ the two actions

def apply_edits(cfg: Config, conn, row, full: Path, user_id: int | None) -> int:
    """Write the photo's edit recipe into its own file. Returns the id of the kept copy of the old file."""
    if not row["edits"]:
        raise Refused(400, "There is nothing to save into the file: edit the photo first.")
    edits = media.parse_edits(row["edits"])
    fmt = _check(cfg, row, full)
    size = full.stat().st_size
    _room(cfg.original_files_dir, size)
    _room(full.parent, size * 2)
    try:
        img = media.render_edited(full, edits, 0)
        exif, icc = _exif_and_profile(full)
    except Exception:
        raise Refused(415, "Snapshots could not read this photo to change it. Nothing was changed.") from None
    backup_id = _keep_old_file(cfg, conn, row["id"], full, row["sha1"], user_id)
    name = conn.execute("SELECT name FROM file_backups WHERE id = ?", (backup_id,)).fetchone()["name"]
    st = full.stat()

    def write(tmp: Path) -> None:
        _write_picture(img, fmt, exif, icc, tmp)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))     # the file's date stays: it may be the photo's only date

    try:
        _swap_in(full, write)
    except Exception as exc:
        _drop_keep(cfg, conn, backup_id, row["id"], name)       # the file was not replaced: no copy needed
        raise Refused(500, f"The file could not be written ({exc}). Nothing was changed.") from None
    _reindex(cfg, conn, row, full)
    return backup_id


def restore_file(cfg: Config, conn, row, full: Path, backup_id: int | None, user_id: int | None) -> int:
    """Put an old copy back as the photo's file (the newest if none is named). What is in the file now is
    kept first, so this can be undone as well. Returns the id of the copy that was put back."""
    if backup_id is None:
        pick = conn.execute("SELECT id FROM file_backups WHERE asset_id = ? AND restored_at IS NULL"
                            " ORDER BY id DESC LIMIT 1", (row["id"],)).fetchone()
    else:
        pick = conn.execute("SELECT id FROM file_backups WHERE id = ? AND asset_id = ?",
                            (backup_id, row["id"])).fetchone()
    if pick is None:
        raise Refused(404, "There is no older file kept for this photo.")
    old = conn.execute("SELECT id, name, sha1 FROM file_backups WHERE id = ?", (pick["id"],)).fetchone()
    src = cfg.original_files_dir / str(row["id"]) / old["name"]
    try:
        if not src.is_file() or scanner.sha1_of(src) != old["sha1"]:
            raise Refused(409, "The kept copy of the old file is missing or damaged, so nothing was changed.")
    except OSError:
        raise Refused(409, "The kept copy of the old file could not be read, so nothing was changed.") from None
    _check_restorable(row, full)
    size = src.stat().st_size
    _room(cfg.original_files_dir, full.stat().st_size)
    _room(full.parent, size * 2)
    current_kept = None
    cur_sha = scanner.sha1_of(full)
    if cur_sha != old["sha1"]:
        current_kept = _keep_old_file(cfg, conn, row["id"], full, cur_sha, user_id)   # undo stays possible
    st = full.stat()

    def write(tmp: Path) -> None:
        shutil.copyfile(src, tmp)
        if scanner.sha1_of(tmp) != old["sha1"]:
            raise OSError("the copy changed on the way")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))

    try:
        _swap_in(full, write)
    except Exception as exc:
        if current_kept is not None:
            name = conn.execute("SELECT name FROM file_backups WHERE id = ?", (current_kept,)).fetchone()["name"]
            _drop_keep(cfg, conn, current_kept, row["id"], name)
        raise Refused(500, f"The old file could not be put back ({exc}). Nothing was changed.") from None
    conn.execute("UPDATE file_backups SET restored_at = ? WHERE id = ?", (scanner.now(), old["id"]))
    conn.commit()
    _reindex(cfg, conn, row, full)
    return old["id"]


def _check_restorable(row, full: Path) -> None:
    if row["locked_by"] is not None:
        raise Refused(403, "Move it out of the locked folder first.")
    if row["trashed_at"] is not None:
        raise Refused(400, "Take it out of the trash first.")
    if row["missing_since"] or not full.is_file():
        raise Refused(404, "The file is no longer on disk.")
    if not os.access(full, os.W_OK) or not os.access(full.parent, os.W_OK):
        raise Refused(403, "Snapshots is not allowed to change this file or its folder.")
    st = full.stat()
    if not (st.st_size == row["size"] and st.st_mtime_ns == row["mtime_ns"]):
        try:
            same = scanner.sha1_of(full) == row["sha1"]
        except OSError:
            same = False
        if not same:
            raise Refused(409, "The file was changed outside Snapshots since it was last looked at. "
                               "Scan the library, then try again.")
