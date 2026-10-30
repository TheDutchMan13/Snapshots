"""Exporting (zip and PDF photo book), trimming videos and saving stills from them."""

import os
import secrets
import threading
import time
import urllib.parse
import uuid
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import db, exports, imports, libraries, media, smart, webhooks
from .app import current_user
from .routes_library import asset_dict, can_see, original_path
from . import scanner
from .scanner import index_file

EXPORT_COLS = ("a.id, a.path, a.filename, a.kind, a.taken_at, a.edits, a.city, a.state, a.country, a.description,"
               " a.locked_by")
TIME_FMT = "%Y-%m-%dT%H:%M:%S"
KEEP_HOURS = 24


class ExportBody(BaseModel):
    album: int | None = None
    ids: list[int] | None = Field(None, max_length=50000)
    format: str = Field("zip", pattern="^(zip|pdf)$")
    edited: bool = True            # zip: edited photos as edited JPEGs (else the original files)
    videos: bool = True            # zip: include videos
    paper: str = Field("letter", pattern="^(letter|a4)$")
    per_page: int = Field(4)
    title: str | None = Field(None, max_length=200)


class TrimBody(BaseModel):
    start: float = Field(ge=0)
    end: float = Field(gt=0)


class StillBody(BaseModel):
    at: float = Field(ge=0)


class Tasks:
    """Slow jobs started from the app (a photo book, a trimmed video), each in a thread of its own."""

    def __init__(self):
        self.jobs: dict[str, dict] = {}
        self.lock = threading.Lock()

    def start(self, user_id: int, kind: str, fn) -> dict:
        task_id = secrets.token_urlsafe(12)
        job = {"id": task_id, "kind": kind, "user_id": user_id, "progress": 0.0, "done": False, "error": None,
               "result": None, "created": time.time()}
        with self.lock:
            self.jobs[task_id] = job

        def progress(fraction):
            job["progress"] = round(max(0.0, min(1.0, fraction)), 3)

        def run():
            try:
                job["result"] = fn(progress)
                job["progress"] = 1.0
            except Exception as exc:
                job["error"] = str(exc) or "That didn't work."
            job["done"] = True
        threading.Thread(target=run, name=f"snapshots-{kind}", daemon=True).start()
        return self.view(job)

    @staticmethod
    def view(job: dict) -> dict:
        return {k: job[k] for k in ("id", "kind", "progress", "done", "error", "result")}

    def get(self, task_id: str, user: dict) -> dict:
        with self.lock:
            job = self.jobs.get(task_id)
        if job is None or job["user_id"] != user["id"]:
            raise HTTPException(404, "No such task.")
        return job

    def tidy(self) -> None:
        cutoff = time.time() - KEEP_HOURS * 3600
        with self.lock:
            for k in [k for k, j in self.jobs.items() if j["created"] < cutoff and j["done"]]:
                del self.jobs[k]


def _disposition(name: str) -> str:
    plain = "".join(c if 32 <= ord(c) < 127 and c not in '"\\' else "_" for c in name)
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{urllib.parse.quote(name)}"


