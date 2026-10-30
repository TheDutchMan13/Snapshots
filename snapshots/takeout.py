"""Importing a Google Photos export (Google Takeout).

Takeout gives your photos plus, beside each one, a .json file with what Google Photos knew: the date,
the location, the description, whether it was a favourite, archived or in the trash, and who is in it.
Albums are folders with a metadata.json holding the album's name ("Photos from 2019" folders are not
albums). Snapshots reads the export as it came: the .zip files themselves, or the folders unpacked from
them, or both.

Each photo is found in Snapshots by its content, so it doesn't matter where the photos ended up. Photos
Snapshots doesn't have can be copied into "From Google Photos" in the library.

Dates: many exported files have lost their own date, so Google's is used when the two differ by more
than a day (a day's leeway, because the export doesn't say which time zone it means). Locations are
used for photos that have none of their own. Google only says who is in a photo, not where, so a name is
given to a face only when the photo has exactly one face and one name.
"""

import hashlib
import json
import logging
import os
import re
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from . import db, imports, linking, media

log = logging.getLogger("snapshots.takeout")

GOOGLE_PHOTOS = re.compile(r"^google (photos|fotos|foto|photo)$", re.I)
DUPLICATE = re.compile(r"^(.*?)(\(\d+\))(\.[^.]+)$")          # IMG(1).jpg
JSON_DUPLICATE = re.compile(r"^(.*?)(\(\d+\))$")               # IMG.jpg(1)  (after .json is removed)
EDITED = re.compile(r"-(edited|bearbeitet|modifié|editado|modificato|bewerkt|redigerad|muokattu)$", re.I)
TRUNCATED_AT = 40         # sidecar names this long may have been cut short by Google
MAX_JSON = 2 * 1024 * 1024
DAY = 26 * 3600           # the export's dates are in UTC; a day (and a bit) of leeway for the time zone


@dataclass
class Entry:
    folder: str           # the folder inside the export ("Trip to Rome"), the same in every zip
    name: str
    size: int
    opener: object        # () -> a binary file
    where: str            # for messages


def _folder_key(parts: list[str]) -> str:
    for i in range(len(parts) - 1, -1, -1):
        if GOOGLE_PHOTOS.match(parts[i]):
            return "/".join(parts[i + 1:])
    return "/".join(parts)


def collect(source: Path, check=None) -> tuple[list[Entry], list]:
    """Every file in the export: (entries, open zip files to close afterwards)."""
    entries, zips = [], []

    def add_zip(path: Path) -> None:
        try:
            zf = zipfile.ZipFile(path)
        except (zipfile.BadZipFile, OSError) as exc:
            log.warning("could not open %s: %s", path, exc)
            return
        zips.append(zf)
        for info in zf.infolist():
            if info.is_dir():
                continue
            parts = info.filename.replace("\\", "/").split("/")
            entries.append(Entry(_folder_key(parts[:-1]), parts[-1], info.file_size,
                                 lambda zf=zf, info=info: zf.open(info), f"{path.name}: {info.filename}"))

    if source.is_file():
        if source.suffix.lower() != ".zip":
            raise ValueError("Choose the folder with the Takeout files, or one of its .zip files.")
        add_zip(source)
        return entries, zips
    if not source.is_dir():
        raise ValueError("That folder doesn't exist, or the drive isn't connected.")
    for dirpath, dirnames, filenames in os.walk(source):
        if check:
            check()
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        rel = Path(dirpath).relative_to(source).parts
        for name in filenames:
            full = Path(dirpath) / name
            if name.lower().endswith(".zip"):
                add_zip(full)
                continue
            try:
                size = full.stat().st_size
            except OSError:
                continue
            entries.append(Entry(_folder_key(list(rel)), name, size, lambda full=full: open(full, "rb"), str(full)))
    return entries, zips


def folder_name(folder: str) -> str:
    """A safe folder name for copies, from the export's folder (never "..", a drive or a path)."""
    from .app import safe_filename
    last = folder.replace("\\", "/").split("/")[-1].strip()
    name = os.path.splitext(safe_filename(last.replace(":", "_") + ".x"))[0]
    return name if name and name not in (".", "..") and not name.startswith(".") else "Photos"


