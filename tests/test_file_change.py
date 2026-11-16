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


# ---------------------------------------------------------------- after the review of 16 November 2026

def applied(c, asset_id, **recipe):
    edit(c, asset_id, **(recipe or {"rotate": 90}))
    r = c.post(f"/api/assets/{asset_id}/apply-edits")
    assert r.status_code == 200, r.text
    return r.json()


def test_kept_files_never_show_up_under_a_later_photo_with_the_same_number(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "0first.jpg", "2023:05:01 09:30:00", size=(640, 480), color=(9, 9, 9))
    make_jpeg(root / "a.jpg", "2024:05:01 09:30:00", size=(640, 480))
    original = (root / "a.jpg").read_bytes()
    c, cfg = new_client(tmp_path, root, keep_missing_days=0)
    with c:
        setup_admin(c)
        wait_scan(c)
        a = ids_by_name(c)["a.jpg"]
        applied(c, a)
        (root / "a.jpg").unlink()                       # deleted in Explorer, and forgotten by the scan
        rescan(c)
        make_jpeg(root / "other.jpg", "2025:01:01 09:30:00", size=(300, 200), color=(1, 2, 3))
        rescan(c)
        new = ids_by_name(c)["other.jpg"]
        assert new == a                                  # the number was given out again
        before = (root / "other.jpg").read_bytes()
        assert c.get(f"/api/assets/{new}").json()["file_backups"] == []
        assert c.post(f"/api/assets/{new}/restore-file", json={}).status_code == 404
        assert (root / "other.jpg").read_bytes() == before
        # The old file was not deleted: only emptying the trash deletes. It is set apart.
        forgotten = [p for p in (cfg.original_files_dir / "forgotten").rglob("*") if p.is_file()]
        assert [p.read_bytes() for p in forgotten] == [original]
        assert not (cfg.original_files_dir / str(a)).exists()
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM file_backups").fetchone()[0] == 0


def test_kept_files_left_without_an_item_are_set_apart_at_start(tmp_path):
    root, c, cfg = setup(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        applied(c, wide)
        with db.session(cfg.db_path) as conn:           # as an older version left things
            conn.execute("DELETE FROM assets WHERE id = ?", (wide,))
            assert filechange.detach_orphans(cfg, conn) == 1
            assert conn.execute("SELECT COUNT(*) FROM file_backups").fetchone()[0] == 0
        assert len([p for p in (cfg.original_files_dir / "forgotten").rglob("*") if p.is_file()]) == 1


def test_the_photo_from_before_the_change_still_counts_as_here(tmp_path):
    """The phone has the old content: it must not send it again, and may free its copy."""
    root, c, cfg = setup(tmp_path)
    original = (root / "2024" / "wide.jpg").read_bytes()
    old = hashlib.sha1(original).hexdigest()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        applied(c, wide)
        for kept in (False, True):
            assert c.post("/api/upload/check", json={"sha1": [old], "kept": kept}).json()["have"] == [old]
        r = c.post("/api/upload", files=[("files", ("wide.jpg", io.BytesIO(original), "image/jpeg"))]).json()
        assert r["results"][0] == {"name": "wide.jpg", "status": "duplicate", "id": wide}
        assert len(c.get("/api/assets").json()["items"]) == 1

        from snapshots import imports
        with db.session(cfg.db_path) as conn:
            assert imports.Finder(cfg).find(conn, old) == [wide]

        # In the trash: already sent, but the phone's own copy is not to be freed. An upload brings it back.
        c.post("/api/trash", json={"ids": [wide]})
        assert c.post("/api/upload/check", json={"sha1": [old]}).json()["have"] == [old]
        assert c.post("/api/upload/check", json={"sha1": [old], "kept": True}).json()["have"] == []
        r = c.post("/api/upload", files=[("files", ("wide.jpg", io.BytesIO(original), "image/jpeg"))]).json()
        assert r["results"][0]["status"] == "restored" and r["results"][0]["id"] == wide

        # If the kept old file itself is gone, the PC no longer has that content: the phone sends it.
        for f in kept_files(cfg, wide):
            f.unlink()
        assert c.post("/api/upload/check", json={"sha1": [old]}).json()["have"] == []


def test_the_kept_copy_and_the_new_file_are_written_through_to_the_disk(tmp_path, monkeypatch):
    root, c, cfg = setup(tmp_path)
    flushed = []
    real = filechange._flush
    monkeypatch.setattr(filechange, "_flush", lambda p: (flushed.append(Path(p).name), real(p))[1])
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        applied(c, wide)
        assert len(flushed) == 2 and flushed[1] == ".wide.jpg.snapshots-edit"      # the copy first, then the new file


def test_a_kept_copy_that_cannot_be_written_through_stops_everything(tmp_path, monkeypatch):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()

    def boom(path):
        raise OSError("the disk said no")

    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)
        monkeypatch.setattr(filechange, "_flush", boom)
        assert c.post(f"/api/assets/{wide}/apply-edits").status_code == 500
        assert original.read_bytes() == before and kept_files(cfg, wide) == []


