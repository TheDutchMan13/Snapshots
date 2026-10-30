"""Browsing the library, serving files, uploading, and status."""

import hashlib
import json
import os
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import __version__, analysis, db, libraries, linking, media
from .app import current_user, require_admin, safe_filename
from .scanner import _make_thumb, index_file
from .videos import Converter

KIND = Query("", pattern="^(photo|video)?$")


class ScanBody(BaseModel):
    remove_missing: bool = False


class EditsBody(BaseModel):
    edits: dict | None = None      # None (or nothing changed) = back to the original picture


class DetailsBody(BaseModel):
    """Change the details of one or more items. Only the fields given are changed."""
    ids: list[int] = Field(min_length=1, max_length=20000)
    taken_at: str | None = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(:\d{2})?$")
    shift_minutes: int | None = Field(None, ge=-60 * 24 * 366 * 50, le=60 * 24 * 366 * 50)
    reset_date: bool = False       # back to the date in the file
    position: list[float] | None = Field(None, min_length=2, max_length=2)  # [lat, lon]
    clear_position: bool = False   # "no location"
    reset_position: bool = False   # back to the position in the file
    description: str | None = Field(None, max_length=5000)


TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"
BBOX_PATTERN = r"^-?[\d.]+,-?[\d.]+,-?[\d.]+,-?[\d.]+$"


def _bbox(text: str | None):
    if not text:
        return None
    try:
        south, west, north, east = (float(x) for x in text.split(","))
    except ValueError:
        raise HTTPException(400, "Bad map area") from None
    if not (-90 <= south <= north <= 90 and -180 <= west <= 180 and -180 <= east <= 180):
        raise HTTPException(400, "Bad map area")
    return south, west, north, east


def make_filter_params(cfg):
    """The query parameters every grid understands, turned into a db.Filter."""

    def filter_params(
        q: str = Query("", max_length=200),
        kind: str = KIND,
        favorite: bool = False,
        trashed: bool = False,
        album: int | None = None,
        person: int | None = None,
        country: str | None = None,
        state: str | None = None,
        city: str | None = None,
        start: str | None = Query(None, pattern=TIME_PATTERN),  # each parameter needs its own Query()
        end: str | None = Query(None, pattern=TIME_PATTERN),
        quality: bool = False,
        archived: str = Query("", pattern="^(hide|only)?$"),
        tag: int | None = None,
        folder: str | None = Query(None, max_length=1000),
        stacks: bool = False,
        bbox: str | None = Query(None, pattern=BBOX_PATTERN),
        locked: bool = False,
    ) -> db.Filter:
        return db.Filter(q=q, kind=kind, favorite=favorite, trashed=trashed, album=album, person=person,
                         country=country, state=state, city=city, start=start, end=end,
                         low_quality=analysis.low_quality_sql(cfg) if quality else None,
                         archived=archived, tag=tag, folder=folder, stacks=stacks, bbox=_bbox(bbox),
                         locked=locked)

    return filter_params


def asset_dict(row, score: float | None = None) -> dict:
    out = {
        "id": row["id"],
        "filename": row["filename"],
        "path": row["path"],
        "kind": row["kind"],
        "size": row["size"],
        "taken_at": row["taken_at"],
        "width": row["width"],
        "height": row["height"],
        "camera": row["camera"],
        "has_thumb": bool(row["has_thumb"]),
        "favorite": bool(row["favorite"]),
        "trashed": row["trashed_at"] is not None,
        "place": {"city": row["city"], "state": row["state"], "country": row["country"]} if row["country"] else None,
        "has_position": row["lat"] is not None,
        "archived": row["archived_at"] is not None,
        "live": bool(row["live_video"] or row["motion_offset"]),
        "stack": row["stack_id"] if row["stack_id"] and row["stack_count"] > 1 else None,
        "stack_count": row["stack_count"] if row["stack_id"] else 0,
        "raw": media.is_raw(row["filename"]),
        "edited": bool(row["edits"]),
        "projection": row["projection"],
        "locked": row["locked_by"] is not None,
        "v": db.version(row),  # changes when the file (or its edit) changes, so browsers refetch
    }
    if score is not None:
        out["score"] = round(score, 4)
    return out


