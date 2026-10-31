"""0.8: face suggestions, the duplicates screen, safer login, XMP sidecars and storage templates."""

import base64
import shutil
import sqlite3
import time
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient

from snapshots import auth, db, faces, storage, xmp
from snapshots.analysis import DuplicateFinder
from helpers import (ADMIN, add_member, ids_by_name, login, make_jpeg, new_client, rescan, setup_admin, wait_for,
                     wait_scan)


def library(tmp_path) -> Path:
    root = tmp_path / "Photos"
    make_jpeg(root / "2023" / "beach.jpg", "2023:07:04 12:00:00", color=(10, 120, 200))
    make_jpeg(root / "2024" / "hike.jpg", "2024:05:01 09:30:00", color=(20, 160, 40))
    make_jpeg(root / "2024" / "dog.jpg", "2024:06:15 16:00:00", color=(120, 90, 30))
    return root


_open = []


@pytest.fixture(autouse=True)
def _close_clients(monkeypatch):
    monkeypatch.setattr(xmp, "CHECK_SECONDS", 0.2)
    yield
    while _open:
        _open.pop().__exit__(None, None, None)


def started(tmp_path, root, **kw):
    c, cfg = new_client(tmp_path, root, **kw)
    c.__enter__()
    _open.append(c)
    return c, cfg


def ready(tmp_path, **kw):
    root = library(tmp_path)
    c, cfg = started(tmp_path, root, **kw)
    setup_admin(c)
    wait_scan(c)
    return c, cfg, root


# ------------------------------------------------------------------ faces

def _unit(v):
    v = np.asarray(v, dtype=np.float32)
    return v / np.linalg.norm(v)


def _near(base, noise, seed):
    rng = np.random.default_rng(seed)
    return _unit(base + rng.normal(0, noise, 128))


def _person(conn, asset_id, vectors, name=None, hidden=0):
    pid = conn.execute("INSERT INTO people (name, hidden, created_at) VALUES (?, ?, '2024')", (name, hidden)).lastrowid
    for v in vectors:
        conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                     " VALUES (?, ?, 0.1, 0.1, 0.2, 0.2, 0.99, ?, '2024')", (asset_id, pid, v.tobytes()))
    return pid


def _faces_fixture(cfg, asset_id):
    rng = np.random.default_rng(1)
    ann, other = _unit(rng.normal(size=128)), _unit(rng.normal(size=128))
    with db.session(cfg.db_path) as conn:
        a = _person(conn, asset_id, [_near(ann, 0.06, i) for i in range(3)], "Ann")
        b = _person(conn, asset_id, [_near(ann, 0.13, 10 + i) for i in range(3)])     # Ann again, a split group
        c = _person(conn, asset_id, [_near(ann, 0.13, 20)])                           # one face: too small to list
        d = _person(conn, asset_id, [_near(other, 0.05, 30 + i) for i in range(3)])   # someone else
        e = _person(conn, asset_id, [_near(ann, 0.06, 40 + i) for i in range(3)], "Bob")  # another name: never
    return a, b, c, d, e


