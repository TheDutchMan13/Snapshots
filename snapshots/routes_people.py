"""People (groups of faces) and places."""

import threading

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import db, faces, webhooks
from . import pets as pets_mod
from .app import current_user, require_admin
from .config import save_settings


class PersonUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=200)
    hidden: bool | None = None
    clear_name: bool = False
    # 'YYYY-MM-DD', or '--MM-DD' without the year; "" removes it
    birthday: str | None = Field(default=None, pattern=r"^$|^(\d{4}|-)-\d{2}-\d{2}$")


def clean_birthday(text: str) -> str | None:
    """A stored birthday from what was typed ("" removes it). Raises HTTPException for a date that doesn't exist."""
    from datetime import date

    if not text:
        return None
    year = 2000 if text.startswith("--") else int(text[:4])     # 2000 has a 29 February
    try:
        when = date(year, int(text[-5:-3]), int(text[-2:]))
    except ValueError:
        raise HTTPException(400, "That date doesn't exist.") from None
    if not text.startswith("--") and when > date.today():
        raise HTTPException(400, "That birthday is in the future.")
    return text


class MergeBody(BaseModel):
    into: int


class PetPhotos(BaseModel):
    ids: list[int] = Field(min_length=1, max_length=5000)


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
            "photos": row["photos"], "face_id": row["face_id"], "birthday": row["birthday"]}


