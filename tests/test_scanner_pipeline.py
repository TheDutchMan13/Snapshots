"""The scan reads each file with nothing locked, then stores it in a moment.

It used to hold the database's write lock while it read photos and made thumbnails (up to 25 files at a
time), so marking a favourite, logging in or a phone upload could wait many seconds during a scan.
"""

import shutil
import sqlite3

from snapshots import db
from snapshots import scanner as scanner_module
from snapshots.scanner import Scanner
from helpers import ids_by_name, make_jpeg, new_client, rescan, setup_admin, wait_scan


def photos(root, n, start=0):
    for i in range(start, start + n):
        make_jpeg(root / f"p{i}.jpg", f"2024:03:{i % 28 + 1:02d} 10:00:00", color=(i * 9 % 255, 80, 160))


def test_nothing_is_locked_while_a_file_is_read(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    photos(root, 8)
    c, cfg = new_client(tmp_path, root, index_on_start=False, scan_threads=1)   # one at a time: read, store, read
    with c:
        setup_admin(c)
        real = scanner_module.prepare
        locked = []

        def prepare(*a, **kw):
            # While a file is read, someone else writes (a favourite, say). It must not have to wait.
            other = sqlite3.connect(str(cfg.db_path), timeout=0)
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("COMMIT")
                locked.append(False)
            except sqlite3.OperationalError:
                locked.append(True)
            finally:
                other.close()
            return real(*a, **kw)

        monkeypatch.setattr(scanner_module, "prepare", prepare)
        s = rescan(c)
        assert s["added"] == 8 and s["errors"] == 0
        assert len(locked) == 8 and not any(locked)

        # the same holds for changed files on a later scan
        locked.clear()
        for i in range(8):
            make_jpeg(root / f"p{i}.jpg", "2025:01:01 10:00:00", color=(1, i * 20, 3), size=(700, 500))
        s = rescan(c)
        assert s["updated"] == 8 and len(locked) == 8 and not any(locked)


def test_no_temporary_thumbnails_are_left_behind(tmp_path):
    root = tmp_path / "Photos"
    photos(root, 5)
    (root / "broken.jpg").write_bytes(b"not a picture")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        assert len(ids) == 6
        for name, asset_id in ids.items():
            assert cfg.thumb_path(asset_id).exists() == (name != "broken.jpg")
        assert list((cfg.thumb_dir / "new").iterdir()) == []


def test_old_unfinished_thumbnails_are_cleared(tmp_path):
    import os
    import time

    root = tmp_path / "Photos"
    photos(root, 1)
    c, cfg = new_client(tmp_path, root, index_on_start=False)
    with c:
        setup_admin(c)
        new = cfg.thumb_dir / "new"
        new.mkdir(parents=True, exist_ok=True)
        (new / "old.jpg").write_bytes(b"x")
        (new / "fresh.jpg").write_bytes(b"x")      # may belong to an upload being stored right now
        os.utime(new / "old.jpg", (time.time() - 7200, time.time() - 7200))
        rescan(c)
        assert sorted(p.name for p in new.iterdir()) == ["fresh.jpg"]


def test_a_moved_photo_and_a_new_copy_of_it(tmp_path):
    """One known photo is gone and two files with its content appear: one is the photo, moved (it keeps
    its album); the other is a new item, read in full."""
    root = tmp_path / "Photos"
    photos(root, 3)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = ids_by_name(c)["p0.jpg"]
        c.post("/api/albums", json={"name": "Trip", "ids": [first]})
        shutil.copy2(root / "p0.jpg", root / "copy.jpg")
        (root / "p0.jpg").rename(root / "renamed.jpg")
        s = rescan(c)
        assert (s["moved"], s["added"], s["removed"], s["errors"]) == (1, 1, 0, 0)
        ids = ids_by_name(c)
        assert sorted(ids) == ["copy.jpg", "p1.jpg", "p2.jpg", "renamed.jpg"]
        assert first in (ids["copy.jpg"], ids["renamed.jpg"])
        assert c.get("/api/albums").json()["albums"][0]["count"] == 1
        conn = db.connect(cfg.db_path)
        rows = conn.execute("SELECT has_thumb, width, taken_at FROM assets WHERE filename IN ('copy.jpg', 'renamed.jpg')").fetchall()
        conn.close()
        assert [tuple(r) for r in rows] == [(1, 640, "2024-03-01T10:00:00")] * 2
        assert all(cfg.thumb_path(i).exists() for i in ids.values())


def test_an_unreadable_file_is_counted_and_the_rest_are_indexed(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    photos(root, 4)
    c, cfg = new_client(tmp_path, root, index_on_start=False)
    with c:
        setup_admin(c)
        real = scanner_module.sha1_of

        def sha1_of(path):
            if path.name == "p2.jpg":
                raise OSError("disk error")
            return real(path)

        monkeypatch.setattr(scanner_module, "sha1_of", sha1_of)
        s = rescan(c)
        assert (s["added"], s["errors"], s["done"], s["total"]) == (3, 1, 4, 4)
        assert sorted(ids_by_name(c)) == ["p0.jpg", "p1.jpg", "p3.jpg"]


def test_a_changed_file_keeps_what_was_added_by_hand_and_gets_a_new_thumbnail(tmp_path):
    root = tmp_path / "Photos"
    photos(root, 2)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        asset_id = ids_by_name(c)["p0.jpg"]
        c.post("/api/albums", json={"name": "Trip", "ids": [asset_id]})
        c.post("/api/assets/details", json={"ids": [asset_id], "description": "Kept"})
        before = cfg.thumb_path(asset_id).read_bytes()
        make_jpeg(root / "p0.jpg", "2025:06:01 10:00:00", color=(250, 250, 0), size=(900, 600))
        s = rescan(c)
        assert (s["updated"], s["added"]) == (1, 0)
        item = c.get(f"/api/assets/{asset_id}").json()
        assert item["description"] == "Kept" and item["width"] == 900
        assert c.get("/api/albums").json()["albums"][0]["count"] == 1
        assert cfg.thumb_path(asset_id).read_bytes() != before


def test_scanner_alone_gives_the_same_library(tmp_path):
    root = tmp_path / "Photos"
    photos(root, 12)
    from snapshots.config import Config

    cfg = Config(library_path=root, data_dir=tmp_path / "data", faces=False, places=False)
    cfg.ensure_dirs()
    db.init(cfg.db_path)
    s = Scanner(cfg).run()
    assert (s.added, s.errors, s.done) == (12, 0, 12)
    conn = db.connect(cfg.db_path)
    rows = conn.execute("SELECT path, has_thumb FROM assets ORDER BY id").fetchall()
    conn.close()
    assert sorted(r["path"] for r in rows) == sorted(f"p{i}.jpg" for i in range(12))
    assert all(r["has_thumb"] for r in rows)


def test_with_several_threads_a_writer_never_waits_for_a_file_to_be_read(tmp_path, monkeypatch):
    import threading
    import time

    root = tmp_path / "Photos"
    photos(root, 16)
    c, cfg = new_client(tmp_path, root, index_on_start=False, scan_threads=4)
    with c:
        setup_admin(c)
        real = scanner_module.prepare
        waits, failed, names = [], [], set()

        def prepare(*a, **kw):
            names.add(threading.current_thread().name)
            time.sleep(0.05)                    # a file that takes a while to read
            other = sqlite3.connect(str(cfg.db_path), timeout=10)
            t = time.monotonic()
            try:
                other.execute("BEGIN IMMEDIATE")
                other.execute("COMMIT")
                waits.append(time.monotonic() - t)
            except sqlite3.OperationalError:
                failed.append(a[1])
            finally:
                other.close()
            return real(*a, **kw)

        monkeypatch.setattr(scanner_module, "prepare", prepare)
        s = rescan(c)
        assert (s["added"], s["errors"], s["done"]) == (16, 0, 16)
        assert not failed and len(waits) == 16
        # rows that are ready are stored together for a moment (_COMMIT_SECONDS): a writer waits about
        # that long at most, never for the reading of a file
        assert max(waits) < 2
        assert len(names) > 1 and all(n.startswith("scan") for n in names)
        ids = ids_by_name(c)
        assert len(ids) == 16 and all(cfg.thumb_path(i).exists() for i in ids.values())
        assert list((cfg.thumb_dir / "new").iterdir()) == []


def test_files_are_stored_in_the_order_they_were_found_whatever_the_threads(tmp_path):
    from snapshots.config import Config

    root = tmp_path / "Photos"
    photos(root, 30)
    orders = []
    for threads in (1, 5):
        cfg = Config(library_path=root, data_dir=tmp_path / f"data{threads}", faces=False, places=False,
                     scan_threads=threads)
        cfg.ensure_dirs()
        db.init(cfg.db_path)
        s = Scanner(cfg).run()
        assert (s.added, s.errors) == (30, 0)
        conn = db.connect(cfg.db_path)
        orders.append([tuple(r) for r in conn.execute(
            "SELECT path, sha1, taken_at, width, height, has_thumb FROM assets ORDER BY id")])
        conn.close()
    assert orders[0] == orders[1]


def test_stopping_early_leaves_nothing_that_was_read_ahead(tmp_path):
    from snapshots.config import Config
    from snapshots.scanner import _Job

    root = tmp_path / "Photos"
    photos(root, 12)
    cfg = Config(library_path=root, data_dir=tmp_path / "data", scan_threads=3)
    cfg.ensure_dirs()
    db.init(cfg.db_path)
    jobs = [_Job(i + 1, f"p{i}.jpg", root / f"p{i}.jpg", None, "index") for i in range(12)]
    reader = Scanner(cfg)._read_all(iter(jobs), frozenset())
    job, first, _ = next(reader)
    assert job.number == 1 and first.thumb.exists()
    first.discard()
    reader.close()                              # the scan stops here
    assert list((cfg.thumb_dir / "new").iterdir()) == []


def test_scan_threads_setting(monkeypatch):
    import os

    from snapshots.config import Config
    from snapshots.scanner import MAX_AUTO_THREADS, scan_threads

    assert scan_threads(Config(scan_threads=3)) == 3
    for cores, expected in ((1, 1), (2, 2), (4, 3), (8, MAX_AUTO_THREADS), (32, MAX_AUTO_THREADS), (None, 1)):
        monkeypatch.setattr(os, "cpu_count", lambda cores=cores: cores)
        assert scan_threads(Config()) == expected


def test_a_scan_cut_short_by_closing_tidies_nothing_away_and_the_next_one_finishes(tmp_path, monkeypatch):
    from snapshots.config import Config

    root = tmp_path / "Photos"
    photos(root, 20)
    cfg = Config(library_path=root, data_dir=tmp_path / "data", faces=False, places=False, scan_threads=2)
    cfg.ensure_dirs()
    db.init(cfg.db_path)
    scanner = Scanner(cfg)
    assert scanner.run().added == 20

    # 5 photos are deleted and 10 are new; Snapshots is closed while the scan is at work
    for i in range(5):
        (root / f"p{i}.jpg").unlink()
    photos(root, 10, start=100)
    real = scanner_module.prepare
    seen = []

    def prepare(*a, **kw):
        seen.append(a[1])
        if len(seen) == 3:
            scanner.stopping.set()
        return real(*a, **kw)

    monkeypatch.setattr(scanner_module, "prepare", prepare)
    s = scanner.run()
    assert s.added < 10 and (s.removed, s.forgotten, s.errors) == (0, 0, 0)
    conn = db.connect(cfg.db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE missing_since IS NOT NULL").fetchone()[0] == 0
        assert list((cfg.thumb_dir / "new").iterdir()) == []

        scanner.stopping.clear()                # started again
        s = scanner.run()
        assert s.removed == 5 and s.errors == 0
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE missing_since IS NULL").fetchone()[0] == 25
    finally:
        conn.close()
