"""People (groups of faces) and places."""

import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, faces, webhooks
from .app import current_user, require_admin
from .config import save_settings


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    hidden: bool | None = None
    clear_name: bool = False


class MergeBody(BaseModel):
    into: int


class PairBody(BaseModel):
    a: int
    b: int


class FaceSettings(BaseModel):
    strictness: str | None = Field(default=None, pattern="^(stricter|normal|looser)$")
    min_faces: int | None = Field(default=None, ge=1, le=50)
    suggest: bool | None = None


# How strictly faces are grouped: (match a face to a person, merge two groups, suggest a merge).
STRICTNESS = {"stricter": (0.55, 0.47, 0.34), "normal": (0.50, 0.42, 0.30), "looser": (0.45, 0.38, 0.27)}


class Suggester:
    """Face suggestions, worked out again only when faces or people changed."""

    def __init__(self, cfg):
        self.cfg = cfg
        self._lock = threading.Lock()
        self._key = None
        self._items: list[dict] = []

    def _version(self, conn):
        return tuple(conn.execute(
            "SELECT (SELECT COUNT(*) || ':' || COALESCE(SUM(person_id), 0) || ':' || COALESCE(MAX(id), 0) FROM faces),"
            " (SELECT COUNT(*) || ':' || SUM(hidden) || ':' || COALESCE(SUM(length(name) * id), 0) FROM people),"
            " (SELECT COUNT(*) FROM people_not_same),"
            " (SELECT COALESCE(SUM(trashed_at IS NOT NULL), 0) || ':' || COALESCE(SUM(locked_by IS NOT NULL), 0) FROM assets)"
        ).fetchone()) + (self.cfg.min_faces_per_person, self.cfg.face_suggest_threshold)

    def items(self, conn) -> list[dict]:
        if self.cfg.face_suggest_threshold <= 0:
            return []
        key = self._version(conn)
        with self._lock:
            if key != self._key:
                self._items = faces.suggestions(conn, self.cfg.min_faces_per_person, self.cfg.face_suggest_threshold)
                self._key = key
            return self._items


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
    suggester = Suggester(cfg)

    def get_person(conn, person_id: int):
        row = conn.execute(_PEOPLE_SQL + " WHERE p.id = ? GROUP BY p.id", (person_id,)).fetchone()
        if row is None:
            if conn.execute("SELECT 1 FROM people WHERE id = ?", (person_id,)).fetchone():
                return {"id": person_id, "name": None, "hidden": False, "photos": 0, "face_id": None}
            raise HTTPException(404, "No such person.")
        return _person_json(row)

    def faces_status() -> dict:
        strict = next((k for k, v in STRICTNESS.items()
                       if abs(v[0] - cfg.face_match_threshold) < 1e-6 and abs(v[1] - cfg.face_merge_threshold) < 1e-6),
                      "custom")
        return {"strictness": strict, "min_faces": cfg.min_faces_per_person,
                "suggest": cfg.face_suggest_threshold > 0, "regrouping": worker.faces.recluster_requested}

    @r.get("/api/people/suggestions")
    def people_suggestions(request: Request, person: int | None = None, limit: int = 30):
        """'Same person?' and 'Is this them?' questions, most likely first."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            items = suggester.items(conn)
            if person is not None:
                items = [i for i in items if person in (i["a"], i["b"])]
            total = len(items)
            items = items[:max(1, min(limit, 100))]
            ids = {i["a"] for i in items} | {i["b"] for i in items}
            people = {}
            for pid in ids:
                try:
                    people[pid] = get_person(conn, pid)
                except HTTPException:
                    pass
        likely = (cfg.face_suggest_threshold + cfg.face_merge_threshold) / 2
        out = [{"kind": i["kind"], "a": people[i["a"]], "b": people[i["b"]], "same_name": i["same_name"],
                "likely": i["score"] >= likely or i["same_name"]}
               for i in items if i["a"] in people and i["b"] in people and people[i["b"]]["face_id"]]
        return {"suggestions": out, "total": total}

    @r.post("/api/people/not-same")
    def not_same(body: PairBody, request: Request):
        """'These are different people': stop suggesting them, and never merge them automatically."""
        current_user(request)
        if body.a == body.b:
            raise HTTPException(400, "That is the same person.")
        with db.session(cfg.db_path) as conn:
            for pid in (body.a, body.b):
                get_person(conn, pid)
            faces.mark_apart(conn, body.a, body.b)
        return {"ok": True}

    @r.get("/api/settings/faces")
    def face_settings(request: Request):
        require_admin(request)
        return faces_status()

    @r.put("/api/settings/faces")
    def save_face_settings(body: FaceSettings, request: Request):
        require_admin(request)
        values = {}
        if body.strictness:
            match, merge, suggest = STRICTNESS[body.strictness]
            values.update(face_match_threshold=match, face_merge_threshold=merge)
            if cfg.face_suggest_threshold > 0:
                values["face_suggest_threshold"] = suggest
        if body.min_faces is not None:
            values["min_faces_per_person"] = body.min_faces
        if body.suggest is not None:
            strict = body.strictness or faces_status()["strictness"]
            values["face_suggest_threshold"] = STRICTNESS.get(strict, STRICTNESS["normal"])[2] if body.suggest else 0.0
        if values:
            save_settings(values, cfg.config_file)
            for k, v in values.items():
                setattr(cfg, k, v)
            worker.faces.invalidate()
        return faces_status()

    @r.post("/api/people/regroup")
    def regroup_faces(request: Request):
        """Group the faces of unnamed people again with the current strictness. Named people stay."""
        require_admin(request)
        worker.faces.recluster_requested = True
        worker.poke()
        return faces_status()

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
                before = conn.execute("SELECT name FROM people WHERE id = ?", (person_id,)).fetchone()[0]
                conn.execute("UPDATE people SET name = ? WHERE id = ?", (name or None, person_id))
                if name and name != before:
                    webhooks.emit(conn, "person.named", {"id": person_id, "name": name, "before": before})
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
            faces.move_apart(conn, person_id, body.into)
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
            if conn.execute("SELECT 1 FROM faces f JOIN assets a ON a.id = f.asset_id"
                            " WHERE f.id = ? AND a.locked_by IS NULL", (face_id,)).fetchone() is None:
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
