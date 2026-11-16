"""Writing an edit into a photo's own file, and putting the old file back.

Edits are normally only a recipe (media.py) and the original is never touched. This is the one place that
changes an original, and only when an administrator asks. What keeps it safe:

* The old file is copied to <data>/original-files/<asset id>/ first, the copy is checked and written
  through to the disk, before anything is replaced. Nothing here deletes one except forget(), when an
  administrator empties the trash with its photo in it.
* Only a file that is exactly the one Snapshots indexed (same size and time, or same content) is changed:
  never one somebody saved over, or that a sync program changed, since.
* Only files that can be written back without losing what is in them: ordinary JPEG and opaque PNG. Not
  RAW, HEIC, photos with a video inside (Live and Motion Photos), 360° photos, pictures with several
  frames or with transparency, files with more data after the picture (a depth map, a second picture),
  files whose notes describe places in the picture (face regions) or point at such extra data, and
  nothing bigger than 12,000 pixels (the editor's limit). The date, camera, place, colour profile,
  XMP notes, IPTC captions and keywords, comment and print size are carried over into the new file.
  (A camera maker's private notes are copied as they are, and some programs may no longer read them.)
* The new file is written next to the old one under another name, read back, written through to the
  disk, and swapped in with one rename: a power cut leaves the old file or the new one.
* The item stays the same item (albums, favourites, tags, comments, date, place). What was worked out
  from the old picture (faces, search words, colours) is looked at again, as for any changed file.
* The old content still counts as "already in Snapshots" (known_old_content): the phone and the imports
  must not bring the original back as a second item.
* When an item's record goes away without the trash (its file vanished, its folder was taken out of
  Snapshots), its kept files are moved to original-files/forgotten/ (detach): they are not deleted, and
  they can never show up under a later photo that gets the same number.
"""

from __future__ import annotations

import io
import logging
import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path

from PIL import Image, JpegImagePlugin, PngImagePlugin

from . import media, scanner
from .config import Config

log = logging.getLogger("snapshots.filechange")

MAX_SIDE = 12000        # render_edited() never goes beyond this: a bigger picture would be shrunk
SUFFIXES = {".jpg", ".jpeg", ".png"}
FORGOTTEN = "forgotten"  # inside original-files: kept files of items whose record is gone

# Notes (XMP) that describe places in the picture, or point at data stored beside it: after a turn or a
# crop they would be wrong, or what they point at would be gone.
RISKY_XMP = (b"HasExtendedXMP", b"GContainer", b"Container:Directory", b"GDepth", b"GImage", b"GAudio",
             b"hdrgm:", b"mwg-rs:", b"MPRI:", b"MP:RegionInfo", b"MotionPhoto", b"MicroVideo", b"GPano:")
IRB_HEADER = b"Photoshop 3.0\x00"


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


# ------------------------------------------------------------------ what is inside a JPEG

def _jpeg_walk(data: bytes) -> tuple[int, int, list[tuple[int, bytes]]]:
    """Follow a JPEG's parts. Returns (where the picture ends, where the first part that is not an APPn
    one starts, the (marker, content) of the parts before the picture data). ValueError if it can't be followed."""
    n = len(data)
    if data[:2] != b"\xff\xd8":
        raise ValueError("not a JPEG")
    i, parts, after_apps, in_header = 2, [], None, True
    while True:
        if i + 1 >= n or data[i] != 0xFF:
            raise ValueError("lost in the file")
        while i < n and data[i] == 0xFF:          # fill bytes
            i += 1
        if i >= n:
            raise ValueError("ends early")
        start = i - 1
        marker = data[i]
        i += 1
        if after_apps is None and not 0xE0 <= marker <= 0xEF:
            after_apps = start
        if marker == 0xD9:                         # the end of the picture
            return i, after_apps, parts
        if 0xD0 <= marker <= 0xD7 or marker == 0x01:
            continue
        if i + 2 > n:
            raise ValueError("ends early")
        length = int.from_bytes(data[i:i + 2], "big")
        if length < 2 or i + length > n:
            raise ValueError("a part is longer than the file")
        if in_header:
            parts.append((marker, data[i + 2:i + length]))
        i += length
        if marker == 0xDA:                         # picture data follows, up to the next real marker
            in_header = False
            while True:
                j = data.find(b"\xff", i)
                if j < 0 or j + 1 >= n:
                    raise ValueError("the picture data never ends")
                nxt = data[j + 1]
                if nxt == 0x00 or 0xD0 <= nxt <= 0xD7:
                    i = j + 2
                elif nxt == 0xFF:
                    i = j + 1
                else:
                    i = j
                    break


