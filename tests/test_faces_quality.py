"""Keeping different people apart: face quality, one person per photo, ambiguous faces, people seen together,
faces that don't fit their group, and Check faces. Synthetic embeddings, so no models are needed."""

import pytest

np = pytest.importorskip("numpy")

from snapshots import db, faces
from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_scan


def _unit(v):
    return (np.asarray(v, np.float32) / np.linalg.norm(v)).astype(np.float32)


def _at(base, cos, seed):
    """A unit vector exactly `cos` alike to `base`."""
    rng = np.random.default_rng(seed)
    u = rng.normal(size=128).astype(np.float32)
    u -= (u @ base) * base
    u = _unit(u)
    return _unit(cos * base + np.sqrt(1 - cos * cos) * u)


def _db(tmp_path, photos=6):
    db.init(tmp_path / "x.db")
    conn = db.connect(tmp_path / "x.db")
    for i in range(1, photos + 1):
        conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at, width, height)"
                     " VALUES (?, ?, ?, 'photo', 1, 1, ?, 't', 't', 4000, 3000)", (i, f"p{i}.jpg", f"p{i}.jpg", f"s{i}"))
    return conn


def _face(conn, asset, person, emb, quality=0.9):
    return conn.execute(
        "INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at, quality)"
        " VALUES (?, ?, 0.1, 0.1, 0.1, 0.1, 0.99, ?, 't', ?)", (asset, person, emb.tobytes(), quality)).lastrowid


def _person(conn, pid, name=None):
    conn.execute("INSERT INTO people (id, name, created_at) VALUES (?, ?, 't')", (pid, name))


def test_quality_from_size_and_angle():
    assert faces.quality_of(40, 1.0) == 0.5            # smallest face, looking straight on
    assert faces.quality_of(100, 1.0) == 1.0
    assert faces.quality_of(300, 0.0) == 0.5           # big but turned away
    assert faces.quality_of(70, None) == pytest.approx(0.25 + 0.35)
    assert faces.weight_of(1.0) == 1.0 and faces.weight_of(0.0) == 0.25


def test_match_skips_the_photo_s_people_ambiguous_faces_and_needs_more_from_poor_ones(tmp_path):
    conn = _db(tmp_path)
    rng = np.random.default_rng(0)
    a, b = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
    cl = faces.Clusterer(conn, 0.50)
    cl.add(1, a)
    cl.add(2, b)
    near_a = _at(a, 0.8, 1)
    assert cl.match(near_a) == 1
    assert cl.match(near_a, exclude={1}) is None       # person 1 is already in this photo
    border = _at(a, 0.54, 2)
    assert cl.match(border, 0.9) == 1
    assert cl.match(border, 0.3) is None               # a poor face must be clearly alike
    # Two people who look a bit alike (not enough to be one person), and a face halfway: it is not guessed.
    c = _unit(rng.normal(size=128))
    d = _at(c, 0.45, 3)
    cl.add(3, c)
    cl.add(4, d)
    halfway = _unit(c + d)
    assert halfway @ c > 0.8 and halfway @ d > 0.8
    assert cl.match(halfway) is None
    assert cl.match(_at(c, 0.97, 4)) in (3, None)      # much nearer one of them: never the other
    conn.close()


def test_one_person_is_given_to_one_face_per_photo(tmp_path):
    conn = _db(tmp_path)
    a = _unit(np.random.default_rng(5).normal(size=128))
    cl = faces.Clusterer(conn, 0.50)
    cl.add(1, a)
    twins = [(_at(a, 0.9, 6), 0.6), (_at(a, 0.8, 7), 0.9)]
    got = faces.assign_photo(cl, twins)
    assert got == [None, 1]                            # the better face chooses first
    assert faces.assign_photo(cl, twins, taken={1}) == [None, None]
    conn.close()


