"""Archive, tags, stacks, folders, memories ("on this day"), the map and the locked folder."""

from datetime import date, datetime

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from . import auth, db, libraries, linking
from .app import current_user, require_session


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


class LockBody(Ids):
    value: bool = True


class PinBody(BaseModel):
    pin: str = Field(min_length=1, max_length=64)


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


_versions = db.versions


# Shown in memories and the map: everything in the library that isn't hidden.
VISIBLE = f"a.trashed_at IS NULL AND a.archived_at IS NULL AND {db.LIVE_PART_HIDDEN} AND {db.NOT_LOCKED}"


def router(ctx) -> APIRouter:
    cfg = ctx.cfg
    r = APIRouter()

    # ------------------------------------------------------------ archive

    @r.post("/api/archive")
    def archive(body: ArchiveBody, request: Request):
        """Archived items leave the timelines (All, Photos, Videos, memories, the map) but stay everywhere else."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = linking.with_clips(conn, db.open_ids(conn, body.ids))
            stamp = _now() if body.value else None
            conn.executemany(
                "UPDATE assets SET archived_at = ? WHERE id = ? AND (archived_at IS NULL) = ?",
                [(stamp, i, 1 if body.value else 0) for i in ids])
        return {"ok": True}

    # ------------------------------------------------------------ tags

    def tag_json(conn, row) -> dict:
        stats = conn.execute(
            "SELECT COUNT(*) AS n, MAX(a.taken_at || '|' || a.id) AS cover FROM asset_tags at"
            f" JOIN assets a ON a.id = at.asset_id AND a.trashed_at IS NULL AND {db.NOT_LOCKED}"
            " WHERE at.tag_id = ?", (row["id"],)
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
                         [(tag_id, i) for i in db.open_ids(conn, ids)])

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
                             [(tag_id, i) for i in db.open_ids(conn, body.ids)])
            return tag_json(conn, get_tag(conn, tag_id))

    # ------------------------------------------------------------ stacks

    @r.post("/api/stacks", status_code=201)
    def create_stack(body: StackBody, request: Request):
        """Put photos in a stack: one tile in the timeline, the others behind it."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
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
            if not conn.execute("SELECT 1 FROM assets WHERE id = ? AND stack_id = ? AND locked_by IS NULL",
                                (body.cover, stack_id)).fetchone():
                raise HTTPException(404, "That photo is not in this stack.")
            conn.execute("UPDATE stacks SET cover_id = ? WHERE id = ?", (body.cover, stack_id))
        return {"ok": True}

    @r.post("/api/stacks/unstack")
    def unstack(body: Ids, request: Request):
        """Take apart the stacks these photos are in."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            ids = db.open_ids(conn, body.ids)
            marks = ",".join("?" * len(ids)) or "NULL"
            stacks = [r[0] for r in conn.execute(
                f"SELECT DISTINCT stack_id FROM assets WHERE id IN ({marks}) AND stack_id IS NOT NULL", ids)]
            n = linking.unstack(conn, stacks)
        return {"stacks": len(stacks), "photos": n}

    # ------------------------------------------------------------ folders

    @r.get("/api/folders")
    def folders(request: Request, path: str = Query("", max_length=1000)):
        """The folders inside a folder of the library, and how many items each holds (including subfolders).

        The main library's top level also lists the extra library folders.
        """
        current_user(request)
        prefix = db.folder_prefix(path)
        n = len(prefix)
        all_roots = libraries.roots(cfg)
        shown = f"a.trashed_at IS NULL AND {db.LIVE_PART_HIDDEN} AND {db.NOT_LOCKED}"
        # the newest photo that isn't too dark or blurry, else the newest item
        cover_sql = (
            "coalesce(MAX(CASE WHEN a.kind = 'photo' AND coalesce(a.brightness, 255) >= 40"
            " AND coalesce(a.sharpness, 999) >= 50 THEN a.taken_at || '|' || a.id END), MAX(a.taken_at || '|' || a.id))")
        main_only = "" if prefix else " AND NOT (a.path GLOB '/*' OR a.path GLOB '[A-Za-z]:/*')"
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(
                f"SELECT substr(a.path, {n + 1}, instr(substr(a.path, {n + 1}), '/') - 1) AS name,"
                f" COUNT(*) AS count, {cover_sql} AS cover"
                f" FROM assets a WHERE substr(a.path, 1, ?) = ? AND instr(substr(a.path, {n + 1}), '/') > 0"
                f" AND {shown}{main_only} GROUP BY name ORDER BY name COLLATE NOCASE",
                (n, prefix),
            ).fetchall()
            direct = conn.execute(
                f"SELECT COUNT(*) FROM assets a WHERE substr(a.path, 1, ?) = ? AND instr(substr(a.path, {n + 1}), '/') = 0"
                f" AND {shown}", (n, prefix)
            ).fetchone()[0]
            extra = []
            if not prefix:
                for root in all_roots:
                    if root.main:
                        continue
                    stats = conn.execute(
                        f"SELECT COUNT(*) AS count, {cover_sql} AS cover FROM assets a"
                        f" WHERE substr(a.path, 1, ?) = ? AND {shown}", (len(root.prefix), root.prefix)).fetchone()
                    extra.append((root, stats))
            covers = [int(row["cover"].rpartition("|")[2]) for row in rows]
            covers += [int(st["cover"].rpartition("|")[2]) for _, st in extra if st["cover"]]
            versions = _versions(conn, covers)
        if prefix and not rows and not direct:
            raise HTTPException(404, "No such folder.")

        # Breadcrumbs: from the library folder the path is in.
        here = prefix.rstrip("/")
        root = next((x for x in all_roots if not x.main and (prefix == x.prefix or prefix.startswith(x.prefix))), None)
        main = next((x for x in all_roots if x.main), None)
        if root is not None:
            crumbs = [{"name": root.name, "path": root.prefix.rstrip("/")}]
            rest = prefix[len(root.prefix):].strip("/")
            base = root.prefix
        else:
            crumbs = [{"name": main.name if main else "Library", "path": ""}]
            rest, base = here, ""
        if rest:
            parts = rest.split("/")
            for i, part in enumerate(parts):
                crumbs.append({"name": part, "path": base + "/".join(parts[:i + 1])})

        folders = [
            {"name": row["name"], "path": prefix + row["name"], "count": row["count"],
             "cover": c, "cover_v": versions.get(c)}
            for row, c in zip(rows, covers)
        ]
        for x, st in extra:
            c = int(st["cover"].rpartition("|")[2]) if st["cover"] else None
            folders.append({"name": x.name, "path": x.prefix.rstrip("/"), "count": st["count"], "cover": c,
                            "cover_v": versions.get(c), "library": str(x.path), "available": x.available()})
        return {"path": here, "items": direct, "crumbs": crumbs, "folders": folders}

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
                f"  AND b.trashed_at IS NULL AND b.archived_at IS NULL AND b.locked_by IS NULL"
                "  AND b.kind = 'photo' AND b.has_thumb = 1"
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
                f"SELECT a.id, a.lat, a.lon, {db.version_sql()} FROM assets a WHERE a.lat IS NOT NULL AND {VISIBLE}"
                f" AND {db.STACK_HIDDEN} ORDER BY a.taken_at DESC")]
        return {
            "points": points,
            "tiles": cfg.map_tiles or None,
            "attribution": cfg.map_attribution if cfg.map_tiles else None,
        }

    # ------------------------------------------------------------ the locked folder

    @r.post("/api/locked/pin")
    def set_pin(request: Request, body: dict):
        """Set or change the PIN of your locked folder. Needs your account password."""
        user = require_session(request)
        pin, password = str(body.get("pin") or ""), str(body.get("password") or "")
        problem = auth.validate_pin(pin)
        if problem:
            raise HTTPException(400, problem)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not auth.verify_password(password, row["pw_hash"]):
                raise HTTPException(400, "Your password is not right.")
            conn.execute("UPDATE users SET pin_hash = ? WHERE id = ?", (auth.hash_password(pin), user["id"]))
        return {"ok": True}

    @r.post("/api/locked/open")
    def open_locked(body: PinBody, request: Request):
        """Open your locked folder for a few minutes, in this session only."""
        user = require_session(request)
        keys = (f"pin:{user['id']}",)
        wait = ctx.pin_throttle.retry_after(*keys)
        if wait:
            raise HTTPException(429, f"Too many wrong PINs. Try again in {max(1, wait // 60)} minute(s).",
                                headers={"Retry-After": str(wait)})
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT pin_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not row["pin_hash"]:
                raise HTTPException(400, "Set a PIN first.")
            if not auth.verify_password(body.pin, row["pin_hash"]):
                ctx.pin_throttle.failed(*keys)
                raise HTTPException(400, "Wrong PIN.")
            ctx.pin_throttle.succeeded(*keys)
            until = auth.set_unlocked(conn, request.state.token, auth.UNLOCK_MINUTES)
        return {"open": True, "until": until}

    @r.post("/api/locked/close")
    def close_locked(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            auth.set_unlocked(conn, request.state.token, 0)
        return {"open": False}

    @r.post("/api/locked")
    def lock(body: LockBody, request: Request):
        """Move items into your locked folder (value=true), or back out of it (false, with the folder open).

        Locked items are hidden from everyone everywhere else in Snapshots. The files stay where they are.
        """
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            if body.value:
                if not conn.execute("SELECT pin_hash FROM users WHERE id = ?", (user["id"],)).fetchone()[0]:
                    raise HTTPException(400, "Set a PIN for your locked folder first.")
                ids = linking.with_clips(conn, db.open_ids(conn, body.ids))
                # A locked item leaves its stack, so nothing about it shows through the rest of the stack.
                conn.executemany("UPDATE assets SET locked_by = ?, stack_id = NULL, no_auto_stack = 1"
                                 " WHERE id = ? AND locked_by IS NULL", [(user["id"], i) for i in ids])
                linking.tidy_stacks(conn)
            else:
                if not user.get("unlocked"):
                    raise HTTPException(403, "Open the locked folder first.")
                mine = [r[0] for r in conn.execute(
                    f"SELECT id FROM assets WHERE locked_by = ? AND id IN ({','.join('?' * len(body.ids))})",
                    [user["id"], *body.ids])]
                ids = linking.with_clips(conn, mine)
                conn.executemany("UPDATE assets SET locked_by = NULL WHERE id = ? AND locked_by = ?",
                                 [(i, user["id"]) for i in ids])
        return {"count": len(ids)}

    return r
