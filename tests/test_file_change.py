"""Writing an edit into the photo's own file (filechange.py): what it keeps, and everything it refuses.

Every file here is in a temporary folder; nothing outside it is touched.
"""

import hashlib
import io
import os
import time
from pathlib import Path

import pytest
from PIL import Image

from snapshots import db, filechange
from helpers import (ADMIN, add_member, ids_by_name, login, make_jpeg, new_client, rescan, setup_admin, wait_for,
                     wait_scan)


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def setup(tmp_path, **kw):
    """A library with one wide photo (with date and GPS in it) and a server on it, an administrator signed in."""
    root = tmp_path / "Photos"
    make_jpeg(root / "2024" / "wide.jpg", "2024:05:01 09:30:00", size=(640, 480), gps=(33.45, -112.07),
              camera=("Canon", "EOS"))
    c, cfg = new_client(tmp_path, root, **kw)
    return root, c, cfg


def edit(c, asset_id, **recipe):
    r = c.post(f"/api/assets/{asset_id}/edits", json={"edits": recipe})
    assert r.status_code == 200, r.text
    return r.json()


def kept_files(cfg, asset_id):
    folder = cfg.original_files_dir / str(asset_id)
    return sorted(folder.iterdir()) if folder.is_dir() else []


def test_the_edit_is_written_into_the_file_and_the_old_file_is_kept(tmp_path):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        c.post("/api/favorites", json={"ids": [wide], "value": True})
        album = c.post("/api/albums", json={"name": "Trip", "ids": [wide]}).json()
        st = original.stat()
        edit(c, wide, rotate=90)

        r = c.post(f"/api/assets/{wide}/apply-edits")
        assert r.status_code == 200, r.text
        out = r.json()

        # The file itself is turned now, and still has its date, camera and place.
        with Image.open(original) as im:
            assert im.size == (480, 640)
            exif = im.getexif()
            assert exif.get(274) in (None, 1)
            assert exif.get_ifd(0x8769).get(36867) == "2024:05:01 09:30:00"
            assert exif.get(271) == "Canon"
            assert exif.get_ifd(0x8825).get(2) is not None
        assert os.stat(original).st_mtime_ns == st.st_mtime_ns          # the file's own date stays

        # The same item, with what people gave it, and no recipe left to apply a second time.
        assert out["id"] == wide and out["favorite"] and not out["edited"]
        assert (out["width"], out["height"]) == (480, 640)
        detail = c.get(f"/api/assets/{wide}").json()
        assert detail["edits"] is None and detail["taken_at"].startswith("2024-05-01T09:30")
        assert [a["name"] for a in detail["albums"]] == ["Trip"]
        assert c.get(f"/api/albums/{album['id']}").json()["count"] == 1
        thumb = Image.open(io.BytesIO(c.get(f"/api/assets/{wide}/thumb").content))
        assert thumb.height > thumb.width                                 # the thumbnail is of the turned picture

        # The old file is kept, byte for byte, outside the library.
        files = kept_files(cfg, wide)
        assert len(files) == 1 and files[0].read_bytes() == before
        assert detail["file_backups"][0]["size"] == len(before)
        assert not list((root / "2024").glob(".*"))                       # no half-written file left beside it
        assert c.get("/api/storage?fresh=true").json()["data"]["kept_files"] == len(before)


def test_putting_the_old_file_back_is_exact_and_can_be_undone(tmp_path):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)
        c.post(f"/api/assets/{wide}/apply-edits")
        turned = original.read_bytes()

        r = c.post(f"/api/assets/{wide}/restore-file", json={})
        assert r.status_code == 200, r.text
        assert original.read_bytes() == before                            # exactly the file it was
        assert (r.json()["width"], r.json()["height"]) == (640, 480)
        # What was in the file a moment ago is kept as well, so putting back can itself be undone.
        assert sorted(f.read_bytes() for f in kept_files(cfg, wide)) == sorted([before, turned])
        copies = c.get(f"/api/assets/{wide}").json()["file_backups"]
        assert len(copies) == 2 and sum(1 for x in copies if x["restored"]) == 1

        newest_unrestored = [x for x in copies if not x["restored"]][0]["id"]
        r = c.post(f"/api/assets/{wide}/restore-file", json={"backup": newest_unrestored})
        assert r.status_code == 200 and original.read_bytes() == turned


def test_nothing_is_changed_when_the_file_is_not_the_one_that_was_looked_at(tmp_path):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)
        make_jpeg(original, "2024:05:01 09:30:00", size=(800, 600), color=(1, 1, 1))   # someone saved over it
        other = original.read_bytes()
        r = c.post(f"/api/assets/{wide}/apply-edits")
        assert r.status_code == 409 and "changed outside Snapshots" in r.json()["detail"]
        assert original.read_bytes() == other and kept_files(cfg, wide) == []


