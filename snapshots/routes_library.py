"""Browsing the library, serving files, uploading, and status."""

import hashlib
import os
import uuid
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Depends, File, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse

from . import __version__, analysis, db, media
from pydantic import BaseModel

from .app import current_user, require_admin, safe_filename
from .scanner import index_file

KIND = Query("", pattern="^(photo|video)?$")


class ScanBody(BaseModel):
    remove_missing: bool = False


TIME_PATTERN = r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}$"


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
    ) -> db.Filter:
        return db.Filter(q=q, kind=kind, favorite=favorite, trashed=trashed, album=album, person=person,
                         country=country, state=state, city=city, start=start, end=end,
                         low_quality=analysis.low_quality_sql(cfg) if quality else None)

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
        "v": row["sha1"][:8],  # changes when the file changes, so browsers refetch
    }
    if score is not None:
        out["score"] = round(score, 4)
    return out


def library_root(cfg) -> Path:
    root = cfg.library_path
    if root is None or not root.is_dir():
        raise HTTPException(409, "The library folder is not available.")
    return root


def original_path(cfg, rel: str) -> Path:
    root = Path(os.path.normpath(library_root(cfg)))
    full = Path(os.path.normpath(root / rel))
    if not full.is_relative_to(root):  # defence in depth; paths come from our own scan
        raise HTTPException(404, "Not found")
    return full


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()
    filter_params = make_filter_params(cfg)

    def get_row(asset_id: int, user_id: int):
        with db.session(cfg.db_path) as conn:
            row = db.get_asset(conn, asset_id, user_id)
        if row is None:
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

    @r.get("/api/assets")
    def list_assets(request: Request, f: db.Filter = Depends(filter_params), cursor: str = "",
                    limit: int = Query(120, ge=1, le=500), look: bool = False):
        """The photos in a grid, newest first. look=true: photos whose content matches q, best first."""
        user = current_user(request)
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
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            return {"months": db.timeline(conn, f, user["id"])}

    @r.get("/api/search")
    def search_summary(request: Request, f: db.Filter = Depends(filter_params)):
        """How many photos match q by words and names, and how many by what they show."""
        user = current_user(request)
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
        row = get_row(asset_id, user["id"])
        out = asset_dict(row)
        out["text"] = row["ocr_text"]
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
        return out

    @r.get("/api/assets/{asset_id}/thumb")
    def get_thumb(asset_id: int):
        path = cfg.thumb_path(asset_id)
        if not path.is_file():
            raise HTTPException(404, "No thumbnail")
        return FileResponse(path, media_type="image/jpeg",
                            headers={"Cache-Control": "private, max-age=31536000, immutable"})

    @r.get("/api/assets/{asset_id}/view")
    def get_view(asset_id: int, request: Request):
        """What the viewer shows: the original if browsers can display it, else a JPEG."""
        row = get_row(asset_id, current_user(request)["id"])
        full = original_path(cfg, row["path"])
        if not full.is_file():
            raise HTTPException(404, "The file is no longer on disk.")
        if row["kind"] == "video" or full.suffix.lower() in media.BROWSER_NATIVE:
            return FileResponse(full, headers={"Cache-Control": "private, max-age=3600"})
        preview = cfg.preview_path(asset_id)
        if not preview.is_file() and not media.make_preview(full, preview):
            raise HTTPException(415, "This file cannot be displayed.")
        return FileResponse(preview, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

    @r.get("/api/assets/{asset_id}/original")
    def get_original(asset_id: int, request: Request, download: bool = False):
        row = get_row(asset_id, current_user(request)["id"])
        full = original_path(cfg, row["path"])
        if not full.is_file():
            raise HTTPException(404, "The file is no longer on disk.")
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
                dup = conn.execute(
                    "SELECT id, trashed_at FROM assets WHERE sha1 = ? ORDER BY trashed_at IS NOT NULL LIMIT 1",
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
        worker.poke()  # new photos may need places and faces
        return {"results": results}

    # ------------------------------------------------------------ status

    @r.get("/api/status")
    def status(request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            counts = {row["kind"]: row["c"] for row in conn.execute(
                "SELECT kind, COUNT(*) AS c FROM assets WHERE trashed_at IS NULL GROUP BY kind")}
            trashed = conn.execute("SELECT COUNT(*) FROM assets WHERE trashed_at IS NOT NULL").fetchone()[0]
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
            "scan_interval_minutes": cfg.scan_interval_minutes,
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

    return r