def test_face_suggestions(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    a, b, small, d, bob = _faces_fixture(cfg, ids["dog.jpg"])
    with db.session(cfg.db_path) as conn:
        link = faces.suggestions(conn, cfg.min_faces_per_person, 0.0)
    assert link, "fixture should produce suggestions"

    s = c.get("/api/people/suggestions").json()
    pairs = {(x["kind"], x["a"]["id"], x["b"]["id"]) for x in s["suggestions"]}
    assert ("merge", a, b) in pairs                      # the named person first
    assert any(k == "is_this" and y == small for k, _x, y in pairs)
    assert not any({x, y} == {a, bob} for _k, x, y in pairs)   # two different names are never suggested
    assert not any(d in (x, y) for _k, x, y in pairs)

    # Per person
    only = c.get("/api/people/suggestions", params={"person": b}).json()["suggestions"]
    assert only and all(b in (x["a"]["id"], x["b"]["id"]) for x in only)

    # "Different people": no longer suggested, and never merged automatically either
    assert c.post("/api/people/not-same", json={"a": b, "b": a}).status_code == 200
    s = c.get("/api/people/suggestions").json()
    assert not any({x["a"]["id"], x["b"]["id"]} == {a, b} for x in s["suggestions"])
    with db.session(cfg.db_path) as conn:
        assert faces.regroup(conn, 0.0) >= 0
        assert conn.execute("SELECT 1 FROM people WHERE id = ?", (a,)).fetchone()
        assert conn.execute("SELECT 1 FROM people WHERE id = ?", (b,)).fetchone()
        assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id = ?", (b,)).fetchone()[0] >= 3

    # Yes, merge: the small group joins Ann
    r = c.post(f"/api/people/{small}/merge", json={"into": a})
    assert r.status_code == 200 and r.json()["name"] == "Ann"


def test_merge_carries_different_people_along(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    a, b, small, d, bob = _faces_fixture(cfg, ids["dog.jpg"])
    c.post("/api/people/not-same", json={"a": small, "b": d})
    assert c.post(f"/api/people/{small}/merge", json={"into": b}).status_code == 200
    with db.session(cfg.db_path) as conn:
        assert conn.execute("SELECT 1 FROM people_not_same WHERE a = ? AND b = ?", (min(b, d), max(b, d))).fetchone()


def test_suggestions_ignore_locked_and_hidden(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    rng = np.random.default_rng(5)
    v = _unit(rng.normal(size=128))
    with db.session(cfg.db_path) as conn:
        a = _person(conn, ids["dog.jpg"], [_near(v, 0.05, i) for i in range(3)], "Ann")
        locked = _person(conn, ids["beach.jpg"], [_near(v, 0.05, 9)])
        hidden = _person(conn, ids["hike.jpg"], [_near(v, 0.05, 8 + i) for i in range(3)], hidden=1)
        conn.execute("UPDATE assets SET locked_by = 1 WHERE id = ?", (ids["beach.jpg"],))
    found = {x["b"]["id"] for x in c.get("/api/people/suggestions").json()["suggestions"]}
    assert locked not in found and hidden not in found and a not in found


def test_face_settings_and_regroup(tmp_path):
    c, cfg, _ = ready(tmp_path)
    assert c.get("/api/settings/faces").json()["strictness"] == "normal"
    r = c.put("/api/settings/faces", json={"strictness": "stricter", "min_faces": 5})
    assert r.status_code == 200 and r.json()["strictness"] == "stricter"
    assert cfg.face_match_threshold == 0.55 and cfg.min_faces_per_person == 5
    text = Path(cfg.config_file or __import__("snapshots.config", fromlist=["x"]).user_config_path()).read_text()
    assert "face_match_threshold = 0.55" in text
    assert c.put("/api/settings/faces", json={"suggest": False}).json()["suggest"] is False
    assert c.get("/api/people/suggestions").json()["suggestions"] == []

    member = add_member(c)
    m = TestClient(c.app)
    m.headers["X-Snapshots-Request"] = "1"
    login(m, member["username"], "member password 1")
    assert m.put("/api/settings/faces", json={"strictness": "looser"}).status_code == 403
    assert m.post("/api/people/regroup").status_code == 403


def test_recluster_keeps_named_people(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    a, b, small, d, bob = _faces_fixture(cfg, ids["dog.jpg"])
    with db.session(cfg.db_path) as conn:
        faces.recluster(conn, 0.5, 0.42)
        names = {r[0] for r in conn.execute("SELECT name FROM people WHERE name IS NOT NULL")}
        assert names == {"Ann", "Bob"}
        assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id IS NULL").fetchone()[0] == 0
        assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id = ?", (a,)).fetchone()[0] >= 3


# ------------------------------------------------------------------ duplicates

def test_identical_files_and_strictness(tmp_path):
    root = library(tmp_path)
    (root / "Backup").mkdir()
    shutil.copy(root / "2024" / "dog.jpg", root / "Backup" / "dog copy.jpg")
    (root / "Videos").mkdir()
    clip = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 40
    (root / "Videos" / "clip.mp4").write_bytes(clip)
    (root / "Backup" / "clip.mp4").write_bytes(clip)
    c, cfg = started(tmp_path, root)
    setup_admin(c)
    wait_scan(c)
    wait_for(lambda: c.get("/api/cleanup").json()["status"]["pending"] == 0, timeout=60)
    s = c.get("/api/cleanup").json()
    assert s["kinds"]["identical"]["groups"] == 2
    assert s["kinds"]["identical"]["space"] > 0
    g = c.get("/api/cleanup/groups", params={"kind": "identical"}).json()["groups"]
    assert len(g) == 2 and all(x["kind"] == "identical" for x in g)
    for group in g:
        # the first one added (lowest id) is kept, whatever order the folders are listed in
        assert group["keep"] == min(i["id"] for i in group["items"])
        assert group["why"]
    assert any(i["kind"] == "video" for group in g for i in group["items"])
    assert c.get("/api/cleanup/groups", params={"kind": "nonsense"}).status_code == 422
    for strictness in ("strict", "loose"):
        assert c.get("/api/cleanup", params={"strictness": strictness}).json()["strictness"] == strictness


def test_resized_copy_is_copies_not_identical(tmp_path):
    root = library(tmp_path)
    from PIL import Image
    img = Image.new("RGB", (640, 480))
    for x in range(640):
        for y in range(0, 480, 4):
            img.putpixel((x, y), (x % 256, y % 256, (x * y) % 256))
    make_jpeg(root / "a" / "big.jpg", "2022:01:01 10:00:00", image=img)
    make_jpeg(root / "b" / "small.jpg", "2022:01:01 10:00:00", image=img.resize((320, 240)))
    c, cfg = started(tmp_path, root)
    setup_admin(c)
    wait_scan(c)
    def pair_found():
        groups = c.get("/api/cleanup/groups").json()["groups"]
        return next((g for g in groups if {i["filename"] for i in g["items"]} == {"big.jpg", "small.jpg"}), None)
    g = wait_for(pair_found, timeout=90)   # the pictures are compared once they are read: wait for that
    assert g["kind"] == "copies"
    assert next(i for i in g["items"] if i["id"] == g["keep"])["filename"] == "big.jpg"
    assert g["why"] == "Most pixels"


# ------------------------------------------------------------------ login

def _second_browser(c):
    other = TestClient(c.app)
    other.headers["X-Snapshots-Request"] = "1"
    return other


def test_strangers_cant_lock_out_a_known_device(tmp_path):
    c, cfg, _ = ready(tmp_path)
    owner = _second_browser(c)
    login(owner, *ADMIN)                       # this browser is now known for "admin"
    assert owner.cookies.get(auth.DEVICE_COOKIE)
    owner.post("/api/auth/logout")

    stranger = _second_browser(c)
    for _ in range(10):
        assert stranger.post("/api/auth/login", json={"username": "admin", "password": "wrong!"}).status_code == 401
    assert stranger.post("/api/auth/login", json={"username": "admin", "password": "wrong!"}).status_code == 429
    # Even the right password from the stranger's device waits now ...
    assert stranger.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]}).status_code == 429
    # ... but the owner's own browser is not locked out.
    assert owner.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]}).status_code == 200


