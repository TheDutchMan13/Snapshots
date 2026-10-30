"""Settings people change from the app: extra library folders, backups, video conversion and API keys."""

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import auth, backup, db, libraries, media
from .app import current_user, require_admin, require_session
from .config import save_extra_libraries
from .scanner import delete_asset_row


class FolderBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()

    def libraries_json(conn) -> list[dict]:
        out = []
        for root in libraries.roots(cfg):
            if root.main:
                n = conn.execute("SELECT COUNT(*) FROM assets WHERE NOT (path GLOB '/*' OR path GLOB '[A-Za-z]:/*')"
                                 ).fetchone()[0]
            else:
                n = conn.execute("SELECT COUNT(*) FROM assets WHERE substr(path, 1, ?) = ?",
                                 (len(root.prefix), root.prefix)).fetchone()[0]
            out.append({"path": str(root.path), "name": root.name, "main": root.main, "available": root.available(),
                        "items": n, "folder": "" if root.main else root.prefix.rstrip("/")})
        return out

    @r.get("/api/settings")
    def settings(request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            libs = libraries_json(conn)
        return {
            "libraries": libs,
            "config_file": str(cfg.config_file) if cfg.config_file else None,
            "backups": {
                "folder": str(cfg.backups_path), "hours": cfg.backup_hours, "keep": cfg.backup_keep,
                "list": backup.list_backups(cfg), "message": worker.backup.state.get("message"),
            },
            "video": {"encoder": cfg.video_encoder, "cache_gb": cfg.video_cache_gb, "ffmpeg": media.has_ffmpeg()},
        }

    # ------------------------------------------------------------ extra library folders

    @r.post("/api/libraries", status_code=201)
    def add_library(body: FolderBody, request: Request):
        """Add another folder of photos (another drive, say). Its files are read, never changed."""
        require_admin(request)
        folder = Path(body.path.strip().strip('"'))
        problem = libraries.problem_with(cfg, folder)
        if problem:
            raise HTTPException(400, problem)
        cfg.extra_libraries = [*cfg.extra_libraries, folder]
        cfg.config_file = save_extra_libraries(cfg.extra_libraries, cfg.config_file)
        worker.request_scan()
        with db.session(cfg.db_path) as conn:
            return {"libraries": libraries_json(conn)}

    @r.delete("/api/libraries")
    def remove_library(request: Request, path: str = Query(..., min_length=1, max_length=1000)):
        """Stop reading a folder. Its items leave Snapshots (with their albums, tags and faces);
        the files themselves are not touched."""
        require_admin(request)
        target = next((x for x in libraries.roots(cfg) if not x.main and str(x.path) == path), None)
        if target is None:
            raise HTTPException(404, "That folder is not one of the extra library folders.")
        cfg.extra_libraries = [p for p in cfg.extra_libraries if str(p) != path]
        cfg.config_file = save_extra_libraries(cfg.extra_libraries, cfg.config_file)
        removed = 0
        with db.session(cfg.db_path) as conn:
            ids = [x[0] for x in conn.execute("SELECT id FROM assets WHERE substr(path, 1, ?) = ?",
                                              (len(target.prefix), target.prefix))]
            for i in ids:
                delete_asset_row(cfg, conn, i)
                removed += 1
            libs = libraries_json(conn)
        return {"removed": removed, "libraries": libs}

    # ------------------------------------------------------------ backups

    @r.post("/api/backups", status_code=201)
    def make_backup(request: Request):
        require_admin(request)
        try:
            path = backup.backup_now(cfg)
        except Exception as exc:
            raise HTTPException(500, f"The backup failed: {exc}") from None
        worker.backup.state.update(message=None)
        return {"name": path.name, "list": backup.list_backups(cfg)}

    # ------------------------------------------------------------ video conversion

    @r.get("/api/video-encoder")
    def video_encoder(request: Request):
        """Which encoder converts videos on this computer (tests the graphics card once; can take a few seconds)."""
        require_admin(request)
        if not media.has_ffmpeg():
            return {"setting": cfg.video_encoder, "using": None, "ffmpeg": False}
        return {"setting": cfg.video_encoder, "using": media.pick_encoder(cfg.video_encoder), "ffmpeg": True}

    # ------------------------------------------------------------ API keys

    @r.get("/api/keys")
    def list_keys(request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT id, name, hint, created_at, last_used_at FROM api_keys WHERE user_id = ?"
                                " ORDER BY created_at DESC, id DESC", (user["id"],)).fetchall()
        return {"keys": [dict(x) for x in rows]}

    @r.post("/api/keys", status_code=201)
    def create_key(body: KeyBody, request: Request):
        """A key for a script or another app. It can do what you can (not open your locked folder).
        The key is shown only once."""
        user = require_session(request)
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(400, "Give the key a name, like the app that will use it.")
        with db.session(cfg.db_path) as conn:
            if conn.execute("SELECT COUNT(*) FROM api_keys WHERE user_id = ?", (user["id"],)).fetchone()[0] >= 50:
                raise HTTPException(400, "You have 50 keys already. Delete some first.")
            key_id, key = auth.create_api_key(conn, user["id"], name)
        return {"id": key_id, "name": name, "key": key}

    @r.delete("/api/keys/{key_id}")
    def delete_key(key_id: int, request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            n = conn.execute("DELETE FROM api_keys WHERE id = ? AND user_id = ?", (key_id, user["id"])).rowcount
        if not n:
            raise HTTPException(404, "No such key.")
        return {"ok": True}

    return r