_PEOPLE_SQL = """
    SELECT p.id, p.name, p.hidden, p.birthday, COUNT(DISTINCT f.asset_id) AS photos, COUNT(f.id) AS faces,
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
                return {"id": person_id, "name": None, "hidden": False, "photos": 0, "face_id": None,
                        "birthday": None}
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
            if body.birthday is not None:
                conn.execute("UPDATE people SET birthday = ? WHERE id = ?", (clean_birthday(body.birthday), person_id))
            return get_person(conn, person_id)

    @r.get("/api/people/{person_id}/together")
    def together(person_id: int, request: Request):
        """Who this person is most often photographed with, and the events they were at."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_person(conn, person_id)
            rows = conn.execute(
                "SELECT o.person_id, COUNT(DISTINCT o.asset_id) AS n FROM faces me"
                " JOIN faces o ON o.asset_id = me.asset_id AND o.person_id != me.person_id"
                " JOIN people p ON p.id = o.person_id AND p.hidden = 0 AND p.name IS NOT NULL"
                " JOIN assets a ON a.id = me.asset_id AND a.trashed_at IS NULL AND a.locked_by IS NULL"
                " WHERE me.person_id = ? GROUP BY o.person_id ORDER BY n DESC LIMIT 12", (person_id,)).fetchall()
            people = []
            for pid, n in rows:
                info = get_person(conn, pid)
                people.append(dict(info, together=n))
            found = [e for e in ctx.events.events(conn) if any(p["id"] == person_id for p in e.get("people", []))]
            versions = db.versions(conn, [e["cover"] for e in found])
        from .routes_explore import event_json

        return {"people": people, "events": [event_json(e, versions) for e in found[:60]]}

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

    @r.get("/api/people/{person_id}/faces")
    def person_faces(person_id: int, request: Request, limit: int = 60):
        """This person's faces, the least like the rest first: where a wrong face is most likely to be."""
        current_user(request)
        import numpy as np

        with db.session(cfg.db_path) as conn:
            get_person(conn, person_id)
            rows = conn.execute(
                "SELECT f.id, f.asset_id, f.embedding, f.quality FROM faces f JOIN assets a ON a.id = f.asset_id"
                " WHERE f.person_id = ? AND a.trashed_at IS NULL AND a.locked_by IS NULL", (person_id,)).fetchall()
        if not rows:
            return {"faces": [], "total": 0}
        V = np.stack([np.frombuffer(r["embedding"], dtype=np.float32) for r in rows])
        w = np.array([faces.weight_of(r["quality"]) for r in rows], dtype=np.float32)
        rest = (V * w[:, None]).sum(axis=0)[None, :] - V * w[:, None]
        norms = np.linalg.norm(rest, axis=1)
        alike = np.where(norms > 0, (rest * V).sum(axis=1) / np.maximum(norms, 1e-9), 1.0)
        order = np.argsort(alike, kind="stable")[:max(1, min(limit, 200))]
        return {"faces": [{"face_id": rows[k]["id"], "asset_id": rows[k]["asset_id"], "alike": round(float(alike[k]), 3)}
                          for k in order], "total": len(rows)}

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

    # ------------------------------------------------------------ pets (pets.py)

    _PET_SQL = (
        "SELECT p.id, p.name, p.species, p.hidden, p.birthday, COUNT(DISTINCT s.asset_id) AS photos,"
        " (SELECT s2.id FROM pet_sightings s2 JOIN assets a2 ON a2.id = s2.asset_id WHERE s2.pet_id = p.id"
        "  AND a2.trashed_at IS NULL AND a2.locked_by IS NULL ORDER BY s2.score DESC LIMIT 1) AS cover"
        " FROM pets p LEFT JOIN pet_sightings s ON s.pet_id = p.id"
        " AND s.asset_id IN (SELECT id FROM assets WHERE trashed_at IS NULL AND locked_by IS NULL)")

    def _pet_json(row) -> dict:
        return {"id": row["id"], "name": row["name"], "species": row["species"], "hidden": bool(row["hidden"]),
                "photos": row["photos"], "cover": row["cover"], "birthday": row["birthday"]}

    def get_pet(conn, pet_id: int) -> dict:
        row = conn.execute(_PET_SQL + " WHERE p.id = ? GROUP BY p.id", (pet_id,)).fetchone()
        if row is None or not row["photos"]:      # only in locked or trashed photos: not for anyone's eyes
            raise HTTPException(404, "No such pet.")
        return _pet_json(row)

    @r.get("/api/pets")
    def list_pets(request: Request, hidden: bool = False):
        """Named pets, and groups of the same animal with enough photos to be worth showing."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            rows = conn.execute(_PET_SQL + " WHERE p.hidden = ? GROUP BY p.id"
                                " HAVING p.name IS NOT NULL OR photos >= ? ORDER BY p.name IS NULL, photos DESC",
                                (int(hidden), pets_mod.MIN_PHOTOS)).fetchall()
        return {"pets": [_pet_json(r) for r in rows if r["photos"]],
                "enabled": cfg.find_pets and cfg.smart_search, "state": worker.pets.state}

    @r.get("/api/pets/{pet_id}")
    def pet(pet_id: int, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            return get_pet(conn, pet_id)

    @r.patch("/api/pets/{pet_id}")
    def update_pet(pet_id: int, body: PersonUpdate, request: Request):
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_pet(conn, pet_id)
            if body.clear_name:
                conn.execute("UPDATE pets SET name = NULL WHERE id = ?", (pet_id,))
            elif body.name is not None:
                conn.execute("UPDATE pets SET name = ? WHERE id = ?", (body.name.strip() or None, pet_id))
            if body.hidden is not None:
                conn.execute("UPDATE pets SET hidden = ? WHERE id = ?", (int(body.hidden), pet_id))
            if body.birthday is not None:
                conn.execute("UPDATE pets SET birthday = ? WHERE id = ?", (clean_birthday(body.birthday), pet_id))
            return get_pet(conn, pet_id)

    @r.post("/api/pets/{pet_id}/merge")
    def merge_pets(pet_id: int, body: MergeBody, request: Request):
        """'Same pet': all of this pet's photos move to `into`."""
        current_user(request)
        if body.into == pet_id:
            raise HTTPException(400, "That is the same pet.")
        with db.session(cfg.db_path) as conn:
            src = conn.execute("SELECT * FROM pets WHERE id = ?", (pet_id,)).fetchone()
            dst = conn.execute("SELECT * FROM pets WHERE id = ?", (body.into,)).fetchone()
            if src is None or dst is None:
                raise HTTPException(404, "No such pet.")
            conn.execute("UPDATE pet_sightings SET pet_id = ?, species = ? WHERE pet_id = ?",
                         (body.into, dst["species"], pet_id))
            if not dst["name"] and src["name"]:
                conn.execute("UPDATE pets SET name = ? WHERE id = ?", (src["name"], body.into))
            conn.execute("DELETE FROM pets WHERE id = ?", (pet_id,))
            return get_pet(conn, body.into)

    @r.post("/api/pets/{pet_id}/remove")
    def remove_from_pet(pet_id: int, body: PetPhotos, request: Request):
        """'This isn't them': take these photos out of the pet (they won't be put back)."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            get_pet(conn, pet_id)
            ids = db.open_ids(conn, body.ids)
            marks = ",".join("?" * len(ids)) or "NULL"
            conn.execute(f"UPDATE pet_sightings SET pet_id = NULL WHERE pet_id = ? AND asset_id IN ({marks})",
                         [pet_id] + ids)
            return get_pet(conn, pet_id)

    @r.get("/api/pets/{pet_id}/thumb")
    def pet_thumb(pet_id: int, request: Request):
        """The part of the clearest photo that shows the animal."""
        current_user(request)
        with db.session(cfg.db_path) as conn:
            cover = get_pet(conn, pet_id)["cover"]
            row = conn.execute("SELECT asset_id, x, y, w, h FROM pet_sightings WHERE id = ?", (cover,)).fetchone() \
                if cover else None
        if row is None:
            raise HTTPException(404, "No picture")
        out = cfg.data_dir / "pets" / f"{cover}.jpg"
        if not out.is_file():
            from PIL import Image

            try:
                with Image.open(cfg.thumb_path(row["asset_id"])) as img:
                    w, h = img.size
                    part = img.convert("RGB").crop((int(row["x"] * w), int(row["y"] * h),
                                                    int((row["x"] + row["w"]) * w), int((row["y"] + row["h"]) * h)))
                    part.thumbnail((240, 240))
                    out.parent.mkdir(parents=True, exist_ok=True)
                    part.save(out, "JPEG", quality=85)
            except OSError:
                raise HTTPException(404, "No picture") from None
        return FileResponse(out, media_type="image/jpeg", headers={"Cache-Control": "private, max-age=3600"})

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