def read_json(entry: Entry) -> dict | None:
    if entry.size > MAX_JSON:
        return None
    try:
        with entry.opener() as fh:
            data = json.loads(fh.read().decode("utf-8", "replace"))
    except (OSError, ValueError, zipfile.BadZipFile):
        return None
    return data if isinstance(data, dict) else None


def is_sidecar(data: dict) -> bool:
    return "photoTakenTime" in data or ("creationTime" in data and "title" in data and "url" in data)


def match_sidecar(name: str, sidecars: dict[str, dict]) -> dict | None:
    """The .json Google wrote for a photo, despite the ways Takeout names them:
    IMG.jpg.json, IMG.jpg.supplemental-metadata.json, names cut short, IMG(1).jpg → IMG.jpg(1).json,
    and IMG-edited.jpg using the original's IMG.jpg.json. A name that really ends in "(1)" (like a browser
    download, "image (1).jpg") is tried as it is too."""
    m = DUPLICATE.match(name)
    if m:
        found = _match(m.group(1) + m.group(3), m.group(2), name, sidecars)
        if found is not None:
            return found
    return _match(name, "", name, sidecars)


def _match(base: str, dup: str, name: str, sidecars: dict[str, dict]) -> dict | None:
    stem, ext = os.path.splitext(base)
    plain = EDITED.sub("", stem) + ext
    best, best_len = None, -1
    for jname, data in sidecars.items():
        key = jname[:-5]
        jdup = ""
        mm = JSON_DUPLICATE.match(key)
        if mm:
            key, jdup = mm.group(1), mm.group(2)
        if jdup != dup:
            continue
        ok = False
        for cand in dict.fromkeys((base, plain)):
            full = cand + ".supplemental-metadata"
            if full.startswith(key) and (len(key) >= len(cand) or len(key) >= TRUNCATED_AT):
                ok = True
            elif key == os.path.splitext(cand)[0]:
                ok = True
        if ok and len(key) > best_len:
            best, best_len = data, len(key)
    if best is None and not dup:
        titled = [d for d in sidecars.values() if d.get("title") in (name, base, plain)]
        if len(titled) == 1:
            best = titled[0]
    return best


def meta_of(data: dict | None, album: str | None) -> dict:
    meta = {"albums": [album] if album else [], "tags": [], "people": []}
    if not data:
        return meta
    meta["favorite"] = bool(data.get("favorited"))
    meta["archived"] = bool(data.get("archived"))
    meta["trashed"] = bool(data.get("trashed"))
    meta["description"] = (data.get("description") or "").strip() or None
    try:
        ts = int((data.get("photoTakenTime") or {}).get("timestamp"))
        meta["google_time"] = datetime.fromtimestamp(ts) if ts > 0 else None
    except (TypeError, ValueError, OSError, OverflowError):
        meta["google_time"] = None
    for key in ("geoData", "geoDataExif"):
        geo = data.get(key) or {}
        try:
            lat, lon = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            continue
        if not (abs(lat) < 1e-6 and abs(lon) < 1e-6):
            meta["lat"], meta["lon"] = lat, lon
            break
    meta["people"] = [{"name": p["name"].strip(), "box": None} for p in data.get("people") or []
                      if isinstance(p, dict) and isinstance(p.get("name"), str) and p["name"].strip()]
    return meta


def _sha1(entry: Entry, check) -> str:
    h = hashlib.sha1(usedforsecurity=False)
    with entry.opener() as fh:
        while chunk := fh.read(1 << 20):
            h.update(chunk)
    return h.hexdigest()


def run(manager, cfg, source: str, user_id: int, copy_missing: bool, dry_run: bool) -> dict:
    check = manager.check
    path = Path(source.strip().strip('"'))
    if not path.is_absolute():
        raise ValueError("Give the whole path, like D:\\Takeout.")
    manager.update(phase="Looking through the export")
    entries, zips = collect(path, check)
    try:
        return _run(manager, cfg, entries, user_id, copy_missing, dry_run)
    finally:
        for zf in zips:
            zf.close()


