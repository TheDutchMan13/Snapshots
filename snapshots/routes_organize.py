"""Archive, tags, stacks, folders, memories ("on this day") and the map."""

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import db, linking
from .app import current_user


class Ids(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=20000)


class ArchiveBody(Ids):
    value: bool = True


class TagBody(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    ids: list[int] = Field(default_factory=list, max_length=20000)


class TagRename(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class StackBody(BaseModel):
    ids: list[int] = Field(min_length=2, max_length=500)
    cover: int | None = None


class CoverBody(BaseModel):
    cover: int


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _versions(conn, ids) -> dict:
    ids = [i for i in ids if i]
    out = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        out.update({r[0]: r[1][:8] for r in conn.execute(
            f"SELECT id, sha1 FROM assets WHERE id IN ({','.join('?' * len(chunk))})", chunk)})
    return out


# Shown in memories, folders and the map: everything in the library that isn't hidden.
VISIBLE = f"a.trashed_at IS NULL AND a.archived_at IS NULL AND {db.LIVE_PART_HIDDEN}"


def router(ctx) -> APIRouter:
    cfg = ctx.cfg
    r = APIRouter()

    # ------------------------------------------------------------ archive

    @r.post("/api/archive")
    def archive(body: ArchiveBody, request: Request):
        """Archived items leave the timelines (All, Photos, Videos, memories, the map) but stay everywhere else."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = linking.with_clips(conn, body.ids)
            stamp = _now() if body.value else None
            conn.executemany(
                "UPDATE assets SET archived_at = ? WHERE id = ? AND (archived_at IS NULL) = ?",
                [(stamp, i, 1 if body.value else 0) for i in ids])
        return {"ok": True}

    # ------------------------------------------------------------ tags

    def tag_json(conn, row) -> dict:
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MAX(a.taken_at || '|' || a.id) AS cover FROM asset_tags at"
            " JOIN assets a ON a.id = at.asset_id AND a.trashed_at IS NULL WHERE at.tag_id = ?", (row["id"],)
        ).fetchone()
        cover = int(stats["cover"].rpartition("|")[2]) if stats["cover"] else None
        return {"id": row["id"], "name": row["name"], "count": stats["n"], "cover": cover,
                "cover_v": _versions(conn, [cover]).get(cover)}

    def get_tag(conn, tag_id: int):
        row = conn.execute("SELECT id, name FROM tags WHERE id = ?", (tag_id,)).fetchone()
        if row is None:
            raise HTTPException(404, "No such tag.")
        return row

    def tag_assets(conn, tag_id: int, ids) -> None:
        conn.executemany("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id) SELECT id, ? FROM assets WHERE id = ?",
                         [(tag_id, i) for i in ids])

    @r.get("/api/tags")
    def list_tags(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT id, name FROM tags ORDER BY name COLLATE NOCASE").fetchall()
            return {"tags": [tag_json(conn, row) for row in rows]}

    @r.post("/api/tags", status_code=201)
    def create_tag(body: TagBody, request: Request):
        """Tag photos with a name, making the tag if it doesn't exist yet."""
        current_user(request)
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(400, "Tags need a name.")
        with db.session(cfg.db_path) as conn:
            conn.execute("INSERT OR IGNORE INTO tags (name, created_at) VALUES (?, ?)", (name, _now()))
            row = conn.execute("SELECT id, name FROM tags WHERE name = ?", (name,)).fetchone()
            tag_assets(conn, row["id"], body.ids)
            return tag_json(conn, row)

    @r.get("/api/tags/{tag_id}")
    def tag(tag_id: int, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            return tag_json(conn, get_tag(conn, tag_id))

    @r.patch("/api/tags/{tag_id}")
    def rename_tag(tag_id: int, body: TagRename, request: Request):
        """Rename a tag. Renaming it to another tag's name puts the two together."""
        current_user(request)
        name = " ".join(body.name.split())
        if not name:
            raise HTTPException(400, "Tags need a name.")
        with db.session(cfg.db_path) as conn:
            get_tag(conn, tag_id)
            other = conn.execute("SELECT id FROM tags WHERE name = ? AND id != ?", (name, tag_id)).fetchone()
            if other:
                conn.execute("INSERT OR IGNORE INTO asset_tags (asset_id, tag_id)"
                             " SELECT asset_id, ? FROM asset_tags WHERE tag_id = ?", (other["id"], tag_id))
                conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
                return tag_json(conn, get_tag(conn, other["id"]))
            conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name, tag_id))
            return tag_json(conn, get_tag(conn, tag_id))

    @r.delete("/api/tags/{tag_id}")
    def delete_tag(tag_id: int, request: Request):
        """Deletes the tag only. The photos stay in the library."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_tag(conn, tag_id)
            conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
        return {"ok": True}

    @r.post("/api/tags/{tag_id}/add")
    def add_tag(tag_id: int, body: Ids, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_tag(conn, tag_id)
            tag_assets(conn, tag_id, body.ids)
            return tag_json(conn, get_tag(conn, tag_id))

    @r.post("/api/tags/{tag_id}/remove")
    def remove_tag(tag_id: int, body: Ids, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_tag(conn, tag_id)
            conn.executemany("DELETE FROM asset_tags WHERE tag_id = ? AND asset_id = ?",
                             [(tag_id, i) for i in body.ids])
            return tag_json(conn, get_tag(conn, tag_id))

    # ------------------------------------------------------------ stacks

    @r.post("/api/stacks", status_code=201)
    def create_stack(body: StackBody, request: Request):
        """Put photos in a stack: one tile in the timeline, the others behind it."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = [r[0] for r in conn.execute(
                f"SELECT id FROM assets WHERE id IN ({','.join('?' * len(body.ids))})", body.ids)]
            if len(ids) < 2:
                raise HTTPException(400, "A stack needs at least two photos.")
            order = {i: n for n, i in enumerate(body.ids)}
            ids.sort(key=order.get)
            stack_id = linking.make_stack(conn, ids, body.cover)
            return {"id": stack_id, "count": len(ids)}

    @r.post("/api/stacks/{stack_id}/cover")
    def set_cover(stack_id: int, body: CoverBody, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            if not conn.execute("SELECT 1 FROM assets WHERE id = ? AND stack_id = ?", (body.cover, stack_id)).fetchone():
                raise HTTPException(404, "That photo is not in this stack.")
            conn.execute("UPDATE stacks SET cover_id = ? WHERE id = ?", (body.cover, stack_id))
        return {"ok": True}

    @r.post("/api/stacks/unstack")
    def unstack(body: Ids, request: Request):
        """Take apart the stacks these photos are in."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            marks = ",".join("?" * len(body.ids))
            stacks = [r[0] for r in conn.execute(
                f"SELECT DISTINCT stack_id FROM assets WHERE id IN ({marks}) AND stack_id IS NOT NULL", body.ids)]
            n = linking.unstack(conn, stacks)
        return {"stacks": len(stacks), "photos": n}

    # ------------------------------------------------------------ folders

    @r.get("/api/folders")
    def folders(request: Request, path: str = Query("", max_length=1000)):
        """The folders inside a folder of the library, and how many items each holds (including subfolders)."""
        current_user(request)
        prefix = path.strip("/")
        prefix = prefix + "/" if prefix else ""
        n = len(prefix)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(
                f"SELECT substr(a.path, {n + 1}, instr(substr(a.path, {n + 1}), '/') - 1) AS name,"
                " COUNT(*) AS count, coalesce("
                # the newest photo that isn't too dark or blurry, else the newest item
                "  MAX(CASE WHEN a.kind = 'photo' AND coalesce(a.brightness, 255) >= 40 AND coalesce(a.sharpness, 999) >= 50"
                "      THEN a.taken_at || '|' || a.id END), MAX(a.taken_at || '|' || a.id)) AS cover"
                f" FROM assets a WHERE substr(a.path, 1, ?) = ? AND instr(substr(a.path, {n + 1}), '/') > 0"
                f" AND a.trashed_at IS NULL AND {db.LIVE_PART_HIDDEN}"
                " GROUP BY name ORDER BY name COLLATE NOCASE",
                (n, prefix),
            ).fetchall()
            direct = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE substr(a.path, 1, ?) = ? AND instr(substr(a.path, {n + 1}), '/') = 0"
                f" AND a.trashed_at IS NULL AND {db.LIVE_PART_HIDDEN}", (n, prefix)
            ).fetchone()[0]
            covers = [int(row["cover"].rpartition("|")[2]) for row in rows]
            versions = _versions(conn, covers)
        if prefix and not rows and not direct:
            raise HTTPException(404, "No such folder.")
        return {
            "path": prefix.rstrip("/"),
            "items": direct,
            "folders": [
                {"name": row["name"], "path": prefix + row["name"], "count": row["count"],
                 "cover": c, "cover_v": versions.get(c)}
                for row, c in zip(rows, covers)
            ],
        }

    # ------------------------------------------------------------ memories

    @r.get("/api/memories")
    def memories(request: Request, today: str = Query(..., pattern=r"^\d{4}-\d{2}-\d{2}$")):
        """Photos taken on this day in earlier years (today is the viewer's own date)."""
        current_user(request)
        try:
            day = date.fromisoformat(today)
        except ValueError:
            raise HTTPException(400, "Bad date") from None
        month_day = day.strftime("-%m-%d")
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(
                "SELECT substr(a.taken_at, 1, 4) AS year, COUNT(*) AS count,"
                # the cover: a favourite if there is one, else the sharpest photo
                " (SELECT b.id FROM assets b WHERE substr(b.taken_at, 1, 10) = substr(a.taken_at, 1, 10)"
                f"  AND b.trashed_at IS NULL AND b.archived_at IS NULL AND b.kind = 'photo' AND b.has_thumb = 1"
                "  ORDER BY EXISTS (SELECT 1 FROM favorites f WHERE f.asset_id = b.id) DESC,"
                "  coalesce(b.sharpness, 0) DESC, b.id LIMIT 1) AS cover"
                f" FROM assets a WHERE substr(a.taken_at, 5, 6) = ? AND a.taken_at < ? AND {VISIBLE}"
                " GROUP BY year ORDER BY year DESC",
                (month_day, f"{day.year:04d}"),
            ).fetchall()
            versions = _versions(conn, [row["cover"] for row in rows])
        out = []
        for row in rows:
            year = int(row["year"])
            if row["cover"] is None:
                continue  # only videos, or nothing with a picture
            out.append({
                "date": f"{year:04d}{month_day}", "year": year, "years_ago": day.year - year,
                "count": row["count"], "cover": row["cover"], "cover_v": versions.get(row["cover"]),
            })
        return {"memories": out}

    # ------------------------------------------------------------ map

    @r.get("/api/map")
    def map_points(request: Request):
        """Every photo and video with a position, as [id, lat, lon], plus how the map should look."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            points = [list(row) for row in conn.execute(
                f"SELECT a.id, a.lat, a.lon, substr(a.sha1, 1, 8) FROM assets a WHERE a.lat IS NOT NULL AND {VISIBLE}"
                f" AND {db.STACK_HIDDEN} ORDER BY a.taken_at DESC")]
        return {
            "points": points,
            "tiles": cfg.map_tiles or None,
            "attribution": cfg.map_attribution if cfg.map_tiles else None,
        }

    return r