def test_only_administrators_and_only_edited_photos(tmp_path):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        assert c.post(f"/api/assets/{wide}/apply-edits").status_code == 400      # no edit to write
        edit(c, wide, rotate=90)
        add_member(c, "member")
        c.post("/api/auth/logout")
        login(c, "member", "member password 1")
        assert c.post(f"/api/assets/{wide}/apply-edits").status_code == 403
        assert c.post(f"/api/assets/{wide}/restore-file", json={}).status_code == 403
        assert original.read_bytes() == before and kept_files(cfg, wide) == []


@pytest.mark.parametrize("what", ["png-alpha", "motion", "trash", "raw-name"])
def test_formats_and_states_that_would_lose_something_are_refused(tmp_path, what):
    root, c, cfg = setup(tmp_path)
    if what == "png-alpha":
        Image.new("RGBA", (200, 100), (10, 20, 30, 128)).save(root / "2024" / "clear.png")
    if what == "raw-name":
        make_jpeg(root / "2024" / "shot.jpg", "2024:06:01 10:00:00", color=(3, 3, 3))
        (root / "2024" / "shot.jpg").rename(root / "2024" / "shot.heic")
    with c:
        setup_admin(c)
        wait_scan(c)
        name = {"png-alpha": "clear.png", "raw-name": "shot.heic"}.get(what, "wide.jpg")
        ids = ids_by_name(c)
        if name not in ids:       # a .heic that Pillow can't read may not be listed: nothing more to test then
            pytest.skip("not indexed on this machine")
        item = ids[name]
        path = next(root.rglob(name))
        before = path.read_bytes()
        edit(c, item, rotate=90)
        if what == "motion":
            with db.session(cfg.db_path) as conn:
                conn.execute("UPDATE assets SET motion_offset = 100, motion_length = 5 WHERE id = ?", (item,))
        if what == "trash":
            assert c.post("/api/trash", json={"ids": [item]}).status_code == 200
        r = c.post(f"/api/assets/{item}/apply-edits")
        assert r.status_code in (400, 409, 415, 404), r.text
        assert path.read_bytes() == before and kept_files(cfg, item) == []


def test_a_failed_write_leaves_the_file_and_the_kept_copies_as_they_were(tmp_path, monkeypatch):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)

        def boom(*a, **k):
            raise OSError("disk full, say")

        monkeypatch.setattr(filechange, "_write_picture", boom)
        r = c.post(f"/api/assets/{wide}/apply-edits")
        assert r.status_code == 500 and "Nothing was changed" in r.json()["detail"]
        assert original.read_bytes() == before
        assert kept_files(cfg, wide) == [] and not list((root / "2024").glob(".*"))
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM file_backups").fetchone()[0] == 0
            assert conn.execute("SELECT edits FROM assets WHERE id = ?", (wide,)).fetchone()[0]   # the recipe is still there


def test_the_old_files_go_when_the_photo_is_deleted_from_the_trash(tmp_path):
    root, c, cfg = setup(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)
        c.post(f"/api/assets/{wide}/apply-edits")
        assert kept_files(cfg, wide)
        c.post("/api/trash", json={"ids": [wide]})
        assert c.post("/api/trash/empty", json={}).json()["deleted"] == 1
        assert kept_files(cfg, wide) == []
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM file_backups").fetchone()[0] == 0


def test_a_photo_with_no_date_in_it_keeps_the_date_it_had(tmp_path):
    """Its date comes from the file's own time: writing a new file must not make it today."""
    root = tmp_path / "Photos"
    make_jpeg(root / "plain.jpg", None, size=(300, 200))
    old = time.time() - 40 * 86400
    os.utime(root / "plain.jpg", (old, old))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)["plain.jpg"]
        taken = c.get(f"/api/assets/{pid}").json()["taken_at"]
        edit(c, pid, rotate=180)
        assert c.post(f"/api/assets/{pid}/apply-edits").status_code == 200
        assert c.get(f"/api/assets/{pid}").json()["taken_at"] == taken


def test_png_is_rewritten_as_png(tmp_path):
    root = tmp_path / "Photos"
    (root).mkdir()
    Image.new("RGB", (300, 200), (30, 90, 200)).save(root / "flat.png")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)["flat.png"]
        edit(c, pid, rotate=90)
        assert c.post(f"/api/assets/{pid}/apply-edits").status_code == 200
        with Image.open(root / "flat.png") as im:
            assert im.format == "PNG" and im.size == (200, 300)