def _run(manager, cfg, entries: list[Entry], user_id: int, copy_missing: bool, dry_run: bool) -> dict:
    check = manager.check
    sidecars: dict[str, dict[str, dict]] = {}
    albums: dict[str, str] = {}
    media_entries = []
    for e in entries:
        if e.name.lower().endswith(".json"):
            data = read_json(e)
            if data is None:
                continue
            if is_sidecar(data):
                sidecars.setdefault(e.folder, {})[e.name] = data
            elif isinstance(data.get("title"), str) and data["title"].strip() and e.folder:
                albums[e.folder] = data["title"].strip()
        elif media.kind_of(e.name):
            media_entries.append(e)
    # Year folders first, so photos copied into the library land in them rather than in album folders.
    media_entries.sort(key=lambda e: (e.folder in albums, e.folder, e.name))
    summary = {"files": len(media_entries), "albums_found": len(set(albums.values())), "matched": 0, "missing": 0,
               "missing_bytes": 0, "copied": 0, "copy_failed": 0, "without_details": 0}
    if not media_entries:
        raise ValueError("No photos or videos were found there. Choose the folder with the Takeout files"
                         " (or the .zip files themselves).")

    manager.update(phase="Matching photos" if dry_run else "Bringing over albums, favourites and dates",
                   done=0, total=len(media_entries))
    root = cfg.library_path
    into = root / "From Google Photos" if root else None
    known: dict[str, list[int]] = {}
    with db.session(cfg.db_path) as conn:
        applier = imports.Applier(conn, user_id, "takeout")
        tick = imports.progress_every()
        for n, e in enumerate(media_entries):
            check()
            if tick():
                manager.update(done=n)
            try:
                sha1 = _sha1(e, check)
            except (OSError, zipfile.BadZipFile) as exc:
                log.warning("could not read %s: %s", e.where, exc)
                summary["copy_failed"] += 1
                continue
            data = match_sidecar(e.name, sidecars.get(e.folder, {}))
            if data is None:
                summary["without_details"] += 1
            meta = meta_of(data, albums.get(e.folder))
            ids = known.get(sha1)
            if ids is None:
                ids = imports.find_assets(conn, sha1)
                if ids is None:     # only in someone's locked folder: left alone
                    summary["matched"] += 1
                    ids = []
                elif ids:
                    summary["matched"] += 1
                elif not meta.get("trashed"):
                    summary["missing"] += 1
                    summary["missing_bytes"] += e.size
                    if not dry_run and copy_missing and into is not None:
                        folder = into / folder_name(e.folder)

                        def write(fh, e=e):
                            with e.opener() as src:
                                shutil.copyfileobj(src, fh, 1 << 20)
                        try:
                            new_id = imports.add_file(cfg, conn, folder, e.name, write, sha1)
                        except OSError as exc:
                            log.warning("could not copy %s: %s", e.where, exc)
                            new_id = None
                        if new_id is None:
                            summary["copy_failed"] += 1
                        else:
                            summary["copied"] += 1
                            ids = [new_id]
                known[sha1] = ids
            if dry_run or not ids:
                continue
            google_time = meta.pop("google_time", None)
            for i in ids:
                m = dict(meta)
                if google_time is not None:
                    row = conn.execute("SELECT taken_at FROM assets WHERE id = ?", (i,)).fetchone()
                    try:
                        mine = datetime.strptime(row["taken_at"], imports.DATE_FMT)
                        if abs((google_time - mine).total_seconds()) > DAY:
                            m["taken_at"] = google_time.strftime(imports.DATE_FMT)
                    except (TypeError, ValueError):
                        pass
                if m.get("lat") is not None:
                    has = conn.execute("SELECT lat FROM assets WHERE id = ?", (i,)).fetchone()["lat"]
                    if has is not None:   # the file's own position wins
                        m.pop("lat"), m.pop("lon")
                applier.apply(i, m)
            if n % 100 == 0:
                conn.commit()
        if not dry_run:
            linking.link_pairs(conn, cfg)
            applier.finish()
            summary.update(applier.counts)
    manager.update(done=len(media_entries))
    return summary