def _iptc_of(parts: list[tuple[int, bytes]]) -> bytes | None:
    """The IPTC block (captions, keywords) from a JPEG's Photoshop part, or None. ValueError if unreadable."""
    blob = b"".join(content[len(IRB_HEADER):] for marker, content in parts
                    if marker == 0xED and content.startswith(IRB_HEADER))
    pos, found = 0, None
    while pos + 12 <= len(blob):
        if blob[pos:pos + 4] != b"8BIM":
            raise ValueError("unreadable Photoshop part")
        resource = int.from_bytes(blob[pos + 4:pos + 6], "big")
        name_len = 1 + blob[pos + 6]
        pos += 6 + name_len + (name_len % 2)
        size = int.from_bytes(blob[pos:pos + 4], "big")
        pos += 4
        if pos + size > len(blob):
            raise ValueError("unreadable Photoshop part")
        if resource == 0x0404 and found is None:
            found = blob[pos:pos + size]
        pos += size + (size % 2)
    return found or None


def _iptc_part(iptc: bytes) -> bytes:
    """A whole APP13 part holding just this IPTC block."""
    body = IRB_HEADER + b"8BIM" + b"\x04\x04" + b"\x00\x00" + len(iptc).to_bytes(4, "big") + iptc + (b"\x00" if len(iptc) % 2 else b"")
    if len(body) + 2 > 0xFFFF:
        raise ValueError("the captions and keywords are too long for one part")
    return b"\xff\xed" + (len(body) + 2).to_bytes(2, "big") + body


# ------------------------------------------------------------------ checks

def _format_of(full: Path) -> str:
    """"JPEG" or "PNG" when the file can be rewritten without losing what is in it, else Refused."""
    if full.suffix.lower() not in SUFFIXES:
        raise Refused(415, "Only JPEG and PNG photos can be changed in the file. "
                           "RAW, HEIC and other formats keep the edit as a recipe; Download edited gives a copy.")
    try:
        with Image.open(full) as im:
            fmt, mode, frames = im.format, im.mode, getattr(im, "n_frames", 1)
            see_through = "transparency" in im.info
            xmp = im.info.get("xmp") or b""
    except Exception:
        raise Refused(415, "Snapshots could not read this file, so it was not changed.") from None
    lose = Refused(415, "This file has something a rewrite would lose (transparency, several pictures, "
                        "or unusual colours), so it was not changed.")
    if fmt == "PNG":
        if mode not in ("RGB", "L") or frames != 1 or see_through:
            raise lose
        return fmt
    if fmt != "JPEG" or mode not in ("RGB", "L"):
        raise lose
    if isinstance(xmp, str):
        xmp = xmp.encode("utf-8", "replace")
    if any(word in xmp for word in RISKY_XMP):
        raise Refused(415, "This file carries notes about places in the picture or extra data beside it "
                           "(face regions, a depth map, a second picture). A rewrite would lose them or "
                           "make them wrong, so it was not changed.")
    try:
        data = full.read_bytes()
        end, _, parts = _jpeg_walk(data)
        _iptc_of(parts)
    except (OSError, ValueError):
        raise Refused(415, "Snapshots could not follow how this file is put together, so it was not changed.") from None
    if data[end:].strip(b"\x00\r\n\t "):
        raise Refused(415, "This file has more data after the picture (a depth map, a second picture or "
                           "a video). A rewrite would lose it, so it was not changed.")
    return fmt


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


def _flush(path: Path) -> None:
    """Have the file's content written through to the disk, not left in memory for later."""
    with open(path, "rb+") as fh:
        os.fsync(fh.fileno())


def _flush_folder(folder: Path) -> None:
    """The same for a folder's list of names (where the system allows it: not on Windows)."""
    if os.name == "nt":
        return
    try:
        fd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)
    except OSError:
        pass