def test_trouble_after_the_file_was_replaced_never_drops_the_kept_copy(tmp_path, monkeypatch):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    calls = []

    def flaky(folder):
        calls.append(folder)
        if len(calls) > 1:                               # the second call is the one after the swap
            raise RuntimeError("something odd")

    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        edit(c, wide, rotate=90)
        monkeypatch.setattr(filechange, "_flush_folder", flaky)
        assert c.post(f"/api/assets/{wide}/apply-edits").status_code == 200
        assert original.read_bytes() != before
        assert [f.read_bytes() for f in kept_files(cfg, wide)] == [before]


def test_putting_back_and_forth_keeps_two_copies_not_one_more_each_time(tmp_path):
    root, c, cfg = setup(tmp_path)
    original = root / "2024" / "wide.jpg"
    before = original.read_bytes()
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        applied(c, wide)
        turned = original.read_bytes()
        for expect in (before, turned, before, turned):
            assert c.post(f"/api/assets/{wide}/restore-file", json={}).status_code == 200
            assert original.read_bytes() == expect
            assert len(kept_files(cfg, wide)) == 2
        copies = c.get(f"/api/assets/{wide}").json()["file_backups"]
        assert len(copies) == 2 and sum(1 for x in copies if x["restored"]) == 1
        # Saving the same edit of the original again keeps nothing new either (the original is kept already).
        assert c.post(f"/api/assets/{wide}/restore-file", json={}).status_code == 200      # the original is back
        applied(c, wide)
        assert len(kept_files(cfg, wide)) == 2


def jpeg_with_notes(path: Path, *, xmp_extra: bytes = b"", trailer: bytes = b"", iptc: bool = True) -> None:
    xmp = (b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?><x:xmpmeta xmlns:x="adobe:ns:meta/">'
           b'<rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"><rdf:Description'
           b' xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmlns:tiff="http://ns.adobe.com/tiff/1.0/"'
           b' xmp:Rating="5" tiff:Orientation="6"' + xmp_extra + b'/></rdf:RDF></x:xmpmeta><?xpacket end="w"?>')
    buf = io.BytesIO()
    Image.new("RGB", (640, 480), (200, 30, 30)).save(buf, "JPEG", xmp=xmp, comment=b"a note", dpi=(300, 300),
                                                     subsampling=0, quality=90)
    data = buf.getvalue()
    if iptc:
        caption = b"\x1c\x02\x78" + (5).to_bytes(2, "big") + b"Hello"
        body = b"Photoshop 3.0\x00" + b"8BIM\x04\x04\x00\x00" + len(caption).to_bytes(4, "big") + caption
        data = data[:2] + b"\xff\xed" + (len(body) + 2).to_bytes(2, "big") + body + data[2:]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data + trailer)


