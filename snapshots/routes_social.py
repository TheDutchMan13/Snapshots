"""Comments on photos and albums, and the Activity page (what others did lately)."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, webhooks
from .app import current_user
from .routes_library import can_see, unread_activity

MAX_COMMENT = 2000


class CommentBody(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_COMMENT)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _comment_json(row, me: dict) -> dict:
    return {
        "id": row["id"], "text": row["text"], "created_at": row["created_at"],
        "user": row["username"] or "Deleted account", "user_id": row["user_id"],
        "mine": row["user_id"] == me["id"],
        "can_delete": row["user_id"] == me["id"] or me["is_admin"],
    }


_COMMENTS = ("SELECT c.id, c.text, c.created_at, c.user_id, u.username FROM comments c"
             " LEFT JOIN users u ON u.id = c.user_id")


def router(ctx) -> APIRouter:
    cfg = ctx.cfg
    r = APIRouter()

    def visible_asset(conn, asset_id: int, user):
        row = conn.execute("SELECT id, locked_by FROM assets WHERE id = ?", (asset_id,)).fetchone()
        if row is None or not can_see(row, user):
            raise HTTPException(404, "Not found")
        return row

    def album_exists(conn, album_id: int):
        if conn.execute("SELECT 1 FROM albums WHERE id = ?", (album_id,)).fetchone() is None:
            raise HTTPException(404, "No such album.")

    def clean(text: str) -> str:
        text = text.strip()
        if not text:
            raise HTTPException(400, "Write something first.")
        return text

    # ------------------------------------------------------------ comments

    @r.get("/api/assets/{asset_id}/comments")
    def asset_comments(asset_id: int, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            visible_asset(conn, asset_id, user)
            rows = conn.execute(_COMMENTS + " WHERE c.asset_id = ? ORDER BY c.created_at, c.id", (asset_id,))
            return {"comments": [_comment_json(x, user) for x in rows]}

    @r.post("/api/assets/{asset_id}/comments", status_code=201)
    def add_asset_comment(asset_id: int, body: CommentBody, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            row = visible_asset(conn, asset_id, user)
            if row["locked_by"] is not None:
                raise HTTPException(403, "Items in a locked folder can't have comments.")
            cid = conn.execute("INSERT INTO comments (asset_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                               (asset_id, user["id"], clean(body.text), _now())).lastrowid
            out = _comment_json(conn.execute(_COMMENTS + " WHERE c.id = ?", (cid,)).fetchone(), user)
            webhooks.emit(conn, "comment.added", {"id": cid, "text": out["text"], "by": out["user"],
                                                  "asset_id": asset_id, "album": None})
            return out

    @r.get("/api/albums/{album_id}/comments")
    def album_comments(album_id: int, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            album_exists(conn, album_id)
            rows = conn.execute(_COMMENTS + " WHERE c.album_id = ? ORDER BY c.created_at, c.id", (album_id,))
            return {"comments": [_comment_json(x, user) for x in rows]}

    @r.post("/api/albums/{album_id}/comments", status_code=201)
    def add_album_comment(album_id: int, body: CommentBody, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            album_exists(conn, album_id)
            cid = conn.execute("INSERT INTO comments (album_id, user_id, text, created_at) VALUES (?, ?, ?, ?)",
                               (album_id, user["id"], clean(body.text), _now())).lastrowid
            out = _comment_json(conn.execute(_COMMENTS + " WHERE c.id = ?", (cid,)).fetchone(), user)
            name = conn.execute("SELECT name FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
            webhooks.emit(conn, "comment.added", {"id": cid, "text": out["text"], "by": out["user"],
                                                  "asset_id": None, "album": {"id": album_id, "name": name}})
            return out

    @r.delete("/api/comments/{comment_id}")
    def delete_comment(comment_id: int, request: Request):
        """People can delete their own comments; administrators can delete any."""
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT c.user_id FROM comments c LEFT JOIN assets a ON a.id = c.asset_id"
                               " WHERE c.id = ? AND (c.asset_id IS NULL OR a.locked_by IS NULL)", (comment_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "No such comment.")
            if row["user_id"] != user["id"] and not user["is_admin"]:
                raise HTTPException(403, "Only the person who wrote it, or an administrator, can delete it.")
            conn.execute("DELETE FROM comments WHERE id = ?", (comment_id,))
        return {"ok": True}

    # ------------------------------------------------------------ activity

    @r.get("/api/activity")
    def activity(request: Request, before: str | None = Query(None, max_length=40),
                 limit: int = Query(50, ge=1, le=200)):
        """Newest first: comments, and photos added to albums (grouped per person, album and hour)."""
        user = current_user(request)
        cutoff = before or "9999"
        with db.session(cfg.db_path) as conn:
            seen = conn.execute("SELECT activity_seen_at FROM users WHERE id = ?", (user["id"],)).fetchone()[0] or ""
            items = []
            for c in conn.execute(
                "SELECT c.id, c.text, c.created_at, c.user_id, u.username, c.asset_id, c.album_id, al.name AS album,"
                f" a.filename, {db.version_sql()} AS v, a.trashed_at"
                " FROM comments c LEFT JOIN users u ON u.id = c.user_id LEFT JOIN albums al ON al.id = c.album_id"
                " LEFT JOIN assets a ON a.id = c.asset_id"
                " WHERE c.created_at < ? AND (c.asset_id IS NULL OR a.locked_by IS NULL)"
                " ORDER BY c.created_at DESC, c.id DESC LIMIT ?", (cutoff, limit)):
                items.append({
                    "kind": "comment", "at": c["created_at"], "user": c["username"] or "Deleted account",
                    "mine": c["user_id"] == user["id"], "text": c["text"], "comment_id": c["id"],
                    "asset": {"id": c["asset_id"], "v": c["v"], "filename": c["filename"],
                              "trashed": c["trashed_at"] is not None} if c["asset_id"] else None,
                    "album": {"id": c["album_id"], "name": c["album"]} if c["album_id"] else None,
                })
            for g in conn.execute(
                "SELECT aa.album_id, al.name, aa.added_by, u.username, substr(aa.added_at, 1, 13) AS hour,"
                " MAX(aa.added_at) AS at, COUNT(*) AS n, MAX(a.taken_at || '|' || a.id) AS cover"
                " FROM album_assets aa JOIN albums al ON al.id = aa.album_id"
                " LEFT JOIN users u ON u.id = aa.added_by"
                " JOIN assets a ON a.id = aa.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
                " WHERE aa.added_at < ? AND aa.added_by IS NOT NULL"
                " GROUP BY aa.album_id, aa.added_by, hour ORDER BY at DESC LIMIT ?", (cutoff, limit)):
                cover = int(g["cover"].rpartition("|")[2])
                items.append({
                    "kind": "added", "at": g["at"], "user": g["username"] or "Deleted account",
                    "mine": g["added_by"] == user["id"], "count": g["n"],
                    "album": {"id": g["album_id"], "name": g["name"]}, "cover": cover,
                })
            versions = db.versions(conn, [i["cover"] for i in items if i["kind"] == "added"])
            for i in items:
                if i["kind"] == "added":
                    i["cover_v"] = versions.get(i["cover"])
                i["new"] = i["at"] > seen and not i["mine"]
        items.sort(key=lambda i: i["at"], reverse=True)
        items = items[:limit]
        return {"items": items, "next": items[-1]["at"] if len(items) == limit else None}

    @r.post("/api/activity/seen")
    def activity_seen(request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE users SET activity_seen_at = ? WHERE id = ?", (_now(), user["id"]))
            return {"unread": unread_activity(conn, user["id"])}

    return r