def library_root(cfg) -> Path:
    root = cfg.library_path
    if root is None or not root.is_dir():
        raise HTTPException(409, "The library folder is not available.")
    return root


def original_path(cfg, stored: str) -> Path:
    """Where an item's file is. Raises if its folder is gone from the settings or not connected."""
    root = libraries.root_of(cfg, stored)
    if root is None:
        raise HTTPException(404, "This item's folder is no longer part of the library.")
    if not root.available():
        raise HTTPException(409, f"The folder {root.path} is not available. Is the drive connected?")
    full = libraries.full_path(cfg, stored)
    if full is None:
        raise HTTPException(404, "Not found")
    return full


def can_see(row, user) -> bool:
    """Items in a locked folder are only for their owner, while the folder is open."""
    owner = row["locked_by"]
    return owner is None or (owner == user["id"] and bool(user.get("unlocked")))


TIME_FMT = "%Y-%m-%dT%H:%M:%S"


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()
    filter_params = make_filter_params(cfg)

    converter = Converter(cfg)
    ctx.videos = converter

    def get_row(asset_id: int, user):
        with db.session(cfg.db_path) as conn:
            row = db.get_asset(conn, asset_id, user["id"])
        if row is None or not can_see(row, user):
            raise HTTPException(404, "Not found")
        return row

    # ------------------------------------------------------------ browsing

    def content_matches(conn, f: db.Filter, user_id: int) -> list:
        """Rows that look like f.q (search by content), best first, within the rest of the filter."""
        ranked = worker.clip.search(conn, f.q)
        if not ranked:
            return []
        score = dict(ranked)
        g = db.Filter(**{**f.__dict__, "q": "", "ids": list(score)})
        rows, _ = db.list_assets(conn, g, user_id, "", len(score))
        return sorted(((row, score[row["id"]]) for row in rows), key=lambda x: -x[1])

    def grid_user(request: Request, f: db.Filter) -> dict:
        user = current_user(request)
        if f.locked and not user.get("unlocked"):
            raise HTTPException(403, "The locked folder is closed.")
        return user

    @r.get("/api/assets")
    def list_assets(request: Request, f: db.Filter = Depends(filter_params), cursor: str = "",
                    limit: int = Query(120, ge=1, le=500), look: bool = False):
        """The photos in a grid, newest first. look=true: photos whose content matches q, best first."""
        user = grid_user(request, f)
        if look:
            with db.session(cfg.db_path) as conn:
                found = content_matches(conn, f, user["id"]) if f.q.strip() else []
            return {"items": [asset_dict(row, sc) for row, sc in found], "next": None}
        try:
            with db.session(cfg.db_path) as conn:
                rows, next_cursor = db.list_assets(conn, f, user["id"], cursor, limit)
        except ValueError:
            raise HTTPException(400, "Bad cursor")
        return {"items": [asset_dict(x) for x in rows], "next": next_cursor}

    @r.get("/api/timeline")
    def timeline(request: Request, f: db.Filter = Depends(filter_params)):
        user = grid_user(request, f)
        with db.session(cfg.db_path) as conn:
            return {"months": db.timeline(conn, f, user["id"])}

    @r.get("/api/search")
    def search_summary(request: Request, f: db.Filter = Depends(filter_params)):
        """How many photos match q by words and names, and how many by what they show."""
        user = grid_user(request, f)
        with db.session(cfg.db_path) as conn:
            where, params = db.where_clause(f, user["id"])
            words = conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0]
            content = len(content_matches(conn, f, user["id"])) if f.q.strip() else 0
            pending = worker.clip.pending(conn) if cfg.smart_search else 0
        return {"q": f.q, "words": words, "content": content, "content_enabled": cfg.smart_search,
                "content_ready": worker.clip.ready(), "content_pending": pending,
                "content_message": worker.clip.state.get("message")}

    @r.get("/api/assets/{asset_id}")
    def get_asset(asset_id: int, request: Request):
        user = current_user(request)
        row = get_row(asset_id, user)
        out = asset_dict(row)
        out["text"] = row["ocr_text"]
        out["description"] = row["description"]
        out["edits"] = media.parse_edits(row["edits"]) if row["edits"] else None
        out["date_edited"] = row["taken_at_file"] is not None
        out["position_edited"] = bool(row["position_edited"])
        out["position"] = [row["lat"], row["lon"]] if row["lat"] is not None else None
        out["quality"] = None if row["keep_quality"] else analysis.quality_label(
            cfg, row["sharpness"], row["brightness"], row["highlights"])
        with db.session(cfg.db_path) as conn:
            out["people"] = [
                {"face_id": p["id"], "person_id": p["person_id"], "name": p["name"]}
                for p in conn.execute(
                    "SELECT f.id, f.person_id, p.name FROM faces f JOIN people p ON p.id = f.person_id"
                    " WHERE f.asset_id = ? AND p.hidden = 0 ORDER BY f.x",
                    (asset_id,),
                )
            ]
            out["albums"] = [
                {"id": a["id"], "name": a["name"]}
                for a in conn.execute(
                    "SELECT al.id, al.name FROM albums al JOIN album_assets aa ON aa.album_id = al.id"
                    " WHERE aa.asset_id = ? ORDER BY al.name COLLATE NOCASE",
                    (asset_id,),
                )
            ]
            out["tags"] = [
                {"id": t["id"], "name": t["name"]}
                for t in conn.execute(
                    "SELECT t.id, t.name FROM tags t JOIN asset_tags at ON at.tag_id = t.id"
                    " WHERE at.asset_id = ? ORDER BY t.name COLLATE NOCASE", (asset_id,))
            ]
            out["stack_items"] = [
                {"id": m["id"], "filename": m["filename"], "v": db.version(m), "cover": bool(m["cover"]),
                 "kind": m["kind"], "taken_at": m["taken_at"], "width": m["width"], "height": m["height"]}
                for m in linking.stack_members(conn, row["stack_id"])
            ] if row["stack_id"] else []
            out["comments"] = conn.execute("SELECT COUNT(*) FROM comments WHERE asset_id = ?", (asset_id,)).fetchone()[0]
        out["folder"] = row["path"].rpartition("/")[0]
        return out

    @r.get("/api/assets/{asset_id}/thumb")
    def get_thumb(asset_id: int, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT locked_by FROM assets WHERE id = ?", (asset_id,)).fetchone()
        path = cfg.thumb_path(asset_id)
        if row is None or not can_see(row, user) or not path.is_file():
            raise HTTPException(404, "No thumbnail")
        # Thumbnails never change at the same address, so browsers may keep them, except locked ones.
        cache = "no-store" if row["locked_by"] is not None else "private, max-age=31536000, immutable"
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": cache})

    def file_of(row) -> Path:
        full = original_path(cfg, row["path"])
        if not full.is_file():
            raise HTTPException(404, "The file is no longer on disk.")
        return full

    @r.get("/api/assets/{asset_id}/view")
    def get_view(asset_id: int, request: Request, plain: bool = False):
        """What the viewer shows: the original if browsers can display it, else a JPEG. An edited photo
        is shown edited, unless plain=true (the editor starts from the unedited picture)."""
        row = get_row(asset_id, current_user(request))
        full = file_of(row)
        headers = {"Cache-Control": "no-store" if row["locked_by"] is not None else "private, max-age=3600"}
        edits = media.parse_edits(row["edits"]) if row["edits"] and row["kind"] == "photo" and not plain else None
        if edits:
            out = cfg.edited_path(asset_id)
            if not out.is_file() and not media.make_preview(full, out, edits):
                raise HTTPException(415, "This file cannot be displayed.")
            return FileResponse(out, media_type="image/jpeg", headers=headers)
        if row["kind"] == "video" or full.suffix.lower() in media.BROWSER_NATIVE:
            return FileResponse(full, headers=headers)
        preview = cfg.preview_path(asset_id)
        if not preview.is_file() and not media.make_preview(full, preview):
            raise HTTPException(415, "This file cannot be displayed.")
        return FileResponse(preview, media_type="image/jpeg", headers=headers)

    @r.get("/api/assets/{asset_id}/pano")
    def get_pano(asset_id: int, request: Request):
        """A big JPEG of a 360° photo or panorama, for looking around in it."""
        row = get_row(asset_id, current_user(request))
        if row["kind"] != "photo":
            raise HTTPException(404, "Not a photo")
        full = file_of(row)
        out = cfg.edited_dir / f"{asset_id}.pano.jpg"
        if not out.is_file() and not media.make_pano(full, out, media.parse_edits(row["edits"])):
            raise HTTPException(415, "This file cannot be displayed.")
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @r.get("/api/assets/{asset_id}/edited")
    def get_edited(asset_id: int, request: Request):
        """The edited photo at full size, as a JPEG to download. The original file is unchanged."""
        row = get_row(asset_id, current_user(request))
        edits = media.parse_edits(row["edits"]) if row["edits"] else None
        if not edits or row["kind"] != "photo":
            raise HTTPException(404, "This photo has no edits.")
        full = file_of(row)
        out = cfg.edited_dir / f"{asset_id}.full.jpg"
        tmp = out.with_name(out.name + ".tmp")
        try:
            media.render_edited(full, edits, 0).save(tmp, "JPEG", quality=93)
            os.replace(tmp, out)
        except Exception:
            tmp.unlink(missing_ok=True)
            raise HTTPException(415, "This photo could not be edited.") from None
        stem = os.path.splitext(row["filename"])[0]
        return FileResponse(out, media_type="image/jpeg", filename=f"{stem} (edited).jpg",
                            content_disposition_type="attachment")

    # ------------------------------------------------------------ playing videos

    @r.get("/api/assets/{asset_id}/play")
    def play(asset_id: int, request: Request, convert: bool = True):
        """How to play a video: straight from the file, from a converted copy, or after converting it."""
        row = get_row(asset_id, current_user(request))
        if row["kind"] != "video":
            raise HTTPException(404, "Not a video")
        copy = cfg.video_path(asset_id)
        if copy.is_file():
            return {"url": f"/api/assets/{asset_id}/converted?v={db.version(row)}", "converted": True}
        full = file_of(row)
        with db.session(cfg.db_path) as conn:
            known = conn.execute("SELECT vcodec FROM assets WHERE id = ?", (asset_id,)).fetchone()["vcodec"]
        info = json.loads(known) if known else None
        if info is None:
            info = media.probe_video(full)
            with db.session(cfg.db_path) as conn:
                conn.execute("UPDATE assets SET vcodec = ? WHERE id = ?", (json.dumps(info), asset_id))
        native = {"url": f"/api/assets/{asset_id}/view?v={db.version(row)}", "converted": False}
        if info.get("playable", True) or not media.has_ffmpeg():
            return dict(native, codec=info.get("codec"), can_convert=media.has_ffmpeg())
        if not convert:
            return dict(native, codec=info.get("codec"), can_convert=True)
        job = converter.request(asset_id, full, info.get("codec"), info.get("duration"))
        return {"preparing": True, "progress": job["progress"], "error": job["error"], "codec": info.get("codec"),
                "fallback": native["url"]}

    @r.get("/api/assets/{asset_id}/converted")
    def converted(asset_id: int, request: Request):
        get_row(asset_id, current_user(request))
        copy = cfg.video_path(asset_id)
        if not copy.is_file():
            raise HTTPException(404, "Not converted yet")
        return FileResponse(copy, media_type="video/mp4", headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------ edits and details

    @r.post("/api/assets/{asset_id}/edits")
    def save_edits(asset_id: int, body: EditsBody, request: Request):
        """Rotate, flip, straighten or crop a photo. Only a recipe is stored: the original file never changes."""
        user = current_user(request)
        row = get_row(asset_id, user)
        if row["locked_by"] is not None:
            raise HTTPException(403, "Move it out of the locked folder first.")
        if row["kind"] != "photo":
            raise HTTPException(400, "Only photos can be edited.")
        try:
            edits = media.parse_edits(body.edits)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, f"Bad edit: {exc}") from None
        text = media.edits_json(edits)
        edit_v = hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:6] if text else None
        full = file_of(row)
        for p in (cfg.edited_path(asset_id), cfg.edited_dir / f"{asset_id}.pano.jpg",
                  cfg.edited_dir / f"{asset_id}.full.jpg"):
            p.unlink(missing_ok=True)
        with db.session(cfg.db_path) as conn:
            # The thumbnail is what Clean up and search by content look at: measure it again.
            conn.execute("UPDATE assets SET edits = ?, edit_v = ?, quality_done = 0, clip_done = 0 WHERE id = ?",
                         (text, edit_v, asset_id))
            conn.execute("DELETE FROM clip_vectors WHERE asset_id = ?", (asset_id,))
            _make_thumb(cfg, conn, asset_id, "photo", full, text)
            row = db.get_asset(conn, asset_id, user["id"])
        worker.poke()
        return asset_dict(row)

    @r.post("/api/assets/details")
    def change_details(body: DetailsBody, request: Request):
        """Change the date, location or description. Stored by Snapshots; the files are not changed."""
        current_user(request)
        if body.position is not None:
            lat, lon = body.position
            if not (-90 <= lat <= 90 and -180 <= lon <= 180):
                raise HTTPException(400, "That position is not on Earth.")
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
            rows = {r["id"]: r for r in conn.execute(
                f"SELECT id, taken_at, taken_at_file FROM assets WHERE id IN ({','.join('?' * len(ids)) or 'NULL'})", ids)}
            for i in ids:
                row = rows[i]
                original = row["taken_at_file"] or row["taken_at"]
                new = None
                if body.reset_date:
                    new = original
                elif body.taken_at:
                    new = body.taken_at if len(body.taken_at) == 19 else body.taken_at + ":00"
                elif body.shift_minutes:
                    try:
                        new = (datetime.strptime(row["taken_at"], TIME_FMT)
                               + timedelta(minutes=body.shift_minutes)).strftime(TIME_FMT)
                    except (ValueError, OverflowError):
                        raise HTTPException(400, "That date is out of range.") from None
                if new is not None:
                    try:
                        datetime.strptime(new, TIME_FMT)
                    except ValueError:
                        raise HTTPException(400, "That is not a valid date.") from None
                    kept = None if new == original and body.reset_date else original
                    conn.execute("UPDATE assets SET taken_at = ?, taken_at_file = ? WHERE id = ?", (new, kept, i))
            if body.reset_position:
                # Read the position from the file again on the next look.
                for i in ids:
                    full = None
                    path = conn.execute("SELECT path, kind FROM assets WHERE id = ?", (i,)).fetchone()
                    full = libraries.full_path(cfg, path["path"])
                    info = {"lat": None, "lon": None}
                    if full is not None and full.is_file():
                        info = media.read_photo_info(full) if path["kind"] == "photo" else media.read_video_info(full)
                    has = info["lat"] is not None
                    conn.execute(
                        "UPDATE assets SET lat = ?, lon = ?, position_edited = 0, city = NULL, state = NULL,"
                        " country = NULL, geo_done = ? WHERE id = ?", (info["lat"], info["lon"], 0 if has else 1, i))
            elif body.clear_position:
                conn.executemany(
                    "UPDATE assets SET lat = NULL, lon = NULL, position_edited = 1, city = NULL, state = NULL,"
                    " country = NULL, geo_done = 1 WHERE id = ?", [(i,) for i in ids])
            elif body.position is not None:
                lat, lon = round(body.position[0], 6), round(body.position[1], 6)
                conn.executemany(
                    "UPDATE assets SET lat = ?, lon = ?, position_edited = 1, city = NULL, state = NULL,"
                    " country = NULL, geo_done = 0 WHERE id = ?", [(lat, lon, i) for i in ids])
            if body.description is not None:
                text = body.description.strip() or None
                conn.executemany("UPDATE assets SET description = ? WHERE id = ?", [(text, i) for i in ids])
        worker.poke()  # new positions need place names
        return {"updated": len(ids)}

    @r.get("/api/assets/{asset_id}/motion")
    def get_motion(asset_id: int, request: Request):
        """The moving part of a Live Photo or motion photo, as a video the app can play."""
        row = get_row(asset_id, current_user(request))
        headers = {"Cache-Control": "private, max-age=3600"}
        cached = cfg.motion_path(asset_id)
        if cached.is_file():
            return FileResponse(cached, media_type="video/mp4", headers=headers)
        if row["live_video"]:
            with db.session(cfg.db_path) as conn:
                clip = conn.execute("SELECT path FROM assets WHERE id = ?", (row["live_video"],)).fetchone()
            full = original_path(cfg, clip["path"]) if clip else None
            if full is None or not full.is_file():
                raise HTTPException(404, "The clip is no longer on disk.")
            if media.make_playable(full, cached, cfg.video_encoder):
                return FileResponse(cached, media_type="video/mp4", headers=headers)
            # .mov and .mp4 are the same kind of file; "video/mp4" plays in more places
            return FileResponse(full, media_type="video/mp4", headers=headers)
        if row["motion_offset"]:
            with db.session(cfg.db_path) as conn:
                length = conn.execute("SELECT motion_length FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
            full = original_path(cfg, row["path"])
            raw = cached.with_name(f"{asset_id}.raw.mp4")
            if not full.is_file() or not media.extract_motion_video(full, row["motion_offset"], length, raw):
                raise HTTPException(404, "The video could not be read from the photo.")
            if not media.make_playable(raw, cached, cfg.video_encoder):
                os.replace(raw, cached)
            else:
                raw.unlink(missing_ok=True)
            return FileResponse(cached, media_type="video/mp4", headers=headers)
        raise HTTPException(404, "This photo has no moving part.")

    @r.get("/api/assets/{asset_id}/original")
    def get_original(asset_id: int, request: Request, download: bool = False):
        row = get_row(asset_id, current_user(request))
        full = file_of(row)
        if download:
            return FileResponse(full, filename=row["filename"], content_disposition_type="attachment")
        return FileResponse(full)

    # ------------------------------------------------------------ upload

    def save_upload(upload: UploadFile, root: Path) -> dict:
        name = safe_filename(upload.filename)
        if media.kind_of(name) is None:
            return {"name": upload.filename, "status": "rejected", "reason": "Unsupported file type"}

        now = datetime.now()
        dest_dir = root / "Uploads" / f"{now:%Y}" / f"{now:%m}"
        incoming = root / "Uploads" / ".incoming"  # same drive as the library, hidden from scans
        dest_dir.mkdir(parents=True, exist_ok=True)
        incoming.mkdir(parents=True, exist_ok=True)

        tmp = incoming / f"{uuid.uuid4().hex}.part"
        digest = hashlib.sha1(usedforsecurity=False)
        try:
            with open(tmp, "wb") as out:
                while chunk := upload.file.read(1024 * 1024):
                    digest.update(chunk)
                    out.write(chunk)

            with db.session(cfg.db_path) as conn:
                # A copy in someone's locked folder doesn't count: the upload is added like any new photo,
                # so nobody can find out what is locked by uploading.
                dup = conn.execute(
                    "SELECT id, trashed_at FROM assets WHERE sha1 = ? AND locked_by IS NULL"
                    " ORDER BY trashed_at IS NOT NULL LIMIT 1",
                    (digest.hexdigest(),),
                ).fetchone()
                if dup and dup["trashed_at"]:
                    # Uploading something that is in the trash brings it back.
                    conn.execute("UPDATE assets SET trashed_at = NULL WHERE id = ?", (dup["id"],))
            if dup:
                tmp.unlink()
                return {"name": upload.filename, "status": "restored" if dup["trashed_at"] else "duplicate",
                        "id": dup["id"]}

            stem, ext = os.path.splitext(name)
            n = 0
            while True:  # claim a free name atomically: "x" mode fails if the file already exists
                target = dest_dir / (name if n == 0 else f"{stem}-{n}{ext}")
                try:
                    open(target, "xb").close()
                    break
                except FileExistsError:
                    n += 1
            os.replace(tmp, target)  # replaces only our own empty placeholder

            rel = os.path.relpath(target, root).replace(os.sep, "/")
            with db.session(cfg.db_path) as conn:
                asset_id = index_file(cfg, conn, rel, target, digest.hexdigest())
            return {"name": upload.filename, "status": "added", "id": asset_id}
        except Exception as exc:
            try:
                tmp.unlink()
            except OSError:
                pass
            return {"name": upload.filename, "status": "rejected", "reason": f"Upload failed: {exc}"}

    @r.post("/api/upload")
    def upload(request: Request, files: list[UploadFile] = File(...)):
        current_user(request)
        root = library_root(cfg)
        results = [save_upload(f, root) for f in files]
        with db.session(cfg.db_path) as conn:  # a Live Photo's photo and clip may arrive together
            linking.link_pairs(conn, cfg)
        worker.poke()  # new photos may need places and faces
        return {"results": results}

    # ------------------------------------------------------------ status

    @r.get("/api/status")
    def status(request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            counts = {row["kind"]: row["c"] for row in conn.execute(
                f"SELECT kind, COUNT(*) AS c FROM assets a WHERE trashed_at IS NULL AND {db.LIVE_PART_HIDDEN}"
                f" AND {db.NOT_LOCKED} GROUP BY kind")}
            trashed = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE trashed_at IS NOT NULL AND {db.NOT_LOCKED}").fetchone()[0]
            locked = conn.execute("SELECT COUNT(*) FROM assets WHERE locked_by = ?", (user["id"],)).fetchone()[0]
            has_pin = conn.execute("SELECT pin_hash IS NOT NULL FROM users WHERE id = ?", (user["id"],)).fetchone()[0]
            unread = unread_activity(conn, user["id"])
        root = cfg.library_path
        out = {
            "version": __version__,
            "user": user,
            "library_path": str(root) if root else None,
            "library_ok": bool(root and root.is_dir()),
            "photos": counts.get("photo", 0),
            "videos": counts.get("video", 0),
            "trashed": trashed,
            "ffmpeg": media.has_ffmpeg(),
            "heif": media.HEIF,
            "raw": media.RAW,
            "scan_interval_minutes": cfg.scan_interval_minutes,
            "libraries": [{"path": str(x.path), "main": x.main, "ok": x.available()} for x in libraries.roots(cfg)],
            "locked": {"count": locked, "has_pin": bool(has_pin), "open": bool(user.get("unlocked"))},
            "activity_unread": unread,
            "map": {"tiles": cfg.map_tiles or None, "attribution": cfg.map_attribution if cfg.map_tiles else None},
        }
        out.update(worker.status())
        return out

    @r.post("/api/scan", status_code=202)
    def start_scan(request: Request, body: ScanBody | None = None):
        remove_missing = bool(body and body.remove_missing)
        if remove_missing:
            require_admin(request)  # forgetting many photos at once is an administrator's call
        else:
            current_user(request)
        library_root(cfg)
        return {"started": worker.request_scan(remove_missing=remove_missing)}

    # ------------------------------------------------------------ finding a town (for setting a location)

    @r.get("/api/places/search")
    def search_places(request: Request, q: str = Query(..., min_length=2, max_length=100)):
        """Towns whose name starts with q, from the offline place list (once it is downloaded)."""
        current_user(request)
        from .places import search_towns
        return {"places": search_towns(cfg, q)}

    return r


def unread_activity(conn, user_id: int) -> int:
    """Comments and album additions by others since this person last looked at Activity."""
    seen = conn.execute("SELECT activity_seen_at FROM users WHERE id = ?", (user_id,)).fetchone()
    since = (seen[0] if seen else None) or ""
    comments = conn.execute(
        "SELECT COUNT(*) FROM comments c LEFT JOIN assets a ON a.id = c.asset_id"
        " WHERE c.created_at > ? AND coalesce(c.user_id, -1) != ? AND (c.asset_id IS NULL OR a.locked_by IS NULL)",
        (since, user_id)).fetchone()[0]
    additions = conn.execute(
        "SELECT COUNT(DISTINCT aa.album_id || '|' || coalesce(aa.added_by, 0) || '|' || substr(aa.added_at, 1, 13))"
        " FROM album_assets aa JOIN assets a ON a.id = aa.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
        " WHERE aa.added_at > ? AND aa.added_by IS NOT NULL AND aa.added_by != ?",
        (since, user_id)).fetchone()[0]
    return comments + additions