def test_known_device_has_its_own_limit(tmp_path):
    c, cfg, _ = ready(tmp_path)
    owner = _second_browser(c)
    login(owner, *ADMIN)
    owner.post("/api/auth/logout")
    for _ in range(10):
        owner.post("/api/auth/login", json={"username": "admin", "password": "nope nope"})
    assert owner.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]}).status_code == 429
    # A device cookie for another account doesn't help with this one
    add_member(c, "member", "member password 1")
    assert owner.post("/api/auth/login", json={"username": "member", "password": "member password 1"}).status_code == 200


def test_devices_list_and_forget(tmp_path):
    c, cfg, _ = ready(tmp_path)
    devs = c.get("/api/auth/devices").json()["devices"]
    assert len(devs) == 1 and devs[0]["this"]
    assert c.delete(f"/api/auth/devices/{devs[0]['id']}").status_code == 200
    assert c.get("/api/auth/devices").json()["devices"] == []


def _code(secret, offset=0):
    return auth.totp_at(secret, int(time.time() // 30) + offset)


def test_two_step_login(tmp_path):
    c, cfg, _ = ready(tmp_path)
    start = c.post("/api/auth/two-step/start").json()
    assert start["uri"].startswith("otpauth://totp/Snapshots:admin?secret=")
    assert c.post("/api/auth/two-step/confirm", json={"code": "000000"}).status_code == 400
    r = c.post("/api/auth/two-step/confirm", json={"code": _code(start["secret"])})
    assert r.status_code == 200
    recovery = r.json()["recovery_codes"]
    assert len(recovery) == 10 and len(set(recovery)) == 10
    assert c.get("/api/auth/two-step").json() == {"enabled": True, "recovery_left": 10}

    b = _second_browser(c)
    r = b.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]})
    assert r.status_code == 200 and r.json()["two_step"] and "token" not in r.json()
    assert b.get("/api/status").status_code == 401             # not logged in yet
    ticket = r.json()["ticket"]
    assert b.post("/api/auth/login/code", json={"ticket": ticket, "code": "123456"}).status_code == 401
    # The code used to confirm can't be used again; the next one works.
    r = b.post("/api/auth/login/code", json={"ticket": ticket, "code": _code(start["secret"], 1)})
    assert r.status_code == 200, r.text
    assert b.get("/api/status").status_code == 200
    # A code works only once
    b2 = _second_browser(c)
    t2 = b2.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]}).json()["ticket"]
    assert b2.post("/api/auth/login/code", json={"ticket": t2, "code": _code(start["secret"], 1)}).status_code == 401
    # A recovery code works once
    r = b2.post("/api/auth/login/code", json={"ticket": t2, "code": recovery[0].upper()})
    assert r.status_code == 200 and r.json()["recovery_left"] == 9
    b3 = _second_browser(c)
    t3 = b3.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]}).json()["ticket"]
    assert b3.post("/api/auth/login/code", json={"ticket": t3, "code": recovery[0]}).status_code == 401


