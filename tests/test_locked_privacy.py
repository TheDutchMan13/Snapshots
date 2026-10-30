"""Nothing about a locked item may show through anywhere else (found by an independent review)."""

import time

from fastapi.testclient import TestClient

from snapshots import db
from helpers import ADMIN, add_member, ids_by_name, jpeg_bytes, login, make_jpeg, mp4_bytes, new_client, rescan, \
    setup_admin, wait_scan


def other_client(c):
    o = TestClient(c.app)
    o.headers["X-Snapshots-Request"] = "1"
    add_member(c)
    login(o, "member", "member password 1")
    return o


def lock(c, ids):
    c.post("/api/locked/pin", json={"pin": "2468", "password": ADMIN[1]})
    r = c.post("/api/locked", json={"ids": ids})
    assert r.status_code == 200, r.text


def test_memories_cover_is_never_locked(tmp_path):
    root = tmp_path / "P"
    make_jpeg(root / "secret.jpg", "2023:10:29 10:00:00")
    make_jpeg(root / "open.jpg", "2023:10:29 11:00:00", color=(1, 2, 3))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/favorites", json={"ids": [ids["secret.jpg"]]})  # would be the preferred cover
        lock(c, [ids["secret.jpg"]])
        m = other_client(c).get("/api/memories", params={"today": "2026-10-29"}).json()["memories"]
        assert m[0]["cover"] == ids["open.jpg"] and m[0]["count"] == 1


def test_locking_takes_an_item_out_of_its_stack(tmp_path):
    root = tmp_path / "P"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:01 10:00:01", color=(1, 2, 3))
    make_jpeg(root / "c.jpg", "2024:01:02 10:00:00", color=(9, 9, 9))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        st = c.post("/api/stacks", json={"ids": [ids["a.jpg"], ids["b.jpg"]], "cover": ids["a.jpg"]}).json()
        lock(c, [ids["a.jpg"]])
        o = other_client(c)
        b = next(i for i in o.get("/api/assets").json()["items"] if i["filename"] == "b.jpg")
        assert b["stack"] is None and b["stack_count"] == 0
        assert sorted(i["filename"] for i in o.get("/api/assets", params={"stacks": True}).json()["items"]) == \
            ["b.jpg", "c.jpg"]
        assert o.post(f"/api/stacks/{st['id']}/cover", json={"cover": ids["a.jpg"]}).status_code == 404


def test_changes_by_others_skip_locked_items(tmp_path):
    root = tmp_path / "P"
    make_jpeg(root / "secret.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "open.jpg", "2024:01:02 10:00:00", color=(1, 2, 3))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        s = ids_by_name(c)["secret.jpg"]
        album = c.post("/api/albums", json={"name": "A", "ids": [s]}).json()
        tag = c.post("/api/tags", json={"name": "t", "ids": [s]}).json()
        with db.session(cfg.db_path) as conn:
            conn.execute("INSERT INTO people (name, hidden, created_at) VALUES ('Bob', 0, 'x')")
            face = conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                                " VALUES (?, 1, 0, 0, .1, .1, 1, x'00', 'x')", (s,)).lastrowid
        lock(c, [s])
        o = other_client(c)
        o.post(f"/api/tags/{tag['id']}/remove", json={"ids": [s]})
        o.post(f"/api/albums/{album['id']}/remove", json={"ids": [s]})
        assert o.post(f"/api/faces/{face}/unassign").status_code == 404
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM asset_tags").fetchone()[0] == 1
            assert conn.execute("SELECT COUNT(*) FROM album_assets").fetchone()[0] == 1
            assert conn.execute("SELECT person_id FROM faces WHERE id = ?", (face,)).fetchone()[0] == 1


def test_activity_count_leaves_out_locked_items(tmp_path):
    root = tmp_path / "P"
    make_jpeg(root / "secret.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        s = ids_by_name(c)["secret.jpg"]
        o = other_client(c)
        o.post("/api/activity/seen")
        time.sleep(1.1)
        c.post("/api/albums", json={"name": "Private trip", "ids": [s]})
        lock(c, [s])
        assert o.get("/api/status").json()["activity_unread"] == 0
        assert o.get("/api/activity").json()["items"] == []


def test_uploading_a_locked_photo_reveals_nothing(tmp_path):
    root = tmp_path / "P"
    root.mkdir()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        data = jpeg_bytes((10, 20, 30))
        first = c.post("/api/upload", files=[("files", ("x.jpg", data, "image/jpeg"))]).json()["results"][0]
        lock(c, [first["id"]])
        again = other_client(c).post("/api/upload", files=[("files", ("guess.jpg", data, "image/jpeg"))]).json()
        assert again["results"][0]["status"] == "added" and again["results"][0]["id"] != first["id"]


def test_a_live_clip_found_later_is_locked_too(tmp_path):
    root = tmp_path / "P"
    make_jpeg(root / "IMG_0001.JPG", "2024:06:01 12:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        s = ids_by_name(c)["IMG_0001.JPG"]
        lock(c, [s])
        (root / "IMG_0001.MOV").write_bytes(mp4_bytes(b"secret live clip"))
        rescan(c)
        with db.session(cfg.db_path) as conn:
            clip = conn.execute("SELECT id, locked_by FROM assets WHERE filename = 'IMG_0001.MOV'").fetchone()
        assert clip["locked_by"] is not None
        o = other_client(c)
        assert o.get(f"/api/assets/{clip['id']}").status_code == 404
        assert o.get(f"/api/assets/{clip['id']}/original").status_code == 404


def test_events_notice_a_swap_of_locked_items(tmp_path):
    root = tmp_path / "P"
    for i in range(9):
        make_jpeg(root / f"p{i}.jpg", f"2024:03:01 10:0{i}:00", color=(i * 20, 5, 5))
    c, cfg = new_client(tmp_path, root, photo_checks=False)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        a, b = ids["p0.jpg"], ids["p4.jpg"]
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET sharpness = CASE WHEN id = ? THEN 999 ELSE 1 END", (b,))
        lock(c, [a])
        o = other_client(c)
        o.get("/api/events")
        c.post("/api/locked/open", json={"pin": "2468"})
        c.post("/api/locked", json={"ids": [b]})
        c.post("/api/locked", json={"ids": [a], "value": False})
        assert all(e["cover"] != b for e in o.get("/api/events").json()["events"])
