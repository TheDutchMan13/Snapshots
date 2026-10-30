"""Favourites (per person), albums (shared by everyone) and the trash."""

import os
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from . import db, linking
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


class AlbumRename(BaseModel):
    name: str = Field(min_length=1, max_length=200)


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

    def album_json(conn, row) -> dict:
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MIN(a.taken_at) AS first, MAX(a.taken_at) AS last, MAX(a.taken_at || '|' || a.id) AS cover"
            " FROM album_assets aa JOIN assets a ON a.id = aa.asset_id AND a.trashed_at IS NULL"
            f" AND {db.NOT_LOCKED} WHERE aa.album_id = ?",
            (row["id"],),
        ).fetchone()
        cover = int(stats["cover"].rpartition("|")[2]) if stats["cover"] else None
        comments = conn.execute("SELECT COUNT(*) FROM comments WHERE album_id = ?", (row["id"],)).fetchone()[0]
        return {
            "id": row["id"], "name": row["name"], "count": stats["n"], "comments": comments,
            "first": stats["first"], "last": stats["last"],
            "cover": cover, "cover_v": _covers(conn, [cover]).get(cover),
            "created_at": row["created_at"], "updated_at": row["updated_at"],
        }

    def get_album(conn, album_id: int):
        row = conn.execute("SELECT * FROM albums WHERE id = ?", (album_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such album.")
        return row

    @r.get("/api/albums")
    def list_albums(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT * FROM albums ORDER BY updated_at DESC, id DESC").fetchall()
            return {"albums": [album_json(conn, row) for row in rows]}

    @r.post("/api/albums", status_code=201)
    def create_album(body: AlbumBody, request: Request):
        user = current_user(request)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Albums need a name.")
        with db.session(cfg.db_path) as conn:
            album_id = conn.execute(
                "INSERT INTO albums (name, created_by, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, user["id"], _now(), _now()),
            ).lastrowid
            _add(conn, album_id, body.ids, user["id"])
            return album_json(conn, get_album(conn, album_id))

    @r.get("/api/albums/{album_id}")
    def album(album_id: int, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            return album_json(conn, get_album(conn, album_id))

    @r.patch("/api/albums/{album_id}")
    def rename_album(album_id: int, body: AlbumRename, request: Request):
        current_user(request)
        name = body.name.strip()
        if not name:
            raise HTTPException(400, "Albums need a name.")
        with db.session(cfg.db_path) as conn:
            get_album(conn, album_id)
            conn.execute("UPDATE albums SET name = ?, updated_at = ? WHERE id = ?", (name, _now(), album_id))
            return album_json(conn, get_album(conn, album_id))

    @r.delete("/api/albums/{album_id}")
    def delete_album(album_id: int, request: Request):
        """Deletes the album only. The photos in it stay in the library."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_album(conn, album_id)
            conn.execute("DELETE FROM albums WHERE id = ?", (album_id,))
        return {"ok": True}

    def _add(conn, album_id: int, ids, user_id: int | None = None) -> None:
        conn.executemany(
            "INSERT OR IGNORE INTO album_assets (album_id, asset_id, added_at, added_by)"
            " SELECT ?, id, ?, ? FROM assets WHERE id = ?",
            [(album_id, _now(), user_id, i) for i in db.open_ids(conn, ids)],
        )
        conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (_now(), album_id))

    @r.post("/api/albums/{album_id}/add")
    def add_to_album(album_id: int, body: Ids, request: Request):
        user = current_user(request)
        with db.session(cfg.db_path) as conn:
            get_album(conn, album_id)
            _add(conn, album_id, body.ids, user["id"])
            return album_json(conn, get_album(conn, album_id))

    @r.post("/api/albums/{album_id}/remove")
    def remove_from_album(album_id: int, body: Ids, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_album(conn, album_id)
            conn.executemany("DELETE FROM album_assets WHERE album_id = ? AND asset_id = ?",
                             [(album_id, i) for i in body.ids])
            conn.execute("UPDATE albums SET updated_at = ? WHERE id = ?", (_now(), album_id))
            return album_json(conn, get_album(conn, album_id))

    return r