def test_two_step_ticket_wears_out_and_admin_reset(tmp_path):
    c, cfg, _ = ready(tmp_path)
    member = add_member(c, "member", "member password 1")
    m = _second_browser(c)
    login(m, "member", "member password 1")
    start = m.post("/api/auth/two-step/start").json()
    assert m.post("/api/auth/two-step/confirm", json={"code": _code(start["secret"])}).status_code == 200
    m2 = _second_browser(c)
    t = m2.post("/api/auth/login", json={"username": "member", "password": "member password 1"}).json()["ticket"]
    for _ in range(5):
        assert m2.post("/api/auth/login/code", json={"ticket": t, "code": "999999"}).status_code == 401
    r = m2.post("/api/auth/login/code", json={"ticket": t, "code": _code(start["secret"], 1)})
    assert r.status_code == 401 and "log in again" in r.json()["detail"]
    users = {u["username"]: u for u in c.get("/api/users").json()["users"]}
    assert users["member"]["two_step"] is True
    assert c.patch(f"/api/users/{member['id']}", json={"two_step_off": True}).status_code == 200
    r = m2.post("/api/auth/login", json={"username": "member", "password": "member password 1"})
    assert r.status_code == 200 and "two_step" not in r.json()


def test_two_step_off_needs_password_and_keys_cant_change_it(tmp_path):
    c, cfg, _ = ready(tmp_path)
    start = c.post("/api/auth/two-step/start").json()
    c.post("/api/auth/two-step/confirm", json={"code": _code(start["secret"])})
    assert c.post("/api/auth/two-step/off", json={"password": "wrong"}).status_code == 400
    key = c.post("/api/keys", json={"name": "script"}).json()["key"]
    k = TestClient(c.app)
    k.headers["Authorization"] = f"Bearer {key}"
    k.headers["X-Snapshots-Request"] = "1"
    assert k.get("/api/status").status_code == 200            # API keys skip two-step, as before
    assert k.post("/api/auth/two-step/off", json={"password": ADMIN[1]}).status_code == 403
    assert k.post("/api/auth/two-step/start").status_code == 403
    assert c.post("/api/auth/two-step/off", json={"password": ADMIN[1]}).json() == {"enabled": False}


