"""Files that go missing from a folder that is there, and come back.

A scan used to forget such a file at once, with its albums, favourites, description and faces: a folder
deleted by mistake and put back from the Recycle Bin lost all of that. Now the record is set aside (hidden
everywhere) and kept for keep_missing_days.
"""

import shutil
import sqlite3
from datetime import datetime, timedelta

import pytest

from snapshots import db
from helpers import ids_by_name, jpeg_bytes, make_jpeg, new_client, rescan, setup_admin, wait_scan


def library(tmp_path, n=10, **kw):
    """n photos: the first 3 in "trip", the rest in "rest". The trip ones are in an album, favourites and described."""
    root = tmp_path / "Photos"
    for i in range(n):
        make_jpeg(root / ("trip" if i < 3 else "rest") / f"p{i}.jpg", f"2024:05:{i + 1:02d} 10:00:00",
                  color=(i * 20, 255 - i * 20, i))
    c, cfg = new_client(tmp_path, root, **kw)
    return c, cfg, root


def mark(c):
    ids = ids_by_name(c)
    trip = [ids[f"p{i}.jpg"] for i in range(3)]
    album = c.post("/api/albums", json={"name": "Trip", "ids": trip}).json()
    assert c.post("/api/favorites", json={"ids": trip, "value": True}).status_code == 200
    assert c.post("/api/assets/details", json={"ids": trip[:1], "description": "The best day"}).status_code == 200
    return trip, album["id"]


def album_count(c):
    return c.get("/api/albums").json()["albums"][0]["count"]


def favourites(c):
    return len(c.get("/api/assets", params={"favorite": "true"}).json()["items"])


