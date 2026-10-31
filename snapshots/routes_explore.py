"""Events, the Clean up page (copies, bursts, blurry and dark photos), screenshots and documents, and
suggested albums."""

import threading
import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import analysis, db, smart, sorting
from .app import current_user
from .routes_library import asset_dict


class Ids(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


class CategoryBody(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)
    category: str = Field(pattern="^(photo|screenshot|document|receipt)$")


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

    def pick(groups, kind):
        return [g for g in groups if g["kind"] == kind] if kind else groups

    @r.get("/api/cleanup")
    def cleanup_summary(request: Request, strictness: str = Query("normal", pattern="^(strict|normal|loose)$")):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            groups = duplicates.groups(conn, strictness) if cfg.photo_checks else []
            low = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE a.trashed_at IS NULL AND {db.NOT_LOCKED}"
                f" AND {analysis.low_quality_sql(cfg)}"
            ).fetchone()[0] if cfg.photo_checks else 0
        kinds = {}
        for k in ("identical", "copies", "burst"):
            gs = pick(groups, k)
            kinds[k] = {"groups": len(gs), "extra": sum(len(g["ids"]) - 1 for g in gs),
                        "space": sum(g["space"] for g in gs)}
        return {
            "enabled": cfg.photo_checks,
            "strictness": strictness,
            "groups": len(groups),
            "copies": kinds["copies"]["groups"],
            "bursts": kinds["burst"]["groups"],
            "identical": kinds["identical"]["groups"],
            "kinds": kinds,
            "extra": sum(len(g["ids"]) - 1 for g in groups),
            "space": sum(g["space"] for g in groups),
            "low_quality": low,
            "status": worker.status()["checks"],
        }

    @r.get("/api/cleanup/groups")
    def cleanup_groups(request: Request, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
                       strictness: str = Query("normal", pattern="^(strict|normal|loose)$"),
                       kind: str | None = Query(None, pattern="^(identical|copies|burst)$")):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            groups = pick(duplicates.groups(conn, strictness), kind)
            page = groups[offset: offset + limit]
            rows = _rows_by_id(conn, [i for g in page for i in g["ids"]], user["id"])
        out = []
        for g in page:
            items = [asset_dict(rows[i]) for i in g["ids"] if i in rows]
            if len(items) < 2:
                continue
            for it in items:
                it["sharpness"] = rows[it["id"]]["sharpness"]
            out.append({"kind": g["kind"], "keep": g["keep"], "why": g["why"], "space": g["space"], "items": items})
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

    # ------------------------------------------------------------ screenshots, documents, receipts

    @r.get("/api/sorting")
    def sorting_summary(request: Request):
        """How many screenshots, documents and receipts there are, and how many highlights."""
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            counts = dict(conn.execute(
                f"SELECT a.category, COUNT(*) FROM assets a WHERE {db.CLUTTER} AND a.trashed_at IS NULL"
                f" AND {db.NOT_LOCKED} AND {db.LIVE_PART_HIDDEN} GROUP BY a.category").fetchall())
            where, params = db.where_clause(db.Filter(highlights=True, archived="hide", stacks=True), user["id"])
            highlights = conn.execute(f"SELECT COUNT(*) FROM assets a WHERE {where}", params).fetchone()[0]
            covers = {}
            for cat in sorting.CATEGORIES:
                row = conn.execute(
                    f"SELECT a.id FROM assets a WHERE a.category = ? AND a.trashed_at IS NULL AND {db.NOT_LOCKED}"
                    " ORDER BY a.taken_at DESC LIMIT 1", (cat,)).fetchone()
                covers[cat] = row[0] if row else None
            versions = db.versions(conn, [c for c in covers.values() if c])
            pending = worker.sort.pending(conn)
        return {
            "categories": [{"category": c, "count": counts.get(c, 0), "cover": covers[c],
                            "cover_v": versions.get(covers[c])} for c in sorting.CATEGORIES],
            "highlights": highlights, "pending": pending, "hide_clutter": cfg.hide_clutter,
            "content": cfg.smart_search,
        }

    @r.post("/api/assets/category")
    def set_category(body: CategoryBody, request: Request):
        """'This is a screenshot' or 'this is a photo': set by hand, so sorting leaves it as it is."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
            for start in range(0, len(ids), 900):
                chunk = ids[start:start + 900]
                # A screenshot or document has no place in Highlights.
                conn.execute(f"UPDATE assets SET category = ?, category_hand = 1,"
                             f" beauty = CASE WHEN ? = 'photo' THEN beauty END"
                             f" WHERE id IN ({','.join('?' * len(chunk))})", [body.category, body.category] + chunk)
        return {"updated": len(ids)}

    # ------------------------------------------------------------ suggested albums

    suggest_cache = {"key": None, "at": 0.0, "items": []}
    suggest_lock = threading.Lock()

    @r.get("/api/suggestions/albums")
    def suggested_albums(request: Request):
        """Smart albums worth making: things many photos show (dogs, sunsets, food...) with no album yet."""
        current_user(request)
        if not worker.clip.ready():
            return {"suggestions": [], "ready": False}
        with db.session(cfg.db_path) as conn:
            key = worker.clip.index._version(conn)
            with suggest_lock:
                fresh = suggest_cache["key"] == key and time.time() - suggest_cache["at"] < 600
                if not fresh:
                    suggest_cache.update(key=key, at=time.time(), items=sorting.suggest_albums(conn, worker.clip))
                items = list(suggest_cache["items"])
            taken = set()
            for name, rule in conn.execute("SELECT name, rule FROM albums"):
                taken.add(name.strip().lower())
                if rule:
                    look = (smart.load(rule) or {}).get("look")
                    if look:
                        taken.add(look.strip().lower())
            items = [x for x in items if x["name"].lower() not in taken and x["look"].lower() not in taken][:8]
            versions = db.versions(conn, [x["cover"] for x in items])
        return {"suggestions": [dict(x, cover_v=versions.get(x["cover"])) for x in items], "ready": True}

    return r