def test_totp_matches_rfc_6238():
    secret = base64.b32encode(b"12345678901234567890").decode()
    assert auth.totp_at(secret, 59 // 30, 8) == "94287082"
    assert auth.totp_at(secret, 1111111109 // 30, 8) == "07081804"
    code = auth.totp_at(secret, 1000)
    assert auth.check_totp(secret, code, None, now=1000 * 30 + 5) == 1000
    assert auth.check_totp(secret, code, 1000, now=1000 * 30 + 5) is None
    assert auth.check_totp(secret, code, None, now=1002 * 30) is None


# ------------------------------------------------------------------ XMP sidecars

def test_sidecars_written_and_merged(tmp_path):
    c, cfg, root = ready(tmp_path)
    ids = ids_by_name(c)
    # darktable already keeps a sidecar for hike.jpg
    foreign = root / "2024" / "hike.jpg.xmp"
    foreign.write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:darktable="http://darktable.sf.net/" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:xmp="http://ns.adobe.com/xap/1.0/" darktable:xmp_version="5" xmp:Rating="3">'
        '<darktable:history><rdf:Seq><rdf:li darktable:operation="exposure"/></rdf:Seq></darktable:history>'
        '<dc:subject><rdf:Bag><rdf:li>mountains</rdf:li></rdf:Bag></dc:subject>'
        '</rdf:Description></rdf:RDF></x:xmpmeta>', encoding="utf-8")
    assert c.put("/api/settings/xmp", json={"enabled": True}).json()["enabled"] is True

    c.post("/api/assets/details", json={"ids": [ids["dog.jpg"]], "description": "Rex at the lake"})
    c.post("/api/tags", json={"name": "pets", "ids": [ids["dog.jpg"]]})
    c.post("/api/favorites", json={"ids": [ids["dog.jpg"]], "value": True})
    with db.session(cfg.db_path) as conn:
        _person(conn, ids["dog.jpg"], [_unit(np.ones(128))], "Hans")

    dog = root / "2024" / "dog.jpg.xmp"
    side = wait_for(lambda: dog.is_file() and xmp.read(dog), timeout=30)
    side = wait_for(lambda: (s := xmp.read(dog))["people"] and s, timeout=30)
    assert side["description"] == "Rex at the lake"
    assert side["tags"] == ["pets"] and side["people"] == ["Hans"] and side["rating"] == 5 and side["ours"]
    assert side["taken"].isoformat() == "2024-06-15T16:00:00"

    # beach.jpg has nothing extra: no sidecar
    wait_for(lambda: c.get("/api/status").json().get("photos"))
    wait_for(lambda: foreign.read_text().count("Snapshots") or None, timeout=30)
    assert not (root / "2023" / "beach.jpg.xmp").exists()

    # The foreign sidecar keeps darktable's part and its rating; its keyword came in as a tag.
    text = foreign.read_text(encoding="utf-8")
    assert "darktable:history" in text and 'darktable:operation="exposure"' in text and "xmp_version" in text
    got = xmp.read(foreign)
    assert got["tags"] == ["mountains"] and got["rating"] == 3 and got["ours"]
    hike_tags = c.get(f"/api/assets/{ids['hike.jpg']}").json().get("tags")
    assert any((t["name"] if isinstance(t, dict) else t) == "mountains" for t in hike_tags)

    # Removing the tag and description updates the file
    tag_id = next(t["id"] for t in c.get("/api/tags").json()["tags"] if t["name"] == "pets")
    c.post(f"/api/tags/{tag_id}/remove", json={"ids": [ids["dog.jpg"]]})
    wait_for(lambda: xmp.read(dog)["tags"] == [], timeout=30)


def test_locked_items_get_no_sidecar_and_trash_removes_it(tmp_path):
    c, cfg, root = ready(tmp_path)
    ids = ids_by_name(c)
    c.put("/api/settings/xmp", json={"enabled": True})
    c.post("/api/assets/details", json={"ids": [ids["beach.jpg"], ids["dog.jpg"]], "description": "secret"})
    # The worker looks for changed items every 10 seconds, and a slow Windows runner can take longer still.
    wait_for(lambda: (root / "2024" / "dog.jpg.xmp").is_file(), timeout=90)
    (root / "2023" / "beach.jpg.xmp").unlink(missing_ok=True)
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE assets SET locked_by = 1 WHERE id = ?", (ids["beach.jpg"],))
        conn.execute("UPDATE assets SET description = 'still secret' WHERE id = ?", (ids["beach.jpg"],))
    time.sleep(1.5)
    assert not (root / "2023" / "beach.jpg.xmp").exists()
    c.post("/api/trash", json={"ids": [ids["dog.jpg"]]})
    assert c.post("/api/trash/empty", json={"ids": [ids["dog.jpg"]]}).json()["deleted"] == 1
    assert not (root / "2024" / "dog.jpg.xmp").exists()


def test_sidecar_read_when_a_new_file_arrives(tmp_path):
    c, cfg, root = ready(tmp_path)
    make_jpeg(root / "new" / "cat.jpg")                 # no date and no position in the file
    (root / "new" / "cat.jpg.xmp").write_text(
        '<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/"'
        ' xmlns:exif="http://ns.adobe.com/exif/1.0/" xmlns:lr="http://ns.adobe.com/lightroom/1.0/"'
        ' exif:DateTimeOriginal="2015-03-02T08:30:00" exif:GPSLatitude="33,27.0N" exif:GPSLongitude="112,4.2W">'
        '<dc:description><rdf:Alt><rdf:li xml:lang="x-default">Kitty</rdf:li></rdf:Alt></dc:description>'
        '<dc:subject><rdf:Bag><rdf:li>cats</rdf:li><rdf:li>Mia</rdf:li></rdf:Bag></dc:subject>'
        '<lr:hierarchicalSubject><rdf:Bag><rdf:li>People|Mia</rdf:li></rdf:Bag></lr:hierarchicalSubject>'
        '</rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>', encoding="utf-8")
    rescan(c)
    cat = ids_by_name(c)["cat.jpg"]
    item = c.get(f"/api/assets/{cat}").json()
    assert item["taken_at"] == "2015-03-02T08:30:00"
    assert item["description"] == "Kitty"
    assert round(item["position"][0], 2) == 33.45 and round(item["position"][1], 2) == -112.07
    tags = [t["name"] if isinstance(t, dict) else t for t in item["tags"]]
    assert tags == ["cats"]                             # "Mia" is a person, not a tag
    with db.session(cfg.db_path) as conn:
        assert [tuple(r) for r in conn.execute("SELECT name, source FROM face_hints WHERE asset_id = ?", (cat,)).fetchall()] == [("Mia", "xmp")]


def test_sidecar_reader_refuses_entities(tmp_path):
    p = tmp_path / "evil.jpg.xmp"
    p.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "aaaa">]><x:xmpmeta xmlns:x="adobe:ns:meta/">&a;</x:xmpmeta>')
    assert xmp.read(p)["description"] is None
    with pytest.raises(ValueError):
        xmp.write(p, {"description": "x"})
    assert "ENTITY" in p.read_text()                    # left alone