def _safe_name(text: str) -> str:
    from .app import safe_filename
    text = (text or "Snapshots").replace("/", "_").replace("\\", "_")
    return os.path.splitext(safe_filename(text + ".x"))[0] or "Snapshots"


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()
    tasks = Tasks()
    ctx.tasks = tasks
    zips: dict[str, dict] = {}
    zips_lock = threading.Lock()

    def tidy_exports() -> None:
        tasks.tidy()
        cutoff = time.time() - KEEP_HOURS * 3600
        with zips_lock:
            for k in [k for k, z in zips.items() if z["created"] < cutoff]:
                zips.pop(k, None)
        try:
            for p in cfg.export_dir.glob("*"):
                if p.stat().st_mtime < cutoff:
                    p.unlink(missing_ok=True)
        except OSError:
            pass

    def rows_for(conn, user, album: int | None, ids: list[int] | None):
        """What an export holds, oldest first. Locked items only for their owner with the folder open."""
        if album is not None:
            row = conn.execute("SELECT name FROM albums WHERE id = ?", (album,)).fetchone()
            if row is None:
                raise HTTPException(404, "No such album.")
            f, _ = smart.resolve(conn, cfg, worker, db.Filter(album=album))
            where, params = db.where_clause(f, user["id"])
            return row["name"], conn.execute(
                f"SELECT {EXPORT_COLS} FROM assets a WHERE {where} ORDER BY a.taken_at, a.id", params).fetchall()
        wanted = list(dict.fromkeys(i for i in ids or [] if isinstance(i, int)))
        out = []
        for start in range(0, len(wanted), 900):
            chunk = wanted[start:start + 900]
            out += [x for x in conn.execute(
                f"SELECT {EXPORT_COLS} FROM assets a WHERE a.id IN ({','.join('?' * len(chunk))})", chunk)
                if can_see(x, user)]
        out.sort(key=lambda x: (x["taken_at"], x["id"]))
        return None, out

    # ------------------------------------------------------------ export

    @r.post("/api/exports", status_code=201)
    def start_export(body: ExportBody, request: Request):
        """zip: returns an address that downloads the zip (made while it downloads).
        pdf: starts making the photo book; follow it with /api/tasks/{id}."""
        user = current_user(request)
        if body.album is None and not body.ids:
            raise HTTPException(400, "Choose an album or some photos.")
        if body.per_page not in exports.LAYOUTS:
            raise HTTPException(400, "Choose 1, 2, 4 or 6 photos a page.")
        with db.session(cfg.db_path) as conn:
            album_name, rows = rows_for(conn, user, body.album, body.ids)
        if not rows:
            raise HTTPException(400, "There is nothing to export.")
        title = (body.title or album_name or "Photos").strip() or "Photos"
        tidy_exports()
        if body.format == "zip":
            token = secrets.token_urlsafe(16)
            with zips_lock:
                zips[token] = {"user_id": user["id"], "ids": [x["id"] for x in rows], "edited": body.edited,
                           "videos": body.videos, "name": _safe_name(title) + ".zip", "created": time.time()}
            return {"url": f"/api/exports/{token}/download", "count": len(rows)}
        photos = [x for x in rows if x["kind"] == "photo"]
        if not photos:
            raise HTTPException(400, "A photo book needs photos (videos are left out).")
        first, last = photos[0]["taken_at"][:10], photos[-1]["taken_at"][:10]

        def nice(day):
            d = datetime.strptime(day, "%Y-%m-%d")
            return f"{d.day} {d:%B %Y}"
        subtitle = nice(first) if first == last else f"{nice(first)} – {nice(last)}"
        cfg.export_dir.mkdir(parents=True, exist_ok=True)
        dest = cfg.export_dir / f"{uuid.uuid4().hex}.pdf"
        file_name = _safe_name(title) + ".pdf"

        def make(progress):
            info = exports.build_pdf(cfg, rows, dest, title, subtitle, body.paper, body.per_page, progress)
            return dict(info, file=dest.name, name=file_name)
        job = tasks.start(user["id"], "photo book", make)
        return dict(job, count=len(photos))

    @r.get("/api/exports/{token}/download")
    def download_export(token: str, request: Request):
        user = current_user(request)
        with zips_lock:
            z = zips.get(token)
        if z is None or z["user_id"] != user["id"]:
            raise HTTPException(404, "This download has expired. Export again.")
        with db.session(cfg.db_path) as conn:
            _, rows = rows_for(conn, user, None, z["ids"])
        return StreamingResponse(exports.zip_stream(cfg, rows, z["edited"], z["videos"]), media_type="application/zip",
                                 headers={"Content-Disposition": _disposition(z["name"]), "Cache-Control": "no-store"})

    @r.get("/api/tasks/{task_id}")
    def task(task_id: str, request: Request):
        return Tasks.view(tasks.get(task_id, current_user(request)))

    @r.get("/api/tasks/{task_id}/file")
    def task_file(task_id: str, request: Request):
        job = tasks.get(task_id, current_user(request))
        result = job["result"] or {}
        if not job["done"] or not result.get("file"):
            raise HTTPException(404, "Not ready yet.")
        path = cfg.export_dir / result["file"]
        if not path.is_file():
            raise HTTPException(404, "This file has expired. Export again.")
        return FileResponse(path, media_type="application/pdf",
                            headers={"Content-Disposition": _disposition(result["name"]), "Cache-Control": "no-store"})

    # ------------------------------------------------------------ trimming videos and stills

    def source_video(asset_id: int, user):
        with db.session(cfg.db_path) as conn:
            row = db.get_asset(conn, asset_id, user["id"])
        if row is None or not can_see(row, user):
            raise HTTPException(404, "Not found")
        if row["kind"] != "video":
            raise HTTPException(400, "That isn't a video.")
        if not media.has_ffmpeg():
            raise HTTPException(409, "Trimming videos and saving stills needs ffmpeg.")
        full = original_path(cfg, row["path"])
        if not full.is_file():
            raise HTTPException(404, "The file is no longer on disk.")
        return row, full

    def add_beside(row, full: Path, make, name: str, offset: float, user_id: int) -> int:
        """Make a new file next to the original (make(path) writes it) and add it like the original:
        same date (plus offset), place, description, tags, albums and locked folder."""
        root = libraries.root_of(cfg, row["path"])
        tmp = full.parent / f".{uuid.uuid4().hex}{Path(name).suffix}"
        try:
            make(tmp)
            target = imports.claim_name(full.parent, name)   # a scan leaves it alone until it has its row
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        try:
            os.replace(tmp, target)
            return _add_row(row, root, target, offset, user_id)
        finally:
            scanner.release(target)
            tmp.unlink(missing_ok=True)

    def _add_row(row, root, target: Path, offset: float, user_id: int) -> int:
        with db.session(cfg.db_path) as conn:
            new_id = index_file(cfg, conn, libraries.stored_path(root, target), target)
            src = conn.execute("SELECT * FROM assets WHERE id = ?", (row["id"],)).fetchone()
            try:
                taken = (datetime.strptime(src["taken_at"], TIME_FMT) + timedelta(seconds=int(offset))).strftime(TIME_FMT)
                conn.execute("UPDATE assets SET taken_at = ?, taken_at_file = NULL WHERE id = ?", (taken, new_id))
            except ValueError:
                pass
            new = conn.execute("SELECT lat FROM assets WHERE id = ?", (new_id,)).fetchone()
            if src["lat"] is not None and new["lat"] is None:
                conn.execute("UPDATE assets SET lat = ?, lon = ?, position_edited = 1, geo_done = 0 WHERE id = ?",
                             (src["lat"], src["lon"], new_id))
            conn.execute("UPDATE assets SET description = ?, locked_by = ? WHERE id = ?",
                         (src["description"], src["locked_by"], new_id))
            conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) SELECT ?, tag_id FROM asset_tags"
                         " WHERE asset_id = ?", (new_id, row["id"]))
            if src["locked_by"] is None:
                conn.execute("INSERT OR IGNORE INTO album_assets (album_id, asset_id, added_at, added_by)"
                             " SELECT aa.album_id, ?, ?, ? FROM album_assets aa JOIN albums al ON al.id = aa.album_id"
                             " WHERE aa.asset_id = ? AND al.rule IS NULL",
                             (new_id, datetime.now().isoformat(timespec="seconds"), user_id, row["id"]))
                webhooks.emit_added(conn, [new_id], "made")
        worker.poke()
        return new_id

    def stamp(seconds: float) -> str:
        m, s = divmod(int(seconds), 60)
        return f"{m}m{s:02d}s"

    @r.post("/api/assets/{asset_id}/trim", status_code=202)
    def trim(asset_id: int, body: TrimBody, request: Request):
        """Save the part between start and end as a new video beside the original (which isn't changed)."""
        user = current_user(request)
        row, full = source_video(asset_id, user)
        if body.end - body.start < 0.5:
            raise HTTPException(400, "Keep at least half a second.")
        duration = media.probe_video(full).get("duration")
        if duration and body.start >= duration:
            raise HTTPException(400, "The start is past the end of the video.")
        end = min(body.end, duration) if duration else body.end
        stem = os.path.splitext(row["filename"])[0]
        name = f"{stem} (trimmed {stamp(body.start)}-{stamp(end)}).mp4"

        def work(progress):
            def make(path):
                if not media.trim_video(full, path, body.start, end, cfg.video_encoder, progress):
                    raise RuntimeError("The video could not be trimmed.")
            new_id = add_beside(row, full, make, name, body.start, user["id"])
            return {"id": new_id}
        return tasks.start(user["id"], "trim", work)

    @r.post("/api/assets/{asset_id}/still", status_code=201)
    def still(asset_id: int, body: StillBody, request: Request):
        """Save the frame `at` seconds in as a photo beside the video, with the video's date and place."""
        user = current_user(request)
        row, full = source_video(asset_id, user)
        frame = media.video_frame(full, body.at)
        if frame is None:
            raise HTTPException(400, "No picture could be read at that moment of the video.")
        try:
            taken = datetime.strptime(row["taken_at"], TIME_FMT) + timedelta(seconds=int(body.at))
        except ValueError:
            taken = None
        stem = os.path.splitext(row["filename"])[0]
        new_id = add_beside(row, full, lambda path: media.save_still(frame, path, taken, row["lat"], row["lon"],
                                                                      row["camera"]),
                            f"{stem} (still {stamp(body.at)}).jpg", body.at, user["id"])
        with db.session(cfg.db_path) as conn:
            return asset_dict(db.get_asset(conn, new_id, user["id"]))

    return r
