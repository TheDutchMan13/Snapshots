"""Turning photos by hand, in bulk (for the ones Snapshots can't tell are on their side)."""

import io

from PIL import Image

from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_scan


def thumb_size(c, asset_id):
    return Image.open(io.BytesIO(c.get(f"/api/assets/{asset_id}/thumb").content)).size


def test_photos_are_turned_on_top_of_what_they_have_and_originals_stay(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 09:30:00", size=(640, 480))
    make_jpeg(root / "b.jpg", "2024:05:02 09:30:00", size=(640, 480), color=(9, 9, 9))
    before = (root / "a.jpg").read_bytes()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        a, b = ids["a.jpg"], ids["b.jpg"]
        assert thumb_size(c, a)[0] > thumb_size(c, a)[1]

        r = c.post("/api/assets/turn", json={"ids": [a, b], "degrees": 90})
        assert r.status_code == 200 and sorted(r.json()["turned"]) == sorted([a, b]) and r.json()["skipped"] == 0
        assert thumb_size(c, a)[1] > thumb_size(c, a)[0]
        assert c.get(f"/api/assets/{a}").json()["edits"]["rotate"] == 90

        # a second turn adds to the first, a crop is dropped, and a flip is kept
        c.post(f"/api/assets/{b}/edits", json={"edits": {"rotate": 90, "flip": True, "crop": [0, 0, 0.5, 0.5]}})
        assert c.post("/api/assets/turn", json={"ids": [a, b], "degrees": 270}).status_code == 200
        assert c.get(f"/api/assets/{a}").json()["edits"] is None             # a full turn: nothing left to apply
        edits_b = c.get(f"/api/assets/{b}").json()["edits"]
        assert edits_b["rotate"] == 0 and edits_b["flip"] is True and edits_b["crop"] is None

        assert (root / "a.jpg").read_bytes() == before                       # the file is not touched


def test_turning_refuses_nonsense_and_leaves_out_what_it_cannot_turn(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 09:30:00", size=(640, 480))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        a = ids_by_name(c)["a.jpg"]
        assert c.post("/api/assets/turn", json={"ids": [a], "degrees": 45}).status_code == 422
        r = c.post("/api/assets/turn", json={"ids": [a, 999999], "degrees": 180})
        assert r.json()["turned"] == [a] and r.json()["skipped"] == 1
        (root / "a.jpg").unlink()
        r = c.post("/api/assets/turn", json={"ids": [a], "degrees": 90})
        assert r.json()["turned"] == [] and r.json()["skipped"] == 1


def test_a_new_import_is_not_refused_while_the_last_one_is_still_being_recorded(tmp_path):
    """The state says 'not running' a moment before the old import's thread has finished writing its record."""
    import threading
    import time

    from snapshots import db, imports

    class Stuck:
        """A worker whose poke() waits: the old import's thread cannot finish until it is let go."""

        def __init__(self):
            self.release = threading.Event()
            self.faces = type("F", (), {"invalidate": staticmethod(lambda: None)})()

        def poke(self):
            self.release.wait(10)

    cfg = type("Cfg", (), {"db_path": tmp_path / "x.db"})()
    db.init(cfg.db_path)
    worker = Stuck()
    manager = imports.Manager(cfg, worker)
    assert manager.start("immich", lambda m: {"files": 0}, 1, dry_run=False)
    deadline = time.time() + 5
    while manager.snapshot().get("running") and time.time() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)                                  # the old thread is now waiting in poke()
    assert manager.thread.is_alive() and not manager.snapshot()["running"]
    assert manager.start("immich", lambda m: {"files": 0}, 1, dry_run=True)       # not refused
    worker.release.set()
