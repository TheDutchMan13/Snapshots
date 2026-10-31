"""Settings people change from the app: extra library folders, backups, video conversion and API keys."""

import json
import threading
import urllib.parse
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import auth, backup, db, libraries, media, offsite, startup, storage, webhooks
from .app import current_user, require_admin, require_session
from .config import OFFSITE_METHODS, save_extra_libraries, save_settings
from .scanner import delete_asset_row


class FolderBody(BaseModel):
    path: str = Field(min_length=1, max_length=1000)


class StartupBody(BaseModel):
    enabled: bool


class KeyBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class OffsiteBody(BaseModel):
    method: str = Field("", max_length=10)
    target: str = Field("", max_length=1000)
    hours: float = Field(24, ge=0, le=24 * 365)


class WebhookBody(BaseModel):
    url: str = Field(min_length=1, max_length=1000)
    events: list[str] = Field(min_length=1, max_length=20)


class XmpBody(BaseModel):
    enabled: bool


class StorageBody(BaseModel):
    template: str = Field(max_length=300)


class WebhookChange(BaseModel):
    enabled: bool | None = None
    events: list[str] | None = Field(None, min_length=1, max_length=20)


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
            "offsite": offsite_json(),
            "xmp": xmp_json(),
            "storage": storage_json(cfg.upload_template),
            "startup": startup_json(),
        }

    def startup_json() -> dict:
        return dict(startup.status(cfg), can_stop=ctx.quit is not None, port=cfg.port)

    @r.put("/api/settings/startup")
    def save_startup(body: StartupBody, request: Request):
        require_admin(request)
        if not startup.supported():
            raise HTTPException(400, "Starting with Windows is only available in the Windows app.")
        try:
            if body.enabled:
                startup.enable(cfg)
            else:
                startup.disable()
        except OSError as exc:
            raise HTTPException(409, f"Windows would not change its startup list. {exc}") from None
        return startup_json()

    @r.post("/api/quit")
    def stop_snapshots(request: Request):
        require_admin(request)
        if ctx.quit is None:
            raise HTTPException(409, "This copy is showing its own window. Close the window to stop Snapshots.")
        threading.Timer(0.5, ctx.quit).start()     # after this reply has gone out
        return {"stopping": True}

    def xmp_json() -> dict:
        return dict(worker.xmp.state, enabled=cfg.xmp_sidecars)

    def storage_json(template: str) -> dict:
        problem = storage.template_problem(template)
        return {"template": template, "problem": problem, "examples": [] if problem else storage.examples(template),
                "variables": storage.VARIABLES}

    @r.put("/api/settings/xmp")
    def save_xmp(body: XmpBody, request: Request):
        require_admin(request)
        save_settings({"xmp_sidecars": body.enabled}, cfg.config_file)
        turned_on = body.enabled and not cfg.xmp_sidecars
        cfg.xmp_sidecars = body.enabled
        if turned_on:
            with db.session(cfg.db_path) as conn:
                worker.xmp.mark_all(conn)    # every item is looked at once; most need no sidecar
            worker.xmp.state.update(written=0, failed=0, message=None)
            worker.xmp.retry_at = 0
            worker.poke()
        return xmp_json()

    @r.post("/api/settings/storage/preview")
    def preview_storage(body: StorageBody, request: Request):
        require_admin(request)
        return storage_json(body.template.strip())

    @r.put("/api/settings/storage")
    def save_storage(body: StorageBody, request: Request):
        require_admin(request)
        template = body.template.strip()
        problem = storage.template_problem(template)
        if problem:
            raise HTTPException(400, problem)
        save_settings({"upload_template": template}, cfg.config_file)
        cfg.upload_template = template
        return storage_json(template)

    def offsite_json() -> dict:
        job = worker.offsite
        return {"method": cfg.offsite_method, "target": cfg.offsite_target, "hours": cfg.offsite_hours,
                "problem": offsite.problem(cfg), "state": dict(job.state, running=job.running()),
                "tools": {"rclone": bool(offsite.shutil.which("rclone")), "restic": bool(offsite.shutil.which("restic"))}}

    # ------------------------------------------------------------ offsite copy of the photos

    @r.post("/api/offsite")
    def set_offsite(body: OffsiteBody, request: Request):
        """Where (and how often) the photos are copied. method "" turns it off."""
        require_admin(request)
        if body.method not in OFFSITE_METHODS:
            raise HTTPException(400, "Choose folder, rclone or restic.")
        target = body.target.strip().strip('"')
        why = offsite.problem(cfg, body.method, target)
        if why:
            raise HTTPException(400, why)
        cfg.offsite_method, cfg.offsite_target, cfg.offsite_hours = body.method, target, float(body.hours)
        cfg.config_file = save_settings({"offsite_method": cfg.offsite_method, "offsite_target": cfg.offsite_target,
                                         "offsite_hours": cfg.offsite_hours}, cfg.config_file)
        worker.offsite.retry_at = 0
        worker.poke()
        return offsite_json()

    @r.post("/api/offsite/run", status_code=202)
    def run_offsite(request: Request):
        require_admin(request)
        why = offsite.problem(cfg) or (None if worker.offsite.enabled() else "Choose where the copy goes first.")
        if why:
            raise HTTPException(400, why)
        worker.offsite.start()
        return offsite_json()

    @r.post("/api/offsite/stop")
    def stop_offsite(request: Request):
        require_admin(request)
        worker.offsite.stop()
        return offsite_json()

    # ------------------------------------------------------------ webhooks

    def webhook_json(row, conn) -> dict:
        waiting = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND delivered_at IS NULL"
                               " AND failed = 0", (row["id"],)).fetchone()[0]
        failed = conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE webhook_id = ? AND failed = 1",
                              (row["id"],)).fetchone()[0]
        return {"id": row["id"], "url": row["url"], "events": json.loads(row["events"]), "enabled": bool(row["enabled"]),
                "created_at": row["created_at"], "last_at": row["last_at"], "last_status": row["last_status"],
                "waiting": waiting, "failed": failed}

    def check_events(events: list[str]) -> list[str]:
        events = list(dict.fromkeys(events))
        unknown = [e for e in events if e not in webhooks.EVENTS and e != "*"]
        if unknown:
            raise HTTPException(400, f"Unknown event: {unknown[0]}")
        return events

    @r.get("/api/webhooks")
    def list_webhooks(request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT * FROM webhooks ORDER BY id").fetchall()
            return {"webhooks": [webhook_json(x, conn) for x in rows], "events": webhooks.EVENTS}

    @r.post("/api/webhooks", status_code=201)
    def add_webhook(body: WebhookBody, request: Request):
        """The secret is shown once: the receiver uses it to check the X-Snapshots-Signature header."""
        require_admin(request)
        url = body.url.strip()
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc:
            raise HTTPException(400, "The address should start with http:// or https://")
        events = check_events(body.events)
        secret = webhooks.new_secret()
        with db.session(cfg.db_path) as conn:
            hook_id = conn.execute("INSERT INTO webhooks (url, events, secret, created_at) VALUES (?, ?, ?, ?)",
                                   (url, json.dumps(events), secret, datetime.now().isoformat(timespec="seconds"))
                                   ).lastrowid
            out = webhook_json(conn.execute("SELECT * FROM webhooks WHERE id = ?", (hook_id,)).fetchone(), conn)
        return dict(out, secret=secret)

    @r.patch("/api/webhooks/{hook_id}")
    def change_webhook(hook_id: int, body: WebhookChange, request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            if conn.execute("SELECT 1 FROM webhooks WHERE id = ?", (hook_id,)).fetchone() is None:
                raise HTTPException(404, "No such webhook.")
            if body.enabled is not None:
                conn.execute("UPDATE webhooks SET enabled = ? WHERE id = ?", (int(body.enabled), hook_id))
            if body.events is not None:
                conn.execute("UPDATE webhooks SET events = ? WHERE id = ?", (json.dumps(check_events(body.events)), hook_id))
            return webhook_json(conn.execute("SELECT * FROM webhooks WHERE id = ?", (hook_id,)).fetchone(), conn)

    @r.delete("/api/webhooks/{hook_id}")
    def delete_webhook(hook_id: int, request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            if not conn.execute("DELETE FROM webhooks WHERE id = ?", (hook_id,)).rowcount:
                raise HTTPException(404, "No such webhook.")
        return {"ok": True}

    @r.post("/api/webhooks/{hook_id}/test")
    def test_webhook(hook_id: int, request: Request):
        """Send a "test" event now."""
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            hook = conn.execute("SELECT enabled FROM webhooks WHERE id = ?", (hook_id,)).fetchone()
            if hook is None:
                raise HTTPException(404, "No such webhook.")
            if not hook["enabled"]:
                raise HTTPException(400, "Turn the webhook on first.")
            webhooks.emit(conn, "test", {"message": "This is a test from Snapshots."}, only=hook_id)
        return {"ok": True}

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