def test_a_folder_deleted_and_put_back_keeps_albums_favourites_and_descriptions(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        trip, _ = mark(c)

        shutil.move(root / "trip", tmp_path / "recycle-bin")
        s = rescan(c)
        assert (s["removed"], s["forgotten"]) == (3, 0) and "kept for 30 days" in s["message"]
        # set aside: shown nowhere, counted nowhere
        st = c.get("/api/status").json()
        assert st["photos"] == 7 and st["set_aside"] == 3
        assert len(ids_by_name(c)) == 7 and album_count(c) == 0 and favourites(c) == 0
        assert c.get("/api/timeline").status_code == 200

        s = rescan(c)   # still gone: nothing more happens to them
        assert (s["removed"], s["forgotten"], s["restored"]) == (0, 0, 0)

        shutil.move(tmp_path / "recycle-bin", root / "trip")
        s = rescan(c)
        assert (s["restored"], s["added"], s["removed"]) == (3, 0, 0)
        assert c.get("/api/status").json()["set_aside"] == 0
        ids = ids_by_name(c)
        assert [ids[f"p{i}.jpg"] for i in range(3)] == trip            # the same items, not new ones
        assert album_count(c) == 3 and favourites(c) == 3
        assert c.get(f"/api/assets/{trip[0]}").json()["description"] == "The best day"


def test_a_missing_file_that_comes_back_somewhere_else_keeps_everything(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        trip, _ = mark(c)
        shutil.move(root / "trip", tmp_path / "elsewhere")
        assert rescan(c)["removed"] == 3
        shutil.move(tmp_path / "elsewhere", root / "2024" / "holiday")   # back under another name
        s = rescan(c)
        assert (s["restored"], s["added"]) == (3, 0)
        assert album_count(c) == 3 and favourites(c) == 3
        assert c.get(f"/api/assets/{trip[0]}").json()["path"] == "2024/holiday/p0.jpg"


def test_set_aside_files_are_forgotten_when_their_time_is_up(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        mark(c)
        shutil.rmtree(root / "trip")
        assert rescan(c)["removed"] == 3
        conn = db.connect(cfg.db_path)
        try:
            long_ago = (datetime.now() - timedelta(days=29)).isoformat(timespec="seconds")
            conn.execute("UPDATE assets SET missing_since = ? WHERE missing_since IS NOT NULL", (long_ago,))
            conn.commit()
            assert rescan(c)["forgotten"] == 0           # 29 days: still kept
            long_ago = (datetime.now() - timedelta(days=31)).isoformat(timespec="seconds")
            conn.execute("UPDATE assets SET missing_since = ? WHERE missing_since IS NOT NULL", (long_ago,))
            conn.commit()
            assert rescan(c)["forgotten"] == 3
            assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 7
            assert conn.execute("SELECT COUNT(*) FROM album_assets").fetchone()[0] == 0
        finally:
            conn.close()


def test_keep_missing_days_zero_forgets_at_once(tmp_path):
    c, cfg, root = library(tmp_path, keep_missing_days=0)
    with c:
        setup_admin(c)
        wait_scan(c)
        (root / "rest" / "p5.jpg").unlink()
        s = rescan(c)
        assert s["removed"] == 1 and c.get("/api/status").json()["set_aside"] == 0
        conn = db.connect(cfg.db_path)
        assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 9
        conn.close()


def test_a_set_aside_file_is_not_forgotten_while_its_drive_is_away(tmp_path):
    c, cfg, root = library(tmp_path)
    extra = tmp_path / "Drive"
    for i in range(4):
        make_jpeg(extra / f"d{i}.jpg", f"2023:01:{i + 1:02d} 10:00:00", color=(9, i * 40, 200))
    cfg.extra_libraries = [extra]
    with c:
        setup_admin(c)
        wait_scan(c)
        (extra / "d0.jpg").rename(tmp_path / "d0-away.jpg")
        assert rescan(c)["removed"] == 1
        conn = db.connect(cfg.db_path)
        try:
            conn.execute("UPDATE assets SET missing_since = '2000-01-01T00:00:00' WHERE missing_since IS NOT NULL")
            conn.commit()
            shutil.move(extra, tmp_path / "unplugged")          # the drive is taken away
            s = rescan(c)
            assert (s["forgotten"], s["removed"]) == (0, 0)
            assert conn.execute("SELECT COUNT(*) FROM assets WHERE missing_since IS NOT NULL").fetchone()[0] == 1
        finally:
            conn.close()


def test_a_different_file_with_a_set_aside_files_name_is_a_new_item(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        trip, _ = mark(c)
        (root / "trip" / "p0.jpg").unlink()
        assert rescan(c)["removed"] == 1
        make_jpeg(root / "trip" / "p0.jpg", "2025:01:01 09:00:00", color=(1, 2, 3), size=(800, 600))
        s = rescan(c)
        assert (s["added"], s["restored"], s["updated"]) == (1, 0, 0)
        assert album_count(c) == 2 and favourites(c) == 2           # it is not in the old one's album
        new = c.get(f"/api/assets/{ids_by_name(c)['p0.jpg']}").json()
        assert not new.get("description")


def test_uploading_a_set_aside_photo_again_brings_it_back(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        trip, _ = mark(c)
        data = (root / "trip" / "p0.jpg").read_bytes()
        sha = __import__("hashlib").sha1(data).hexdigest()
        assert c.post("/api/upload/check", json={"sha1": [sha]}).json()["have"] == [sha]
        (root / "trip" / "p0.jpg").unlink()
        assert rescan(c)["removed"] == 1
        # the library no longer has the file, and says so: the phone sends it again and must not free it up
        assert c.post("/api/upload/check", json={"sha1": [sha]}).json()["have"] == []
        assert c.post("/api/upload/check", json={"sha1": [sha], "kept": True}).json()["have"] == []
        r = c.post("/api/upload", files=[("files", ("p0.jpg", data, "image/jpeg"))]).json()["results"][0]
        assert (r["status"], r["id"]) == ("added", trip[0])           # the same item, with what it had
        assert album_count(c) == 3 and favourites(c) == 3
        item = c.get(f"/api/assets/{trip[0]}").json()
        assert item["path"].startswith("Uploads/") and item["description"] == "The best day"
        assert (root / item["path"]).read_bytes() == data
        assert c.post("/api/upload/check", json={"sha1": [sha], "kept": True}).json()["have"] == [sha]
        s = rescan(c)
        assert (s["added"], s["removed"], s["restored"]) == (0, 0, 0)


def test_an_upload_never_takes_over_a_set_aside_item_in_a_locked_folder(tmp_path):
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        data = (root / "rest" / "p5.jpg").read_bytes()
        locked_id = ids_by_name(c)["p5.jpg"]
        conn = db.connect(cfg.db_path)
        try:
            admin = conn.execute("SELECT id FROM users").fetchone()[0]
            conn.execute("UPDATE assets SET locked_by = ? WHERE id = ?", (admin, locked_id))
            conn.commit()
            (root / "rest" / "p5.jpg").unlink()
            rescan(c)
            r = c.post("/api/upload", files=[("files", ("p5.jpg", data, "image/jpeg"))]).json()["results"][0]
            assert r["status"] == "added" and r["id"] != locked_id
            assert conn.execute("SELECT locked_by FROM assets WHERE id = ?", (r["id"],)).fetchone()[0] is None
        finally:
            conn.close()


def test_set_aside_files_do_not_count_towards_most_of_the_library_vanished(tmp_path):
    root = tmp_path / "Photos"
    for i in range(60):
        make_jpeg(root / ("a" if i < 25 else "b") / f"p{i}.jpg", f"2024:01:{i % 28 + 1:02d} 10:00:00",
                  color=(i * 4, 50, 100))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        shutil.rmtree(root / "a")           # 25 of 60: less than half, set aside
        assert rescan(c)["removed"] == 25
        shutil.move(root / "b", tmp_path / "b-away")    # then 35 of the 35 that are left: kept, not set aside
        make_jpeg(root / "one.jpg", "2024:02:02 10:00:00")      # (an empty folder is never cleaned up at all)
        s = rescan(c)
        assert (s["removed"], s["skipped_removals"]) == (0, 35) and "were kept" in s["message"]
        assert c.get("/api/status").json()["set_aside"] == 25


# ------------------------------------------------------------------ the database upgrade

def old_database(path, version=12):
    conn = sqlite3.connect(path)
    for n in range(version):
        conn.executescript(f"BEGIN;\n{db.MIGRATIONS[n]}\nPRAGMA user_version = {n + 1};\nCOMMIT;")
    conn.execute(
        "INSERT INTO assets (path, filename, kind, size, mtime_ns, sha1, taken_at, has_thumb, added_at)"
        " VALUES ('old.jpg', 'old.jpg', 'photo', 1, 1, 'abc', '2020-01-01T00:00:00', 0, '2020-01-01T00:00:00')")
    conn.execute("INSERT INTO albums (name, created_at, updated_at) VALUES ('Trip', '2020-01-01T00:00:00', '2020-01-01T00:00:00')")
    conn.execute("INSERT INTO album_assets (album_id, asset_id, added_at) VALUES (1, 1, '2020-01-01T00:00:00')")
    conn.commit()
    conn.close()


def test_upgrading_copies_the_database_first(tmp_path):
    path = tmp_path / "data" / "snapshots.db"
    path.parent.mkdir()
    old_database(path)
    db.init(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert tuple(conn.execute("SELECT path, missing_since FROM assets").fetchone()) == ("old.jpg", None)
    assert conn.execute("SELECT COUNT(*) FROM album_assets").fetchone()[0] == 1
    conn.close()

    copies = list((path.parent / "backups").glob("snapshots-before-upgrade-v12-*.db"))
    assert len(copies) == 1
    copy = sqlite3.connect(copies[0])           # the copy is the database as it was: whole, and still version 12
    assert copy.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert copy.execute("PRAGMA user_version").fetchone()[0] == 12
    assert copy.execute("SELECT path FROM assets").fetchone()[0] == "old.jpg"
    assert "missing_since" not in [r[1] for r in copy.execute("PRAGMA table_info(assets)")]
    copy.close()

    db.init(path)                               # up to date: no second copy
    assert len(list((path.parent / "backups").iterdir())) == 1


def test_a_new_database_needs_no_copy(tmp_path):
    db.init(tmp_path / "new.db")
    assert not (tmp_path / "backups").exists()


def test_the_upgrade_copy_goes_to_the_chosen_backups_folder_or_beside_the_database(tmp_path):
    path = tmp_path / "snapshots.db"
    old_database(path)
    db.init(path, tmp_path / "elsewhere")
    assert len(list((tmp_path / "elsewhere").glob("snapshots-before-upgrade-v12-*.db"))) == 1

    path2 = tmp_path / "two" / "snapshots.db"
    path2.parent.mkdir()
    old_database(path2)
    blocked = tmp_path / "a-file"               # the chosen folder can't be used: the copy goes beside the database
    blocked.write_text("x")
    db.init(path2, blocked / "backups")
    assert len(list((path2.parent / "backups").glob("snapshots-before-upgrade-v12-*.db"))) == 1


def test_no_copy_means_no_upgrade(tmp_path, monkeypatch):
    path = tmp_path / "snapshots.db"
    old_database(path)

    def fail(*a, **kw):
        raise OSError("disk full")

    monkeypatch.setattr(db, "backup_before_upgrade", fail)
    with pytest.raises(RuntimeError, match="nothing was changed"):
        db.init(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 12       # untouched
    conn.close()


def test_only_the_newest_upgrade_copies_are_kept(tmp_path):
    folder = tmp_path / "backups"
    folder.mkdir()
    for n in range(5):
        (folder / f"snapshots-before-upgrade-v{n + 5}-2026010{n + 1}-000000.db").write_text("old")
    (folder / "snapshots-20260101-000000.db").write_text("a timed backup")
    path = tmp_path / "snapshots.db"
    old_database(path)
    db.init(path)
    names = sorted(p.name for p in folder.iterdir())
    assert len([n for n in names if "before-upgrade" in n]) == db.UPGRADE_BACKUPS_KEPT
    assert any("before-upgrade-v12-" in n for n in names) and "snapshots-20260101-000000.db" in names


def test_emptying_the_trash_leaves_a_set_aside_items_name_alone(tmp_path):
    """A trashed file that then went missing is hidden from the trash. A new file with its name is another
    photo: emptying the trash must not delete it."""
    c, cfg, root = library(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        old = ids_by_name(c)["p5.jpg"]
        assert c.post("/api/trash", json={"ids": [old]}).status_code == 200
        (root / "rest" / "p5.jpg").unlink()
        rescan(c)
        assert c.get("/api/assets", params={"trashed": "true"}).json()["items"] == []
        # Another photo is saved under that name, and the trash is emptied before a scan has seen it.
        make_jpeg(root / "rest" / "p5.jpg", "2025:02:02 09:00:00", color=(9, 9, 9), size=(640, 480))
        for body in ({"confirm": True}, {"confirm": True, "ids": [old]}):
            r = c.post("/api/trash/empty", json=body)
            assert r.status_code == 200 and r.json()["deleted"] == 0
            assert (root / "rest" / "p5.jpg").is_file()
        s = rescan(c)
        assert s["added"] == 1 and "p5.jpg" in ids_by_name(c)
