"""Fixes from the November 2026 bug review."""

import json
from datetime import datetime, timedelta

from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan

from snapshots import analysis, db, media, placeguess


def test_a_failed_video_probe_is_not_remembered(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    root.mkdir()
    (root / "old.avi").write_bytes(b"not really a video" * 50)
    answers = [{"codec": None, "duration": None, "playable": True, "probed": False},
               {"codec": "mpeg4", "duration": 1.0, "playable": False, "probed": True}]
    monkeypatch.setattr(media, "probe_video", lambda path: dict(answers[0]))
    monkeypatch.setattr(media, "has_ffmpeg", lambda: True)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        vid = ids_by_name(c)["old.avi"]
        # ffprobe failed (a slow drive, say): played as it is, but nothing is remembered...
        assert c.get(f"/api/assets/{vid}/play", params={"convert": "false"}).json()["codec"] is None
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT vcodec FROM assets WHERE id = ?", (vid,)).fetchone()[0] is None
            # ...and an answer remembered by an older version (no "probed") is looked at again.
            conn.execute("UPDATE assets SET vcodec = ? WHERE id = ?",
                         (json.dumps({"codec": None, "duration": None, "playable": True}), vid))
        answers.pop(0)
        monkeypatch.setattr(media, "probe_video", lambda path: dict(answers[0]))
        info = c.get(f"/api/assets/{vid}/play", params={"convert": "false"}).json()
        assert info["codec"] == "mpeg4"
        with db.session(cfg.db_path) as conn:
            assert json.loads(conn.execute("SELECT vcodec FROM assets WHERE id = ?", (vid,)).fetchone()[0])["codec"] \
                == "mpeg4"


def test_moving_burst_frames_apart_ends_the_burst(tmp_path):
    root = tmp_path / "Photos"
    for k in range(3):
        make_jpeg(root / f"b{k}.jpg", f"2024:05:01 10:00:0{k}", color=(120, 60 + k, 30))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/cleanup").json()["status"]["pending"] == 0)
        finder = analysis.DuplicateFinder(cfg)
        with db.session(cfg.db_path) as conn:
            before = finder.groups(conn)
            conn.execute("UPDATE assets SET taken_at = '2024-06-01T10:00:00' WHERE path = 'b1.jpg'")
            conn.execute("UPDATE assets SET taken_at = '2024-07-01T10:00:00' WHERE path = 'b2.jpg'")
            after = finder.groups(conn)
            fresh = analysis.DuplicateFinder(cfg).groups(conn)
        assert after == fresh          # the cache noticed the new dates
        assert len(after) <= len(before)


def test_renaming_someone_updates_cached_answers(tmp_path):
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    with db.session(cfg.db_path) as conn:
        conn.execute("INSERT INTO people (id, name, created_at) VALUES (1, 'Anna', 't')")
        before = db.people_version(conn)
        conn.execute("UPDATE people SET name = 'Emma' WHERE id = 1")      # same length
        renamed = db.people_version(conn)
        conn.execute("UPDATE people SET birthday = '--03-04' WHERE id = 1")
        assert len({before, renamed, db.people_version(conn)}) == 3


def test_merging_keeps_the_birthday(tmp_path):
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    with c:
        setup_admin(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("INSERT INTO people (id, name, birthday, created_at) VALUES (1, 'Anna', '1990-03-04', 't')")
            conn.execute("INSERT INTO people (id, name, created_at) VALUES (2, NULL, 't')")
        assert c.post("/api/people/1/merge", json={"into": 2}).status_code == 200
        with db.session(cfg.db_path) as conn:
            assert tuple(conn.execute("SELECT name, birthday FROM people WHERE id = 2").fetchone()) \
                == ("Anna", "1990-03-04")


def test_switching_guesses_off_removes_them(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:01 10:05:00", color=(10, 20, 30))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET lat = 1, lon = 2, place_guess = 'nearby', landmark = NULL WHERE id = ?",
                         (ids["a.jpg"],))
            conn.execute("UPDATE assets SET lat = 3, lon = 4, place_guess = 'landmark', landmark = 'Eiffel Tower',"
                         " landmark_done = 1 WHERE id = ?", (ids["b.jpg"],))
        c.put("/api/settings/sorting", json={"landmarks": False})
        with db.session(cfg.db_path) as conn:
            rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, lat, place_guess, landmark FROM assets")}
        assert rows[ids["b.jpg"]] == {"id": ids["b.jpg"], "lat": None, "place_guess": None, "landmark": None}
        assert rows[ids["a.jpg"]]["place_guess"] == "nearby"            # not from a landmark: kept
        c.put("/api/settings/sorting", json={"guess_places": False})
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM assets WHERE place_guess IS NOT NULL OR lat IS NOT NULL"
                                ).fetchone()[0] == 0


def test_a_place_guess_for_an_old_date_is_dropped(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    make_jpeg(root / "gps.jpg", "2024:01:01 10:00:00", gps=(33.4, -112.0))
    make_jpeg(root / "nogps.jpg", "2024:01:01 10:05:00", color=(10, 20, 30))
    c, cfg = new_client(tmp_path, root, guess_places=False)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
    job = placeguess.PlaceGuessJob(cfg)
    cfg.guess_places = True
    real = placeguess.neighbours

    def date_changes_meanwhile(conn, taken_at):
        found = real(conn, taken_at)
        # Someone moves the photo to another year while the job is looking (as change_details does).
        conn.execute("UPDATE assets SET taken_at = '2019-06-01T12:00:00', place_checked = 0 WHERE id = ?",
                     (ids["nogps.jpg"],))
        return found

    monkeypatch.setattr(placeguess, "neighbours", date_changes_meanwhile)
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE assets SET sort_done = 1")
        job.run_batch(conn, 0, lambda: False)
        row = conn.execute("SELECT lat, place_guess, place_checked FROM assets WHERE id = ?",
                           (ids["nogps.jpg"],)).fetchone()
    assert tuple(row) == (None, None, 0)      # no guess from the old date, and looked at again later


def test_birthdays_just_over_new_year():
    from snapshots.eventnames import near_birthday

    assert near_birthday("--01-02", "2024-12-30T10:00:00", "2024-12-31T18:00:00")
    assert near_birthday("1985-12-30", "2025-01-01T10:00:00", "2025-01-02T10:00:00")
    assert not near_birthday("--06-01", "2024-12-30T10:00:00", "2024-12-31T18:00:00")


def test_the_home_page_does_not_wait_for_events(tmp_path):
    root = tmp_path / "Photos"
    for k in range(4):
        make_jpeg(root / f"p{k}.jpg", (datetime(2020, 7, 1, 10) + timedelta(hours=k)).strftime("%Y:%m:%d %H:%M:%S"))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert c.get("/api/memories", params={"today": "2024-07-01"}).status_code == 200


def test_landmark_places_come_back_after_guessing_is_switched_on_again(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "tower.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET lat = 48.8, lon = 2.3, place_guess = 'landmark', landmark = 'Eiffel Tower',"
                         " landmark_done = 1")
        c.put("/api/settings/sorting", json={"guess_places": False})
        with db.session(cfg.db_path) as conn:
            # The place goes; the photo is looked at for landmarks again, so it returns when switched back on.
            assert tuple(conn.execute("SELECT lat, landmark_done FROM assets").fetchone()) == (None, 0)


def test_free_up_space_only_counts_files_not_in_the_trash(tmp_path):
    import hashlib

    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00", color=(10, 20, 30))
    sums = {n: hashlib.sha1((root / n).read_bytes()).hexdigest() for n in ("a.jpg", "b.jpg")}
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert c.post("/api/trash", json={"ids": [ids_by_name(c)["b.jpg"]]}).status_code == 200
        both = list(sums.values())
        # Backup: a file in the trash is still "there" (it is not sent again)...
        assert set(c.post("/api/upload/check", json={"sha1": both}).json()["have"]) == set(both)
        # ...but the phone may only remove its own copy of a file that is kept.
        r = c.post("/api/upload/check", json={"sha1": both, "kept": True}).json()
        assert r["have"] == [sums["a.jpg"]] and r["kept"] is True