def test_new_faces_in_one_photo_never_share_a_person(tmp_path):
    from PIL import Image
    from snapshots.config import Config

    cfg = Config(library_path=tmp_path, data_dir=tmp_path / "data")
    cfg.data_dir.mkdir()
    cfg.face_dir.mkdir()
    conn = _db(tmp_path)
    a = _unit(np.random.default_rng(8).normal(size=128))
    _person(conn, 1)
    _face(conn, 2, 1, a)
    job = faces.FacesJob(cfg)
    job.clusterer = faces.Clusterer(conn, 0.50)
    pic = Image.new("RGB", (8, 8))
    found = [{"x": 0.1, "y": 0.1, "w": 0.1, "h": 0.1, "score": 0.99, "embedding": _at(a, 0.85, s), "thumb": pic,
              "quality": 0.9} for s in (9, 10)]
    job._store(conn, 1, found)
    people = [r[0] for r in conn.execute("SELECT person_id FROM faces WHERE asset_id = 1")]
    assert len(people) == 2 and len(set(people)) == 2 and 1 in people
    conn.close()


def test_people_seen_together_are_never_merged(tmp_path):
    conn = _db(tmp_path)
    a = _unit(np.random.default_rng(11).normal(size=128))
    for pid in (1, 2):
        _person(conn, pid)
    for k, asset in enumerate((1, 2, 3)):
        _face(conn, asset, 1, _at(a, 0.9, 20 + k))
    for k, asset in enumerate((4, 5, 3)):              # photo 3 shows both
        _face(conn, asset, 2, _at(a, 0.9, 30 + k))
    assert faces.seen_together(conn) == [(1, 2)]
    assert faces.regroup(conn, 0.42) == 0
    conn.execute("UPDATE faces SET asset_id = 6 WHERE person_id = 2 AND asset_id = 3")
    assert faces.regroup(conn, 0.42) == 1              # not seen together any more: one person
    conn.close()


def test_faces_that_do_not_fit_leave_unnamed_groups_only(tmp_path):
    conn = _db(tmp_path, photos=8)
    rng = np.random.default_rng(12)
    a, b = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
    _person(conn, 1)
    _person(conn, 2)
    _person(conn, 3, "Sam")
    good = [_face(conn, asset, 1, _at(a, 0.92, 40 + asset)) for asset in (1, 2, 3, 4)]
    stranger = _face(conn, 5, 1, _at(b, 0.95, 50))      # someone else, in group 1
    twice = _face(conn, 4, 1, _at(a, 0.6, 51))          # group 1 a second time in photo 4, less alike
    for asset in (6, 7, 8):
        _face(conn, asset, 2, _at(b, 0.92, 60 + asset))  # where the stranger really belongs
    named = [_face(conn, asset, 3, _at(a, 0.92, 70 + asset)) for asset in (6, 7)]
    sam_odd = _face(conn, 8, 3, _at(b, 0.9, 80))        # a wrong face in a named person: left alone
    moved = faces.split_outliers(conn, 0.50)
    person = {fid: pid for fid, pid in conn.execute("SELECT id, person_id FROM faces")}
    assert moved == 2
    assert all(person[f] == 1 for f in good)
    assert person[stranger] == 2                        # found its place
    assert person[twice] not in (1, None)
    assert all(person[f] == 3 for f in named) and person[sam_odd] == 3
    assert faces.split_outliers(conn, 0.50) == 0        # settled
    conn.close()


def test_regrouping_from_scratch_keeps_the_photo_rule(tmp_path):
    conn = _db(tmp_path)
    a = _unit(np.random.default_rng(13).normal(size=128))
    _person(conn, 1)
    for asset in (1, 2, 3):
        _face(conn, asset, 1, _at(a, 0.9, 90 + asset))
    _face(conn, 3, 1, _at(a, 0.88, 99))                 # a look-alike beside them in photo 3
    faces.recluster(conn, 0.50, 0.42)
    by_photo = conn.execute("SELECT person_id FROM faces WHERE asset_id = 3").fetchall()
    assert len({r[0] for r in by_photo}) == 2
    conn.close()


def test_quality_of_older_faces_is_worked_out_on_upgrade(tmp_path):
    conn = _db(tmp_path)
    _person(conn, 1)
    fid = _face(conn, 1, 1, _unit(np.ones(128)), quality=None)
    conn.execute("UPDATE faces SET w = 0.05, h = 0.0667 WHERE id = ?", (fid,))
    update = db.MIGRATIONS[9].split("UPDATE faces SET quality", 1)[1]
    conn.execute("UPDATE faces SET quality" + update.rsplit(";", 1)[0])
    q = conn.execute("SELECT quality FROM faces WHERE id = ?", (fid,)).fetchone()[0]
    # 200 px wide in a 4000 px photo, seen at 1920 px: 96 px; angle unknown (0.7)
    assert q == pytest.approx(0.5 * (96 - 40) / 60 + 0.35, abs=0.002)
    conn.close()


