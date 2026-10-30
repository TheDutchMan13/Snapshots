"""Favourites (per person), albums (shared by everyone) and the trash."""

import json
import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, linking, smart, webhooks
from .app import current_user, require_admin
from .routes_library import library_root, original_path
from .scanner import delete_asset_row


class Ids(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=20000)


class FavoriteBody(Ids):
    value: bool = True


class EmptyTrash(BaseModel):
    ids: list[int] | None = Field(default=None, max_length=20000)  # None = everything in the trash


class AlbumBody(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    ids: list[int] = Field(default_factory=list, max_length=20000)
    rule: dict | None = None       # a smart album: its items are found by this rule


class AlbumRename(BaseModel):
    name: str | None = Field(None, min_length=1, max_length=200)
    rule: dict | None = None       # change a smart album's rule


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_covers = db.versions  # cache key for each cover photo, so its thumbnail URL is right


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()

    # ------------------------------------------------------------ favourites

    @r.post("/api/favorites")
    def set_favorite(body: FavoriteBody, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            if body.value:
                conn.executemany(
                    "INSERT OR IGNORE INTO favorites (user_id, asset_id, created_at)"
                    " SELECT ?, id, ? FROM assets WHERE id = ?",
                    [(user["id"], _now(), i) for i in db.open_ids(conn, body.ids)],
                )
            else:
                conn.executemany("DELETE FROM favorites WHERE user_id = ? AND asset_id = ?",
                                 [(user["id"], i) for i in body.ids])
        return {"ok": True}

    # ------------------------------------------------------------ trash

    @r.post("/api/trash")
    def move_to_trash(body: Ids, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            conn.executemany("UPDATE assets SET trashed_at = ? WHERE id = ? AND trashed_at IS NULL",
                             [(_now(), i) for i in linking.with_clips(conn, db.open_ids(conn, body.ids))])
        return {"ok": True}

    @r.post("/api/trash/restore")
    def restore(body: Ids, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            conn.executemany("UPDATE assets SET trashed_at = NULL WHERE id = ?",
                             [(i,) for i in linking.with_clips(conn, db.open_ids(conn, body.ids))])
        worker.poke()  # restored photos may still need their faces found
        return {"ok": True}

    @r.post("/api/trash/empty")
    def empty_trash(body: EmptyTrash, request: Request):
        """Deletes the files from disk. Administrators only, and only items already in the trash."""
        require_admin(request)
        library_root(cfg)
        deleted, failed = 0, []
        with db.session(cfg.db_path) as conn:
            if body.ids is None:
                rows = conn.execute("SELECT id, path, filename FROM assets"
                                    " WHERE trashed_at IS NOT NULL AND locked_by IS NULL").fetchall()
            else:
                ids = linking.with_clips(conn, db.open_ids(conn, body.ids))
                marks = ",".join("?" * len(ids)) or "NULL"
                rows = conn.execute(
                    f"SELECT id, path, filename FROM assets WHERE trashed_at IS NOT NULL AND id IN ({marks})",
                    ids,
                ).fetchall()
            for row in rows:
                try:
                    full = original_path(cfg, row["path"])
                except HTTPException as exc:  # its folder isn't connected (or no longer in the library)
                    failed.append({"id": row["id"], "filename": row["filename"], "reason": exc.detail})
                    continue
                try:
                    if full.exists():
                        os.remove(full)
                except OSError as exc:
                    failed.append({"id": row["id"], "filename": row["filename"], "reason": exc.strerror or str(exc)})
                    continue
                delete_asset_row(cfg, conn, row["id"])
                deleted += 1
            linking.tidy_stacks(conn)
        return {"deleted": deleted, "failed": failed}

    # ------------------------------------------------------------ albums

    def album_json(conn, row, user_id: int | None = None) -> dict:
        rule = smart.load(row["rule"])
        waiting = False
        if rule:
            f, waiting = smart.resolve(conn, cfg, worker, db.Filter(album=row["id"]))
            where, params = db.where_clause(f, user_id)
            stats = conn.execute(
                "SELECT COUNT(*) AS n, MIN(a.taken_at) AS first, MAX(a.taken_at) AS last,"
                f" MAX(a.taken_at || '|' || a.id) AS cover FROM assets a WHERE {where}", params).fetchone()
        else:
            stats = conn.execute(
                "SELECT COUNT(*) AS n, MIN(a.taken_at) AS first, MAX(a.taken_at) AS last,"
                " MAX(a.taken_at || '|' || a.id) AS cover"
                " FROM album_assets aa JOIN assets a ON a.id = aa.asset_id AND a.trashed_at IS NULL"
                f" AND {db.NOT_LOCKED} WHERE aa.album_id = ?",
                (row["id"],),
            ).fetchone()
        cover = int(stats["cover"].rpartition("|")[2]) if stats["cover"] else None
        comments = conn.execute("SELECT COUNT(*) FROM comments WHERE album_id = ?", (row["id"],)).fetchone()[0]
        out = {
            "id": row["id"], "name": row["name"], "count": stats["n"], "comments": comments,
            "first": stats["first"], "last": stats["last"],
            "cover": cover, "cover_v": _covers(conn, [cover]).get(cover),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
            "smart": rule is not None,
        }
        if rule:
            out["rule"] = rule
            out["rule_text"] = describe(conn, rule)
            out["waiting"] = waiting
        return out

    def describe(conn, rule: dict) -> str:
        """The rule in words, for the album's page."""
        parts = []
        if rule.get("people"):
            names = [r[0] or "someone unnamed" for r in conn.execute(
                f"SELECT name FROM people WHERE id IN ({','.join('?' * len(rule['people']))})", rule["people"])]
            parts.append(" and ".join(names) if names else "people no longer found")
        if rule.get("look"):
            parts.append(f"showing “{rule['look']}”")
        if rule.get("words"):
            parts.append(f"matching “{rule['words']}”")
        if rule.get("tag") is not None:
            tag = conn.execute("SELECT name FROM tags WHERE id = ?", (rule["tag"],)).fetchone()
            parts.append(f"tagged “{tag[0]}”" if tag else "a tag that no longer exists")
        if rule.get("kind"):
            parts.append("videos only" if rule["kind"] == "video" else "photos only")
        if rule.get("start") and rule.get("end"):
            parts.append(f"from {rule['start']} to {rule['end']}")
        elif rule.get("start"):
            parts.append(f"from {rule['start']}")
        elif rule.get("end"):
            parts.append(f"until {rule['end']}")
        if rule.get("country"):
            parts.append("in " + ", ".join(x for x in (rule.get("city"), rule.get("state"), rule["country"]) if x))
        if rule.get("favorite"):
            parts.append("your favourites")
        if rule.get("hide_archived"):
            parts.append("not archived")
        return "; ".join(parts)

    def check_rule(conn, value) -> str:
        try:
            rule = smart.parse_rule(value)
        except (ValueError, TypeError) as exc:
            raise HTTPException(400, str(exc)) from None
        return json.dumps(rule, ensure_ascii=False)

    def get_album(conn, album_id: int):
        row = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such album.")
        return row

    @r.get("/api/albums")
    def list_albums(request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT * FROM albums ORDER BY updated_at DESC, id DESC").fetchall()
            return {"albums": [album_json(conn, row, user["id"]) for row in rows]}

    @r.post("/api/albums", status_code=201)
    def create_album(body: AlbumBody, request: Request):
        user = current_user(request)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Albums need a name.")
        with db.session(cfg.db_path) as conn:
            rule = check_rule(conn, body.rule) if body.rule is not None else None
            album_id = conn.execute(
                "INSERT INTO albums (name, created_by, created_at, updated_at, rule) VALUES (?, ?, ?, ?, ?)",
                (name, user["id"], _now(), _now(), rule),
            ).lastrowid
            if rule is None:
                _add(conn, album_id, body.ids, user["id"])
            return album_json(conn, get_album(conn, album_id), user["id"])

    @r.get("/api/albums/{album_id}")
    def album(album_id: int, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            return album_json(conn, get_album(conn, album_id), user["id"])

    @r.patch("/api/albums/{album_id}")
    def rename_album(album_id: int, body: AlbumRename, request: Request):
        """Rename an album, or change a smart album's rule."""
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            album = get_album(conn, album_id)
            if body.name is not None:
                name = body.name.strip()
                if not name:
                    raise HTTPException(400, "Albums need a name.")
                conn.execute("UPDATE albums SET name = ?, updated_at = ? WHERE id = ?", (name, _now(), album_id))
            if body.rule is not None:
                if not album["rule"]:
                    raise HTTPException(400, "This is not a smart album.")
                conn.execute("UPDATE albums SET rule = ?, updated_at = ? WHERE id = ?",
                             (check_rule(conn, body.rule), _now(), album_id))
            return album_json(conn, get_album(conn, album_id), user["id"])

    @r.delete("/api/albums/{album_id}")
    def delete_album(album_id: int, request: Request):
        """Deletes the album only. The photos in it stay in the library."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_album(conn, album_id)
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        return {"ok": True}

    def _add(conn, album_id: int, ids, user_id: int | None = None) -> None:
        ids = db.open_ids(conn, ids)
        before = conn.total_changes
        conn.executemany(
            "INSERT OR IGNORE INTO album_assets (album_id, asset_id, added_at, added_by)"
            " SELECT ?, id, ?, ? FROM assets WHERE id = ?",
            [(album_id, _now(), user_id, i) for i in ids],
        )
        added = conn.total_changes - before
        conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (_now(), album_id))
        if added:
            name = conn.execute("SELECT name FROM albums WHERE id = ?", (album_id,)).fetchone()[0]
            by = conn.execute("SELECT username FROM users WHERE id = ?", (user_id,)).fetchone() if user_id else None
            webhooks.emit(conn, "album.added", {"album": {"id": album_id, "name": name}, "count": added,
                                                "ids": ids[:webhooks.MAX_ITEMS], "by": by[0] if by else None})

    def not_smart(album) -> None:
        if album["rule"]:
            raise HTTPException(400, "A smart album finds its photos by its rules: change the rules instead.")

    @r.post("/api/albums/{album_id}/add")
    def add_to_album(album_id: int, body: Ids, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            not_smart(get_album(conn, album_id))
            _add(conn, album_id, body.ids, user["id"])
            return album_json(conn, get_album(conn, album_id), user["id"])

    @r.post("/api/albums/{album_id}/remove")
    def remove_from_album(album_id: int, body: Ids, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            not_smart(get_album(conn, album_id))
            conn.executemany("DELETE FROM album_assets WHERE album_id = ? AND asset_id = ?",
                             [(album_id, i) for i in db.open_ids(conn, body.ids)])
            conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (_now(), album_id))
            return album_json(conn, get_album(conn, album_id), user["id"])

    return r
