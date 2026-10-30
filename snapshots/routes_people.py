"""People (groups of faces) and places."""

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db
from .app import current_user


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    hidden: bool | None = None
    clear_name: bool = False


class MergeBody(BaseModel):
    into: int


def _person_json(row) -> dict:
    return {"id": row["id"], "name": row["name"], "hidden": bool(row["hidden"]),
            "photos": row["photos"], "face_id": row["face_id"]}


_PEOPLE_SQL = """
    SELECT p.id, p.name, p.hidden, COUNT(DISTINCT f.asset_id) AS photos, COUNT(f.id) AS faces,
           (SELECT f2.id FROM faces f2 JOIN assets a2 ON a2.id = f2.asset_id AND a2.trashed_at IS NULL
              AND a2.locked_by IS NULL
             WHERE f2.person_id = p.id ORDER BY f2.score * f2.w DESC LIMIT 1) AS face_id
    FROM people p
    JOIN faces f ON f.person_id = p.id
    JOIN assets a ON a.id = f.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL
"""


def router(ctx) -> APIRouter:
    cfg, worker = ctx.cfg, ctx.worker
    r = APIRouter()

    def get_person(conn, person_id: int):
        row = conn.execute(_PEOPLE_SQL + " WHERE p.id = ? GROUP BY p.id", (person_id,)).fetchone()
        if row is None:
            if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone():
                return {"id": person_id, "name": None, "hidden": False, "photos": 0, "face_id": None}
            raise HTTPException(404, "No such person.")
        return _person_json(row)

    @r.get("/api/people")
    def list_people(request: Request, hidden: bool = False):
        """Named people, and unnamed groups with enough photos to be worth showing."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(
                _PEOPLE_SQL
                + " WHERE p.hidden = ? GROUP BY p.id"
                  " HAVING p.name IS NOT NULL OR COUNT(f.id) >= ?"
                  " ORDER BY p.name IS NULL, photos DESC, p.id",
                (int(hidden), cfg.min_faces_per_person),
            ).fetchall()
            hidden_count = conn.execute("SELECT COUNT(*) FROM people WHERE hidden = 1").fetchone()[0]
        return {"people": [_person_json(x) for x in rows], "hidden_count": hidden_count,
                "faces": worker.status()["faces"]}

    @r.get("/api/people/{person_id}")
    def person(person_id: int, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            return get_person(conn, person_id)

    @r.patch("/api/people/{person_id}")
    def update_person(person_id: int, body: PersonUpdate, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_person(conn, person_id)
            if body.clear_name:
                conn.execute("UPDATE people SET name = NULL WHERE id = ?", (person_id,))
            elif body.name is not None:
                name = body.name.strip()
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (name or None, person_id))
            if body.hidden is not None:
                conn.execute("UPDATE people SET hidden = ? WHERE id = ?", (int(body.hidden), person_id))
            return get_person(conn, person_id)

    @r.post("/api/people/{person_id}/merge")
    def merge(person_id: int, body: MergeBody, request: Request):
        """All of this person's faces move to `into`, and this person is removed."""
        current_user(request)
        if body.into == person_id:
            raise HTTPException(400, "That is the same person.")
        with db.session(cfg.db_path) as conn:
            src = conn.execute("SELECT * FROM people WHERE id = ?", (person_id,)).fetchone()
            dst = conn.execute("SELECT * FROM people WHERE id = ?", (body.into,)).fetchone()
            if src is None or dst is None:
                raise HTTPException(404, "No such person.")
            conn.execute("UPDATE faces SET person_id = ? WHERE person_id = ?", (body.into, person_id))
            if not dst["name"] and src["name"]:
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (src["name"], body.into))
            conn.execute("DELETE FROM people WHERE id = ?", (person_id,))
            result = get_person(conn, body.into)
        worker.faces.invalidate()
        return result

    @r.post("/api/faces/{face_id}/unassign")
    def unassign(face_id: int, request: Request):
        """'This is not them': take one face out of its person."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            if conn.execute("SELECT 1 FROM faces WHERE id = ?", (face_id,)).fetchone() is None:
                raise HTTPException(404, "No such face.")
            conn.execute("UPDATE faces SET person_id = NULL WHERE id = ?", (face_id,))
        worker.faces.invalidate()
        return {"ok": True}

    @r.get("/api/faces/{face_id}/thumb")
    def face_thumb(face_id: int, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT a.locked_by FROM faces f JOIN assets a ON a.id = f.asset_id WHERE f.id = ?",
                               (face_id,)).fetchone()
        path = cfg.face_path(face_id)
        if row is None or row["locked_by"] is not None or not path.is_file():
            raise HTTPException(404, "No picture")
        return FileResponse(path, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=86400"})

    # ------------------------------------------------------------ places

    @r.get("/api/places")
    def places(request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(
                "SELECT country, state, city, COUNT(*) AS count,"
                " MAX(taken_at || '|' || id) AS cover FROM assets"
                " WHERE trashed_at IS NULL AND locked_by IS NULL AND country IS NOT NULL"
                " GROUP BY country, state, city ORDER BY country, state, count DESC"
            ).fetchall()
            out = []
            for row in rows:
                cover = int(row["cover"].rpartition("|")[2])
                out.append({"country": row["country"], "state": row["state"], "city": row["city"],
                            "count": row["count"], "cover": cover})
            versions = db.versions(conn, [o["cover"] for o in out])
            for o in out:
                o["cover_v"] = versions.get(o["cover"])
            located = conn.execute(
                "SELECT COUNT(*) FROM assets WHERE trashed_at IS NULL AND locked_by IS NULL AND lat IS NOT NULL"
            ).fetchone()[0]
        return {"places": out, "located": located, "status": worker.status()["places"]}

    return r