def test_the_mirror_image_is_averaged_in(tmp_path):
    cv2 = pytest.importorskip("cv2")

    class Detector:
        def setInputSize(self, size):
            pass

        def detect(self, img):
            row = [10, 10, 60, 60, 25, 30, 55, 30, 40, 45, 28, 58, 52, 58, 0.99]
            return 1, np.array([row], np.float32)

    calls = []

    class Recognizer:
        def alignCrop(self, img, row):
            crop = np.zeros((112, 112, 3), np.uint8)
            crop[:, :56] = 200                           # left half bright: the mirror image differs
            return crop

        def feature(self, crop):
            calls.append(crop.copy())
            v = np.zeros((1, 128), np.float32)
            v[0, 0 if crop[0, 0, 0] else 1] = 1.0
            return v

    eng = faces.FaceEngine.__new__(faces.FaceEngine)
    eng.cv2, eng.detector, eng.recognizer = cv2, Detector(), Recognizer()
    make_jpeg(tmp_path / "a.jpg", "2024:01:01 10:00:00")
    found = eng.analyze(tmp_path / "a.jpg")
    assert len(calls) == 2 and (calls[1] == cv2.flip(calls[0], 1)).all()
    assert found[0]["embedding"][:2] == pytest.approx([2 ** -0.5, 2 ** -0.5])
    assert 0 <= found[0]["quality"] <= 1


def test_check_faces_lists_the_least_alike_first(tmp_path):
    root = tmp_path / "Photos"
    for n in ("a", "b", "c", "d"):
        make_jpeg(root / f"{n}.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        rng = np.random.default_rng(14)
        a, b = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
        with db.session(cfg.db_path) as conn:
            pid = conn.execute("INSERT INTO people (name, created_at) VALUES ('Sam', 't')").lastrowid
            mine = [_face(conn, ids[f"{n}.jpg"], pid, _at(a, 0.92, k)) for k, n in enumerate("abc")]
            odd = _face(conn, ids["d.jpg"], pid, _at(b, 0.9, 9))
        r = c.get(f"/api/people/{pid}/faces").json()
        assert r["total"] == 4 and r["faces"][0]["face_id"] == odd
        assert {f["face_id"] for f in r["faces"][1:]} == set(mine)
        assert r["faces"][0]["alike"] < r["faces"][1]["alike"]
        assert c.post(f"/api/faces/{odd}/unassign").json()["ok"]
        assert c.get(f"/api/people/{pid}/faces").json()["total"] == 3
        assert c.get("/api/people/999999/faces").status_code == 404


def test_a_person_split_in_two_still_gets_new_faces(tmp_path):
    conn = _db(tmp_path)
    a = _unit(np.random.default_rng(15).normal(size=128))
    cl = faces.Clusterer(conn, 0.50)
    for k in range(20):                                 # one person, in two groups that regrouping hasn't joined yet
        cl.add(1, _at(a, 0.9, 100 + k))
        cl.add(2, _at(a, 0.9, 200 + k))
    got = [cl.match(_at(a, 0.85, 300 + k)) for k in range(30)]
    assert all(g in (1, 2) for g in got)                # never a new group of one


def test_faces_moved_out_never_go_to_hidden_people(tmp_path):
    conn = _db(tmp_path, photos=8)
    rng = np.random.default_rng(16)
    a, b = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
    _person(conn, 1)
    conn.execute("INSERT INTO people (id, hidden, created_at) VALUES (2, 1, 't')")
    for asset in (1, 2, 3):
        _face(conn, asset, 1, _at(a, 0.92, 400 + asset))
    stranger = _face(conn, 4, 1, _at(b, 0.95, 410))
    for asset in (5, 6, 7):
        _face(conn, asset, 2, _at(b, 0.92, 420 + asset))
    assert faces.split_outliers(conn, 0.50) == 1
    owner = conn.execute("SELECT person_id FROM faces WHERE id = ?", (stranger,)).fetchone()[0]
    assert owner not in (1, 2, None)
    conn.close()