def test_notes_in_the_file_are_carried_over(tmp_path):
    from PIL import IptcImagePlugin, JpegImagePlugin

    root = tmp_path / "Photos"
    jpeg_with_notes(root / "noted.jpg")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)["noted.jpg"]
        applied(c, pid)
        with Image.open(root / "noted.jpg") as im:
            # The notes said "shown a quarter turned" (Pillow reads that too), so the photo was upright
            # 480 x 640 before the edit turned it. Now the picture itself is turned and the note says upright.
            assert im.size == (640, 480)
            xmp = im.info["xmp"]
            xmp = xmp.encode() if isinstance(xmp, str) else xmp
            assert b'xmp:Rating="5"' in xmp and b'tiff:Orientation="1"' in xmp
            assert im.info["comment"] == b"a note"
            assert tuple(round(v) for v in im.info["dpi"]) == (300, 300)
            assert JpegImagePlugin.get_sampling(im) == 0                 # as sharp in colour as it was
            assert IptcImagePlugin.getiptcinfo(im) == {(2, 120): b"Hello"}
            im.load()


@pytest.mark.parametrize("what", ["trailer", "regions", "extended", "png-colour"])
def test_files_with_something_a_rewrite_would_lose_are_refused(tmp_path, what):
    root = tmp_path / "Photos"
    name = "x.png" if what == "png-colour" else "x.jpg"
    if what == "trailer":
        jpeg_with_notes(root / name, trailer=b"\xff\xd8 a second picture \xff\xd9")
    elif what == "regions":
        jpeg_with_notes(root / name, xmp_extra=b' xmlns:mwg-rs="http://www.metadataworkinggroup.com/schemas/regions/" mwg-rs:Regions="a face"')
    elif what == "extended":
        jpeg_with_notes(root / name, xmp_extra=b' xmlns:xmpNote="http://ns.adobe.com/xmp/note/" xmpNote:HasExtendedXMP="ABC"')
    else:
        root.mkdir(parents=True)
        Image.new("RGB", (200, 100), (255, 0, 255)).save(root / name, transparency=(255, 0, 255))
    before = (root / name).read_bytes()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)[name]
        edit(c, pid, rotate=90)
        r = c.post(f"/api/assets/{pid}/apply-edits")
        assert r.status_code == 415, r.text
        assert (root / name).read_bytes() == before and kept_files(cfg, pid) == []


def test_what_a_person_said_about_a_pet_stays_when_the_file_changes(tmp_path):
    root, c, cfg = setup(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        wide = ids_by_name(c)["wide.jpg"]
        with db.session(cfg.db_path) as conn:
            conn.execute("INSERT INTO pets (id, name, species, created_at) VALUES (1, 'Rex', 'dog', '2026-01-01T00:00:00')")
            conn.execute("INSERT INTO pet_sightings (asset_id, pet_id, species, x, y, w, h, vec, score, created_at, manual)"
                         " VALUES (?, 1, 'dog', 0.2, 0.2, 0.3, 0.3, ?, 0.9, '2026-01-01T00:00:00', 1)", (wide, b"\0" * 1024))
            conn.execute("INSERT INTO pet_sightings (asset_id, pet_id, species, x, y, w, h, vec, score, created_at, manual)"
                         " VALUES (?, NULL, 'cat', 0.2, 0.2, 0.3, 0.3, ?, 0.9, '2026-01-01T00:00:00', 0)", (wide, b"\0" * 1024))
        applied(c, wide)
        with db.session(cfg.db_path) as conn:
            rows = [tuple(r) for r in conn.execute("SELECT pet_id, manual, x, y, w, h, score FROM pet_sightings WHERE asset_id = ?", (wide,))]
        assert rows == [(1, 1, 0, 0, 1, 1, 0)]          # the hand-made one stays, as the whole picture; the found one goes