# ------------------------------------------------------------------ the old file is kept

def _keep_old_file(cfg: Config, conn, asset_id: int, full: Path, sha1: str, user_id: int | None) -> tuple[int, bool]:
    """Have a checked copy of `full` in the original-files folder. Returns (its file_backups id, whether a
    copy was made now: False when the very same content was already kept for this photo)."""
    folder = cfg.original_files_dir / str(asset_id)
    for kept in conn.execute("SELECT id, name FROM file_backups WHERE asset_id = ? AND sha1 = ? ORDER BY id DESC",
                             (asset_id, sha1)).fetchall():
        try:
            if scanner.sha1_of(folder / kept["name"]) == sha1 and scanner.sha1_of(full) == sha1:
                return kept["id"], False
        except OSError:
            continue
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
        _flush(dest)
        _flush_folder(folder)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        raise Refused(500, f"The old file could not be kept safely ({exc}). Nothing was changed.") from None
    try:
        cur = conn.execute("INSERT INTO file_backups (asset_id, name, sha1, size, created_at, created_by)"
                           " VALUES (?, ?, ?, ?, ?, ?)",
                           (asset_id, name, sha1, dest.stat().st_size, scanner.now(), user_id))
        conn.commit()       # recorded before the file is replaced: whatever happens next, the copy is known
    except Exception as exc:
        dest.unlink(missing_ok=True)
        raise Refused(500, f"The old file could not be kept safely ({exc}). Nothing was changed.") from None
    return cur.lastrowid, True


def _drop_keep(cfg: Config, conn, backup_id: int, asset_id: int) -> None:
    """The change did not happen: the copy made for it is not needed (only ever one made a moment ago)."""
    row = conn.execute("SELECT name FROM file_backups WHERE id = ? AND asset_id = ?", (backup_id, asset_id)).fetchone()
    conn.execute("DELETE FROM file_backups WHERE id = ?", (backup_id,))
    conn.commit()
    if row is not None:
        (cfg.original_files_dir / str(asset_id) / row["name"]).unlink(missing_ok=True)


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


