"""Events, and the Clean up page (copies, bursts, blurry and dark photos)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import analysis, db
from .app import current_user
from .routes_library import asset_dict


class Ids(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class KeepBody(BaseModel):
    keep: list[int] = Field(min_length=1, max_length=500)
    trash: list[int] = Field(default_factory=list, max_length=500)


def _rows_by_id(conn, ids: list[int], user_id: int) -> dict:
    f = db.Filter(ids=ids)
    rows, _ = db.list_assets(conn, f, user_id, "", max(1, len(ids)))
    return {row["id"]: row for row in rows}


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    duplicates = analysis.DuplicateFinder(cfg)
    events = analysis.EventFinder(cfg)
    ctx.duplicates, ctx.events = duplicates, events
    r = APIRouter()

    # ------------------------------------------------------------ clean up

    @r.get("/api/cleanup")
    def cleanup_summary(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            groups = duplicates.groups(conn) if cfg.photo_checks else []
            low = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE a.trashed_at IS NULL AND {db.NOT_LOCKED}"
                f" AND {analysis.low_quality_sql(cfg)}"
            ).fetchone()[0] if cfg.photo_checks else 0
        return {
            "enabled": cfg.photo_checks,
            "groups": len(groups),
            "copies": sum(1 for g in groups if g["kind"] == "copies"),
            "bursts": sum(1 for g in groups if g["kind"] == "burst"),
            "extra": sum(len(g["ids"]) - 1 for g in groups),
            "low_quality": low,
            "status": worker.status()["checks"],
        }

    @r.get("/api/cleanup/groups")
    def cleanup_groups(request: Request, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100)):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            groups = duplicates.groups(conn)
            page = groups[offset: offset + limit]
            rows = _rows_by_id(conn, [i for g in page for i in g["ids"]], user["id"])
        out = []
        for g in page:
            items = [asset_dict(rows[i]) for i in g["ids"] if i in rows]
            if len(items) < 2:
                continue
            for it in items:
                it["sharpness"] = rows[it["id"]]["sharpness"]
            out.append({"kind": g["kind"], "keep": g["keep"], "items": items})
        return {"groups": out, "total": len(groups), "next": offset + limit if offset + limit < len(groups) else None}

    @r.post("/api/cleanup/resolve")
    def resolve(body: KeepBody, request: Request):
        """Keep some photos of a group and move the others to the trash (nothing is deleted)."""
        current_user(request)
        if set(body.keep) & set(body.trash):
            raise HTTPException(400, "A photo can't be both kept and moved to the trash.")
        with db.session(cfg.db_path) as conn:
            trash = db.open_ids(conn, body.trash)
            if trash:
                stamp = datetime.now().isoformat(timespec="seconds")
                conn.executemany("UPDATE assets SET trashed_at = ? WHERE id = ? AND trashed_at IS NULL",
                                 [(stamp, i) for i in trash])
            keep = db.open_ids(conn, body.keep)
            if len(keep) > 1:
                analysis.record_not_duplicates(conn, keep)  # kept together: not copies after all
        return {"trashed": len(trash)}

    @r.post("/api/cleanup/not-duplicates")
    def not_duplicates(body: Ids, request: Request):
        current_user(request)
        if len(body.ids) < 2 or len(body.ids) > 500:
            raise HTTPException(400, "Choose between 2 and 500 photos.")
        with db.session(cfg.db_path) as conn:
            analysis.record_not_duplicates(conn, db.open_ids(conn, body.ids))
        return {"ok": True}

    @r.post("/api/cleanup/fine")
    def looks_fine(body: Ids, request: Request):
        """'These blurry or dark photos are fine': stop suggesting them."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
            marks = ",".join("?" * len(ids)) or "NULL"
            n = conn.execute(f"UPDATE assets SET keep_quality = 1 WHERE id IN ({marks})", ids).rowcount
        return {"updated": n}

    # ------------------------------------------------------------ events

    @r.get("/api/events")
    def list_events(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            found = events.events(conn)
            versions = db.versions(conn, [e["cover"] for e in found])
        return {"events": [dict(e, cover_v=versions.get(e["cover"])) for e in found]}

    return r
