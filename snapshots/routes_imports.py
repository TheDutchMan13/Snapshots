"""Importing from Immich and from Google Photos (Takeout). Administrators only: an import adds albums and
names for everyone, and may copy photos into the library."""

import json

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, immich, imports, takeout
from .app import require_admin


class ImmichBody(BaseModel):
    url: str = Field(min_length=1, max_length=500)
    key: str = Field(min_length=1, max_length=500)
    user_id: int | None = None       # whose favourites Immich's favourites become (default: you)
    copy_missing: bool = True
    dry_run: bool = False


class TakeoutBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)
    user_id: int | None = None
    copy_missing: bool = True
    dry_run: bool = False


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()
    manager = imports.Manager(cfg, worker)
    ctx.imports = manager

    def target_user(admin: dict, user_id: int | None) -> int:
        if user_id is None:
            return admin["id"]
        with db.session(cfg.db_path) as conn:
            if conn.execute("SELECT 1 FROM users WHERE id = ?", (user_id,)).fetchone() is None:
                raise HTTPException(404, "No such account.")
        return user_id

    def ready(copy_missing: bool, dry_run: bool) -> None:
        if copy_missing and not dry_run and (cfg.library_path is None or not cfg.library_path.is_dir()):
            raise HTTPException(409, "The library folder is not available, so nothing can be copied into it.")

    @r.get("/api/imports")
    def state(request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            history = [
                {"id": x["id"], "kind": x["kind"], "started_at": x["started_at"], "finished_at": x["finished_at"],
                 "by": x["username"], **json.loads(x["summary"] or "{}")}
                for x in conn.execute("SELECT i.*, u.username FROM imports i LEFT JOIN users u ON u.id = i.started_by"
                                      " ORDER BY i.id DESC LIMIT 10")]
        return {"current": manager.snapshot(), "history": history}

    @r.post("/api/imports/immich", status_code=202)
    def from_immich(body: ImmichBody, request: Request):
        """Read Immich's albums, favourites, people and so on through its API. dry_run: only count."""
        admin = require_admin(request)
        user_id = target_user(admin, body.user_id)
        ready(body.copy_missing, body.dry_run)
        try:
            client = immich.Client(body.url, body.key)   # checks the address and key are given
        except immich.ImmichError as exc:
            raise HTTPException(400, str(exc)) from None
        started = manager.start("immich", lambda m: immich.run(m, cfg, client.base, client.key, user_id,
                                                                body.copy_missing, body.dry_run),
                                admin["id"], body.dry_run)
        if not started:
            raise HTTPException(409, "An import is already running.")
        return manager.snapshot()

    @r.post("/api/imports/takeout", status_code=202)
    def from_takeout(body: TakeoutBody, request: Request):
        """Read a Google Photos export (the .zip files, or the folder they were unpacked into)."""
        admin = require_admin(request)
        user_id = target_user(admin, body.user_id)
        ready(body.copy_missing, body.dry_run)
        started = manager.start("takeout", lambda m: takeout.run(m, cfg, body.path, user_id, body.copy_missing,
                                                                  body.dry_run),
                                admin["id"], body.dry_run)
        if not started:
            raise HTTPException(409, "An import is already running.")
        return manager.snapshot()

    @r.post("/api/imports/stop")
    def stop(request: Request):
        require_admin(request)
        manager.cancel()
        return manager.snapshot()

    return r