def detach(cfg: Config, conn, asset_id: int) -> int:
    """The item's record is going away, and not because its photo was deleted from the trash (its file
    vanished from the library, or its folder was taken out of Snapshots). The old files kept for it are
    not deleted, but they must not stay under this item's number, which a later photo can get: they move
    to original-files/forgotten/. Returns how many files were moved."""
    folder = cfg.original_files_dir / str(asset_id)
    had_rows = conn.execute("DELETE FROM file_backups WHERE asset_id = ?", (asset_id,)).rowcount
    if not folder.is_dir():
        return 0
    try:
        files = [p for p in folder.iterdir() if p.is_file()]
        if not files:
            folder.rmdir()
            return 0
        dest = cfg.original_files_dir / FORGOTTEN / f"{asset_id}-{datetime.now():%Y%m%d-%H%M%S}"
        n = 1
        while dest.exists():
            n += 1
            dest = dest.with_name(f"{asset_id}-{datetime.now():%Y%m%d-%H%M%S}-{n}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        os.replace(folder, dest)
        return len(files)
    except OSError:
        # Left where they are: with their records gone they are never offered for any photo.
        log.warning("the kept files of item %s could not be moved to %s (%s records dropped)", asset_id, FORGOTTEN, had_rows)
        return 0


def detach_orphans(cfg: Config, conn) -> int:
    """Kept files whose item is gone (from before detach() existed, or left by a move that failed)."""
    ids = {r[0] for r in conn.execute("SELECT DISTINCT asset_id FROM file_backups"
                                      " WHERE asset_id NOT IN (SELECT id FROM assets)")}
    try:
        for entry in cfg.original_files_dir.iterdir():
            if entry.is_dir() and entry.name.isdigit() and not conn.execute(
                    "SELECT 1 FROM file_backups WHERE asset_id = ?", (int(entry.name),)).fetchone():
                ids.add(int(entry.name))
    except OSError:
        pass
    moved = sum(detach(cfg, conn, i) for i in sorted(ids))
    conn.commit()
    return moved


def kept_bytes(conn) -> int:
    return conn.execute("SELECT COALESCE(SUM(size), 0) FROM file_backups").fetchone()[0]


def known_old_content(cfg: Config, conn, sha1s) -> list:
    """Items one of whose kept old files has this content: the photo is in Snapshots, as the item it became
    when an edit was saved into its file. Only where the kept file is really there, and never an item in
    a locked folder or one set aside. Rows of (sha1, id, path, size, mtime_ns, trashed_at)."""
    wanted = sorted(set(sha1s))
    if not wanted:
        return []
    marks = ",".join("?" * len(wanted))
    rows = conn.execute(
        f"SELECT b.sha1 AS sha1, b.name AS kept_name, b.size AS kept_size, a.id AS id, a.path AS path,"
        f" a.size AS size, a.mtime_ns AS mtime_ns, a.trashed_at AS trashed_at"
        f" FROM file_backups b JOIN assets a ON a.id = b.asset_id"
        f" WHERE a.locked_by IS NULL AND a.missing_since IS NULL AND b.sha1 IN ({marks})"
        f" ORDER BY a.trashed_at IS NOT NULL, a.id", wanted).fetchall()
    out = []
    for r in rows:
        try:
            if (cfg.original_files_dir / str(r["id"]) / r["kept_name"]).stat().st_size == r["kept_size"]:
                out.append(r)
        except OSError:
            continue
    return out


# ------------------------------------------------------------------ swapping a file in

def _swap_in(full: Path, write) -> None:
    """write(tmp) makes the new file; it is written through to the disk and swapped in with one rename.
    The scanner leaves the file alone meanwhile. If this raises, the file was not replaced."""
    tmp = full.with_name(f".{full.name}.snapshots-edit")
    scanner.hold(full)
    try:
        write(tmp)
        _flush(tmp)
        for attempt in range(10):       # antivirus or the search indexer may hold the file for a moment
            try:
                os.replace(tmp, full)
                break
            except PermissionError:
                if attempt == 9:
                    raise
                time.sleep(0.3)
    except BaseException:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        scanner.release(full)
        raise
    # The file is replaced. Nothing below may make it look as if it was not.
    try:
        _flush_folder(full.parent)
    except Exception:
        log.exception("could not flush the folder of %s", full)
    finally:
        scanner.release(full)


def _carry_over(full: Path, fmt: str, size: tuple[int, int]) -> dict:
    """What the new file takes from the old one besides the picture (see the top of this file)."""
    carry: dict = {}
    with Image.open(full) as im:
        exif = im.getexif()
        carry["icc"] = im.info.get("icc_profile")
        carry["dpi"] = im.info.get("dpi")
        if fmt == "JPEG":
            xmp = im.info.get("xmp")
            if isinstance(xmp, str):
                xmp = xmp.encode("utf-8", "replace")
            carry["xmp"] = _xmp_for(xmp, size) if xmp else None
            carry["comment"] = im.info.get("comment")
            sampling = JpegImagePlugin.get_sampling(im)
            carry["subsampling"] = sampling if sampling in (0, 1, 2) else None
        else:
            carry["text"] = dict(getattr(im, "text", {}) or {})
    if exif:
        if 274 in exif:
            exif[274] = 1                       # the picture is drawn the right way up now
        ifd = exif.get_ifd(0x8769)
        for tag in (0xA002, 0xA003):            # the size the file used to have
            ifd.pop(tag, None)
    carry["exif"] = exif
    if fmt == "JPEG":
        carry["iptc"] = _iptc_of(_jpeg_walk(full.read_bytes())[2])
    return carry


def _xmp_for(xmp: bytes, size: tuple[int, int]) -> bytes:
    """The old file's XMP notes, saying the picture is the right way up and has its new size."""
    w, h = (str(v).encode() for v in size)
    for name, value in ((b"tiff:Orientation", b"1"), (b"exif:PixelXDimension", w), (b"exif:PixelYDimension", h),
                        (b"tiff:ImageWidth", w), (b"tiff:ImageLength", h)):
        xmp = re.sub(name + rb'="[^"]*"', name + b'="' + value + b'"', xmp)
        xmp = re.sub(b"<" + name + rb">[^<]*</" + name + b">", b"<" + name + b">" + value + b"</" + name + b">", xmp)
    return xmp


def _write_picture(img: Image.Image, fmt: str, carry: dict, dest: Path) -> None:
    kw = {}
    if carry.get("exif"):
        kw["exif"] = carry["exif"]
    if carry.get("icc"):
        kw["icc_profile"] = carry["icc"]
    try:
        dpi = tuple(float(v) for v in carry.get("dpi") or ())
    except (TypeError, ValueError):
        dpi = ()
    if len(dpi) == 2 and all(v > 0 for v in dpi):
        kw["dpi"] = tuple(int(round(v)) for v in dpi) if fmt == "JPEG" else dpi
    if fmt == "JPEG":
        if carry.get("xmp"):
            kw["xmp"] = carry["xmp"]
        if carry.get("comment"):
            kw["comment"] = carry["comment"]
        if carry.get("subsampling") is not None and img.mode != "L":
            kw["subsampling"] = carry["subsampling"]
        img.save(dest, "JPEG", quality=95, optimize=True, **kw)
        if carry.get("iptc"):
            _add_iptc(dest, carry["iptc"])
    else:
        if carry.get("text"):
            notes = PngImagePlugin.PngInfo()
            for key, value in carry["text"].items():
                notes.add_text(str(key), str(value))
            kw["pnginfo"] = notes
        img.save(dest, "PNG", optimize=True, **kw)
    with Image.open(dest) as back:               # read it back before it is trusted
        if back.format != fmt or back.size != img.size:
            raise OSError("the new file did not come out as planned")
        back.load()


def _add_iptc(dest: Path, iptc: bytes) -> None:
    """Put the old file's captions and keywords into the new JPEG, and check the picture is untouched by it."""
    data = dest.read_bytes()
    try:
        _, after_apps, _ = _jpeg_walk(data)
        part = _iptc_part(iptc)
    except ValueError as exc:
        raise OSError(str(exc)) from None
    joined = data[:after_apps] + part + data[after_apps:]
    with Image.open(io.BytesIO(data)) as a, Image.open(io.BytesIO(joined)) as b:
        if a.size != b.size or a.tobytes() != b.tobytes():
            raise OSError("the captions could not be put into the new file")
    dest.write_bytes(joined)


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


def _in_place(conn, asset_id: int, backup_id: int | None) -> None:
    """Note which kept copy (if any) is what the photo's file holds now."""
    conn.execute("UPDATE file_backups SET restored_at = NULL WHERE asset_id = ?", (asset_id,))
    if backup_id is not None:
        conn.execute("UPDATE file_backups SET restored_at = ? WHERE id = ?", (scanner.now(), backup_id))
    conn.commit()


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
        carry = _carry_over(full, fmt, img.size)
    except Exception:
        raise Refused(415, "Snapshots could not read this photo to change it. Nothing was changed.") from None
    backup_id, made = _keep_old_file(cfg, conn, row["id"], full, row["sha1"], user_id)
    st = full.stat()

    def write(tmp: Path) -> None:
        _write_picture(img, fmt, carry, tmp)
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))     # the file's date stays: it may be the photo's only date

    try:
        _swap_in(full, write)
    except Exception as exc:
        if made:
            _drop_keep(cfg, conn, backup_id, row["id"])         # the file was not replaced: no copy needed
        raise Refused(500, f"The file could not be written ({exc}). Nothing was changed.") from None
    _in_place(conn, row["id"], None)
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
    current_kept, made = None, False
    cur_sha = scanner.sha1_of(full)
    if cur_sha != old["sha1"]:
        current_kept, made = _keep_old_file(cfg, conn, row["id"], full, cur_sha, user_id)   # undo stays possible
    st = full.stat()

    def write(tmp: Path) -> None:
        shutil.copyfile(src, tmp)
        if scanner.sha1_of(tmp) != old["sha1"]:
            raise OSError("the copy changed on the way")
        os.utime(tmp, ns=(st.st_atime_ns, st.st_mtime_ns))

    try:
        _swap_in(full, write)
    except Exception as exc:
        if made:
            _drop_keep(cfg, conn, current_kept, row["id"])
        raise Refused(500, f"The old file could not be put back ({exc}). Nothing was changed.") from None
    _in_place(conn, row["id"], old["id"])
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
