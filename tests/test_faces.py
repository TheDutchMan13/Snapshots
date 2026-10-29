import os
import shutil
from collections import defaultdict
from pathlib import Path

import pytest

np = pytest.importorskip("numpy")

from snapshots import db, faces
from helpers import ids_by_name, new_client, setup_admin, wait_for, wait_scan

# ------------------------------------------------------------------ grouping (no models needed)


def _unit(v):
    return (v / np.linalg.norm(v)).astype(np.float32)


def test_clusterer_groups_similar_faces(tmp_path):
    db.init(tmp_path / "x.db")
    conn = db.connect(tmp_path / "x.db")
    rng = np.random.default_rng(1)
    people = [_unit(rng.normal(size=128)) for _ in range(6)]
    cl = faces.Clusterer(conn, threshold=0.45)
    assigned = defaultdict(set)
    next_id = 1000
    for round_ in range(8):
        for who, base in enumerate(people):
            emb = _unit(base + rng.normal(size=128) * 0.07)  # same person, different photo
            pid = cl.match(emb)
            if pid is None:
                pid, next_id = next_id, next_id + 1
            cl.add(pid, emb)
            assigned[who].add(pid)
    assert all(len(ids) == 1 for ids in assigned.values())          # nobody was split
    assert len({next(iter(ids)) for ids in assigned.values()}) == 6  # nobody was merged
    conn.close()


def test_clusterer_starts_from_saved_faces(tmp_path):
    db.init(tmp_path / "x.db")
    conn = db.connect(tmp_path / "x.db")
    conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                 " VALUES (1, 'a', 'a', 'photo', 1, 1, 's', 't', 't')")
    conn.execute("INSERT INTO people (id, created_at) VALUES (7, 't')")
    v = _unit(np.arange(128, dtype=np.float32) + 1)
    conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                 " VALUES (1, 7, 0, 0, .1, .1, .9, ?, 't')", (v.tobytes(),))
    cl = faces.Clusterer(conn, 0.45)
    assert cl.match(v) == 7
    assert cl.match(_unit(-v)) is None
    for i in range(100):  # grows past its first capacity without trouble
        cl.add(10_000 + i, _unit(np.random.default_rng(i).normal(size=128)))
    assert cl.match(v) == 7
    conn.close()


# ------------------------------------------------------------------ model download


def test_models_are_checked_before_use(tmp_path, monkeypatch):
    good = tmp_path / "good.onnx"
    good.write_bytes(b"model bytes")
    bad = tmp_path / "bad.onnx"
    bad.write_bytes(b"tampered")
    sha = faces._sha256(good)
    spec = {"detector": {"file": "det.onnx", "sha256": sha, "urls": [bad.as_uri(), good.as_uri()]}}
    monkeypatch.setattr(faces, "MODELS", spec)

    paths = faces.ensure_models(tmp_path / "models")       # first address is wrong, second is right
    assert paths["detector"].read_bytes() == b"model bytes"

    paths["detector"].write_bytes(b"corrupted on disk")     # damaged later: downloaded again
    assert faces.ensure_models(tmp_path / "models")["detector"].read_bytes() == b"model bytes"

    spec["detector"]["urls"] = [bad.as_uri()]
    paths["detector"].unlink()
    with pytest.raises(RuntimeError, match="Could not download"):
        faces.ensure_models(tmp_path / "models")
    assert not list((tmp_path / "models").glob("*.part"))


# ------------------------------------------------------------------ the real thing (optional)
# Set SNAPSHOTS_TEST_MODELS to a folder holding the two .onnx files, and SNAPSHOTS_TEST_FACES to
# a folder of face photos named "<person>_<anything>.jpg" (several per person).

MODELS = os.environ.get("SNAPSHOTS_TEST_MODELS")
FACES = os.environ.get("SNAPSHOTS_TEST_FACES")
needs_real_faces = pytest.mark.skipif(not (MODELS and FACES and faces.available() is None),
                                      reason="set SNAPSHOTS_TEST_MODELS and SNAPSHOTS_TEST_FACES")


@needs_real_faces
def test_faces_end_to_end(tmp_path):
    by_person = defaultdict(list)
    for p in sorted(Path(FACES).glob("*.jpg")):
        by_person[p.name.split("_")[0]].append(p)
    chosen = [k for k, v in by_person.items() if len(v) >= 4][:6]
    assert len(chosen) >= 4, "need at least 4 people with 4+ photos"
    lib = tmp_path / "Photos"
    lib.mkdir()
    label_of = {}
    for person in chosen:
        for p in by_person[person][:6]:
            shutil.copy(p, lib / p.name)
            label_of[p.name] = person

    data = tmp_path / "data"
    (data / "models").mkdir(parents=True)
    for spec in faces.MODELS.values():
        shutil.copy(Path(MODELS) / spec["file"], data / "models" / spec["file"])

    c, cfg = new_client(tmp_path, lib, faces=True, data_dir=data)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/status").json()["faces"]["pending"] == 0, timeout=300)
        listed = c.get("/api/people").json()["people"]
        assert listed, "no people were found"

        # every group must contain one person only
        groups = {}
        for person in listed:
            names = set(ids_by_name(c, person=person["id"]))
            labels = {label_of[n] for n in names}
            assert len(labels) == 1, f"group {person['id']} mixes {labels}"
            groups[person["id"]] = labels.pop()
        assert len(set(groups.values())) >= len(chosen) - 1  # nearly everyone was found

        # name someone, then find them by name
        pid, label = next(iter(groups.items()))
        assert c.patch(f"/api/people/{pid}", json={"name": "Grandma"}).json()["name"] == "Grandma"
        found = set(ids_by_name(c, q="grandma"))
        assert found and {label_of[n] for n in found} == {label}

        # the photo shows who is in it, with a picture of each face
        some_photo = ids_by_name(c, person=pid)
        detail = c.get(f"/api/assets/{next(iter(some_photo.values()))}").json()
        assert any(p["name"] == "Grandma" for p in detail["people"])
        assert c.get(f"/api/faces/{detail['people'][0]['face_id']}/thumb").status_code == 200

        # merging two groups
        if len(groups) >= 2:
            a, b = list(groups)[:2]
            before = len(ids_by_name(c, person=a)) + len(ids_by_name(c, person=b))
            merged = c.post(f"/api/people/{b}/merge", json={"into": a}).json()
            assert merged["id"] == a and merged["photos"] == before
            assert c.get(f"/api/people/{b}").status_code == 404

        # "not this person"
        face_id = detail["people"][0]["face_id"]
        assert c.post(f"/api/faces/{face_id}/unassign").status_code == 200

        # hiding someone takes them out of the list and out of search
        c.patch(f"/api/people/{pid}", json={"hidden": True})
        assert pid not in [p["id"] for p in c.get("/api/people").json()["people"]]
        assert pid in [p["id"] for p in c.get("/api/people", params={"hidden": True}).json()["people"]]
        assert ids_by_name(c, q="grandma") == {}

        # trashing a photo removes it from its person's count; deleting it removes its faces
        before = c.get(f"/api/people/{a}").json()["photos"]
        victim = next(iter(ids_by_name(c, person=a).values()))
        c.post("/api/trash", json={"ids": [victim]})
        assert c.get(f"/api/people/{a}").json()["photos"] == before - 1
        face_files = len(list(cfg.face_dir.glob("*.jpg")))
        c.post("/api/trash/empty", json={})
        assert len(list(cfg.face_dir.glob("*.jpg"))) < face_files