def test_xmp_settings_admin_only(tmp_path):
    c, cfg, _ = ready(tmp_path)
    add_member(c)
    m = _second_browser(c)
    login(m, "member", "member password 1")
    assert m.put("/api/settings/xmp", json={"enabled": True}).status_code == 403
    assert m.put("/api/settings/storage", json={"template": "x"}).status_code == 403


# ------------------------------------------------------------------ storage templates

def test_template_rules():
    assert storage.template_problem("Uploads/{year}/{month}") is None
    assert storage.template_problem("Photos/{year}/{year}-{month}-{day} {camera}") is None
    for bad in ("", "/abs/{year}", "C:/x", "../up", "a//b", "{nope}", "a/{year", ".hidden/{year}", "a/b:c"):
        assert storage.template_problem(bad), bad
    from datetime import datetime
    when = datetime(2019, 7, 4, 9, 30)
    assert storage.folder_for("Photos/{year}/{monthname}", when) == "Photos/2019/July"
    assert storage.folder_for("{type}/{camera}", when, 'Can"on: EOS?', "video") == "Videos/Can_on_ EOS_"
    assert storage.folder_for("{user}/{date}", when, None, "photo", "con") == "_con/2019-07-04"


def test_uploads_follow_the_template(tmp_path):
    c, cfg, root = ready(tmp_path)
    r = c.put("/api/settings/storage", json={"template": "Phone/{year}/{monthname} {day}"})
    assert r.status_code == 200 and r.json()["examples"][0] == "Phone/2026/October 30"
    assert c.put("/api/settings/storage", json={"template": "../oops"}).status_code == 400
    assert c.post("/api/settings/storage/preview", json={"template": "{bad}"}).json()["problem"]
    src = tmp_path / "upload.jpg"
    make_jpeg(src, "2019:07:04 09:30:00")
    nodate = tmp_path / "nodate.jpg"
    make_jpeg(nodate, color=(9, 9, 9))
    with open(src, "rb") as a, open(nodate, "rb") as b:
        res = c.post("/api/upload", files=[("files", ("upload.jpg", a, "image/jpeg")),
                                           ("files", ("nodate.jpg", b, "image/jpeg"))]).json()["results"]
    assert [x["status"] for x in res] == ["added", "added"]
    assert (root / "Phone" / "2019" / "July 04" / "upload.jpg").is_file()
    today = time.localtime()
    month = storage.MONTHS[today.tm_mon - 1]
    assert (root / "Phone" / f"{today.tm_year}" / f"{month} {today.tm_mday:02d}" / "nodate.jpg").is_file()
    assert not list((root / "Uploads" / ".incoming").iterdir())
