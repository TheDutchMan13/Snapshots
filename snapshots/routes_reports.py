"""The library health check, the storage report and the year in review."""

import os
import shutil
import sqlite3
import threading
import time
import uuid
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, health, imports, libraries, media, review, webhooks
from .app import current_user, require_admin
from .config import save_settings
from .routes_library import asset_dict
from .scanner import index_file


class HealthSettings(BaseModel):
    days: float = Field(ge=0, le=365)    # 0: only when asked; otherwise at least 1


class ReviewBody(BaseModel):
    count: int = Field(60, ge=10, le=200)


class VideoBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=300)
    size: str = Field("720p", pattern="^(720p|1080p)$")
    seconds: float = Field(4, ge=2, le=10)
    title: str = Field("", max_length=100)
    save: bool = False          # also add the video to the library


def _dir_size(path: Path) -> tuple[int, int]:
    """Bytes and files under a folder (0, 0 if it isn't there)."""
    total = files = 0
    for dirpath, _dirs, names in os.walk(path):
        for n in names:
            try:
                total += os.stat(os.path.join(dirpath, n)).st_size
                files += 1
            except OSError:
                pass
    return total, files


def _disk(path: Path) -> dict | None:
    try:
        u = shutil.disk_usage(path)
    except OSError:
        return None
    return {"total": u.total, "used": u.used, "free": u.free}


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()
    cache: dict = {}
    cache_lock = threading.Lock()

    # ------------------------------------------------------------ library health

    def health_state(conn) -> dict:
        job = worker.health
        return {"running": job.running(), "progress": dict(job.state), "days": cfg.health_check_days,
                "last": health.last_run(conn), "offsite_folder": cfg.offsite_method == "folder"}

    @r.get("/api/library-health")
    def health_get(request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            return health_state(conn)

    @r.post("/api/library-health/run")
    def health_run(request: Request):
        user = require_admin(request)
        worker.health.start(user["id"])
        time.sleep(0.05)
        with db.session(cfg.db_path) as conn:
            return health_state(conn)

    @r.post("/api/library-health/stop")
    def health_stop(request: Request):
        require_admin(request)
        worker.health.stop()
        with db.session(cfg.db_path) as conn:
            return health_state(conn)

    @r.put("/api/library-health/settings")
    def health_settings(body: HealthSettings, request: Request):
        require_admin(request)
        if 0 < body.days < 1:
            raise HTTPException(400, "Check at most once a day (or 0 for only when you ask).")
        save_settings({"health_check_days": body.days}, cfg.config_file)
        cfg.health_check_days = body.days
        worker.health.retry_at = 0
        worker.poke()
        with db.session(cfg.db_path) as conn:
            return health_state(conn)

    @r.get("/api/library-health/issues")
    def health_issues(request: Request, status: str | None = Query(None, pattern="^(damaged|unreadable|missing|changed|unavailable)$"),
                      offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=200)):
        user = require_admin(request)
        with db.session(cfg.db_path) as conn:
            found, total = health.issues(conn, status, offset, limit)
            ids = [i["asset_id"] for i in found]
            marks = ",".join("?" * len(ids)) or "NULL"
            # Directly, not through a grid filter: a Live Photo's hidden clip and trashed items are listed too.
            rows = {x["id"]: x for x in conn.execute(
                f"SELECT {db.ASSET_COLUMNS}, (fav.asset_id IS NOT NULL) AS favorite FROM assets a"
                f" LEFT JOIN favorites fav ON fav.asset_id = a.id AND fav.user_id = ?"
                f" WHERE a.id IN ({marks}) AND a.locked_by IS NULL", [user["id"]] + ids)}
            out = []
            for i in found:
                row = rows.get(i["asset_id"])
                if row is None:
                    continue
                item = asset_dict(row)
                restorable = False
                if i["status"] in health.PROBLEMS:
                    p = health.offsite_copy(cfg, row["path"])
                    restorable = bool(p and p.is_file())
                out.append({"item": item, "status": i["status"], "detail": i["detail"], "restorable": restorable})
        return {"issues": out, "total": total, "next": offset + limit if offset + limit < total else None}

    @r.post("/api/library-health/restore/{asset_id}")
    def health_restore(asset_id: int, request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            try:
                message = health.restore(cfg, conn, asset_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc))
            except ValueError as exc:
                raise HTTPException(409, str(exc))
            except (OSError, sqlite3.OperationalError) as exc:
                raise HTTPException(409, f"The file could not be put back: {exc}")
        return {"ok": True, "message": message}

    # ------------------------------------------------------------ storage report

    def storage_report(user) -> dict:
        admin = bool(user.get("is_admin"))
        with db.session(cfg.db_path) as conn:
            base = f"a.trashed_at IS NULL AND {db.NOT_LOCKED}"
            kinds = {"photos": [0, 0], "raw": [0, 0], "videos": [0, 0]}
            years: dict[str, list] = defaultdict(lambda: [0, 0, 0])   # photos bytes, video bytes, items
            folders: dict[str, list] = defaultdict(lambda: [0, 0])
            names = {r.prefix: r.name for r in libraries.roots(cfg) if not r.main}
            for row in conn.execute(f"SELECT a.path, a.filename, a.kind, a.size, a.taken_at FROM assets a WHERE {base}"):
                size = row["size"] or 0
                k = "videos" if row["kind"] == "video" else "raw" if media.is_raw(row["filename"]) else "photos"
                kinds[k][0] += 1
                kinds[k][1] += size
                y = years[row["taken_at"][:4]]
                y[1 if row["kind"] == "video" else 0] += size
                y[2] += 1
                root = libraries.root_of(cfg, row["path"])
                if root is None:
                    top = "Folders no longer in the library"
                elif root.main:
                    parts = row["path"].split("/")
                    top = parts[0] if len(parts) > 1 else "(the library folder itself)"
                else:
                    top = names.get(root.prefix, root.name) + " (another drive)"
                folders[top][0] += 1
                folders[top][1] += size
            trash = conn.execute(f"SELECT COUNT(*), COALESCE(SUM(size), 0) FROM assets a WHERE a.trashed_at IS NOT NULL"
                                 f" AND {db.NOT_LOCKED}").fetchone()
            biggest_rows = conn.execute(
                f"SELECT {db.ASSET_COLUMNS}, 0 AS favorite FROM assets a"
                f" WHERE {base} AND {db.LIVE_PART_HIDDEN} ORDER BY a.size DESC LIMIT 20").fetchall()
            dup_space = 0
            dups = getattr(ctx, "duplicates", None)
            if dups is not None and cfg.photo_checks:
                dup_space = sum(g["space"] for g in dups.groups(conn))
        data = {}
        for key, path in (("thumbnails", cfg.thumb_dir), ("previews", cfg.preview_dir), ("videos", cfg.video_dir),
                          ("faces", cfg.face_dir), ("edited", cfg.edited_dir), ("motion", cfg.motion_dir),
                          ("models", cfg.model_dir), ("places", cfg.places_dir), ("backups", cfg.backups_path),
                          ("exports", cfg.export_dir)):
            data[key] = _dir_size(path)[0]
        try:
            data["database"] = sum(p.stat().st_size for p in cfg.data_dir.glob("snapshots.db*") if p.is_file())
        except OSError:
            data["database"] = 0
        disks, seen = [], set()
        shown = (lambda name, path: f"{name} ({path})") if admin else (lambda name, path: name)
        for label, path in ([(shown("Library", cfg.library_path), cfg.library_path)] if cfg.library_path else []) + \
                [(f"{r.name} (another drive)", r.path) for r in libraries.roots(cfg) if not r.main] + \
                [(shown("Snapshots' data", cfg.data_dir), cfg.data_dir)] + \
                ([("Offsite copy", Path(cfg.offsite_target))] if cfg.offsite_method == "folder" and cfg.offsite_target else []):
            u = _disk(Path(path))
            if u is None:
                disks.append({"label": label, "available": False})
                continue
            try:
                key = os.stat(path).st_dev               # the same drive (volume) seen twice
            except OSError:
                key = (u["total"], u["free"])
            if key in seen:
                continue
            seen.add(key)
            disks.append(dict(u, label=label, available=True))
        return {
            "kinds": {k: {"items": v[0], "bytes": v[1]} for k, v in kinds.items()},
            "total": sum(v[1] for v in kinds.values()),
            "years": [{"year": y, "photos": v[0], "videos": v[1], "items": v[2]} for y, v in sorted(years.items())],
            "folders": sorted(({"folder": f, "items": v[0], "bytes": v[1]} for f, v in folders.items()),
                              key=lambda x: -x["bytes"])[:30],
            "biggest": [asset_dict(x) for x in biggest_rows],
            "trash": {"items": trash[0], "bytes": trash[1]},
            "duplicates": dup_space,
            "data": data,
            "data_total": sum(data.values()),
            "video_cache_limit": int(cfg.video_cache_gb * 1e9),
            "disks": disks,
            "made_at": datetime.now().isoformat(timespec="seconds"),
        }

    @r.get("/api/storage")
    def storage(request: Request, fresh: bool = False):
        user = current_user(request)
        admin = bool(user.get("is_admin"))
        fresh = fresh and admin            # a full count reads the data folder: administrators only
        with cache_lock:
            hit = cache.get(admin)
        if fresh or not hit or time.time() - hit[0] >= 600:
            out = storage_report(user)
            with cache_lock:
                cache[admin] = (time.time(), out)
        else:
            out = hit[1]
        # Something locked since the count is never shown.
        ids = [b["id"] for b in out["biggest"]]
        if ids:
            with db.session(cfg.db_path) as conn:
                marks = ",".join("?" * len(ids))
                still = {r[0] for r in conn.execute(
                    f"SELECT id FROM assets WHERE id IN ({marks}) AND locked_by IS NULL AND trashed_at IS NULL", ids)}
            out = dict(out, biggest=[b for b in out["biggest"] if b["id"] in still])
        return out

    # ------------------------------------------------------------ year in review

    @r.get("/api/review")
    def review_years(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            return {"years": review.years(conn), "ffmpeg": media.has_ffmpeg(), "seconds": cfg.review_seconds}

    @r.post("/api/review/{year}")
    def review_pick(year: int, body: ReviewBody, request: Request):
        user = current_user(request)
        if not 1800 <= year <= 2200:
            raise HTTPException(400, "That isn't a year.")
        with db.session(cfg.db_path) as conn:
            picked = review.pick(conn, cfg, year, user["id"], body.count)
            rows, _ = db.list_assets(conn, db.Filter(ids=picked["ids"]), user["id"], "", max(1, len(picked["ids"])))
        by_id = {x["id"]: x for x in rows}
        picked["items"] = [asset_dict(by_id[i]) for i in picked["ids"] if i in by_id]
        return picked

    @r.post("/api/review/{year}/video", status_code=202)
    def review_video(year: int, body: VideoBody, request: Request):
        user = current_user(request)
        if not media.has_ffmpeg():
            raise HTTPException(409, "Making a video needs ffmpeg.")
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
            marks = ",".join("?" * len(ids)) or "NULL"
            rows = conn.execute(f"SELECT id, path, edits, taken_at, kind FROM assets WHERE id IN ({marks})"
                                f" AND kind = 'photo' AND trashed_at IS NULL ORDER BY taken_at, id", ids).fetchall()
            facts = review.pick(conn, cfg, year, user["id"], 10) if rows else None
        if not rows:
            raise HTTPException(400, "Choose some photos first.")
        title = body.title.strip() or str(year)
        lines = [f"{len(rows)} moments"]
        if facts and facts["places"]:
            lines.append(" · ".join(facts["places"][:3]))
        if facts and facts["people"]:
            lines.append("With " + ", ".join(facts["people"][:4]))
        cfg.export_dir.mkdir(parents=True, exist_ok=True)
        dest = cfg.export_dir / f"{uuid.uuid4().hex}.mp4"
        from .routes_tools import _safe_name
        file_name = _safe_name(f"{title} in review" if title == str(year) else title) + ".mp4"
        if user.get("is_admin") and float(body.seconds) != cfg.review_seconds:
            save_settings({"review_seconds": float(body.seconds)}, cfg.config_file)   # remembered for next time
            cfg.review_seconds = float(body.seconds)

        def make(progress):
            info = review.render_video(cfg, [dict(x) for x in rows], dest, title, lines, body.size, body.seconds,
                                       lambda f: progress(f * (0.97 if body.save else 1.0)))
            out = dict(info, file=dest.name, name=file_name)
            if body.save:
                out["id"] = save_to_library(dest, file_name, year, user["id"])
            return out
        return ctx.tasks.start(user["id"], "year in review", make)

    def save_to_library(src: Path, name: str, year: int, user_id: int) -> int:
        """Copy the video into the library (folder "Year in review"), dated the last day of the year."""
        root = next(r for r in libraries.roots(cfg) if r.main)
        if not root.available():
            raise RuntimeError("The library folder is not available.")
        from . import scanner
        folder = root.path / "Year in review"
        target = imports.claim_name(folder, name)
        try:
            if not target.resolve().is_relative_to(folder.resolve()):
                raise RuntimeError("That title can't be used as a file name.")
            shutil.copyfile(src, target)
            with db.session(cfg.db_path) as conn:
                new_id = index_file(cfg, conn, libraries.stored_path(root, target), target)
                conn.execute("UPDATE assets SET taken_at = ?, taken_at_file = NULL WHERE id = ?",
                             (f"{year:04d}-12-31T23:59:00", new_id))
                webhooks.emit_added(conn, [new_id], "made")
        finally:
            scanner.release(target)
        worker.poke()
        return new_id

    return r
