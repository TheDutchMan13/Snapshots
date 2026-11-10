"""Does the library have this photo? The answer must come from the disk, not only from the records.

The phone asks /api/upload/check before it sends a photo, and again ("kept") before "Free up space"
removes the phone's own copy. Records are kept on purpose when a folder is emptied or a drive is away,
so an answer from the records alone would let the phone delete the last copy of a photo, and would
throw a re-sent photo away as a duplicate.
"""

import hashlib
import os
import shutil
import sqlite3

from helpers import add_member, ids_by_name, jpeg_bytes, login, make_jpeg, new_client, rescan, setup_admin, wait_scan
from fastapi.testclient import TestClient

DATA = jpeg_bytes((10, 200, 30))
SHA = hashlib.sha1(DATA).hexdigest()


def start(tmp_path, n=3, **kw):
    """A library with n other photos, and one photo uploaded as the phone would."""
    root = tmp_path / "Photos"
    for i in range(n):
        make_jpeg(root / "2024" / f"other{i}.jpg", f"2024:03:{i + 1:02d} 10:00:00", color=(i * 40, 9, 200))
    c, cfg = new_client(tmp_path, root, **kw)
    return c, cfg, root


def send(c, data=DATA, name="phone.jpg"):
    return c.post("/api/upload", files=[("files", (name, data, "image/jpeg"))]).json()["results"][0]


def check(c, kept=False, sha=SHA):
    r = c.post("/api/upload/check", json={"sha1": [sha], "kept": kept}).json()
    assert r["verified"] is True and r["kept"] is kept
    return r["have"] == [sha]


def rows(cfg, sha=SHA):
    conn = sqlite3.connect(cfg.db_path)
    try:
        return conn.execute("SELECT id, path, trashed_at, missing_since FROM assets WHERE sha1 = ? ORDER BY id", (sha,)).fetchall()
    finally:
        conn.close()


def files(root, name="phone*.jpg"):
    return sorted(p for p in root.rglob(name) if ".incoming" not in p.parts)


def test_a_photo_that_is_on_the_disk_counts(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert not check(c) and not check(c, kept=True)
        assert send(c)["status"] == "added"
        assert check(c) and check(c, kept=True)
        assert send(c)["status"] == "duplicate" and len(files(root)) == 1


def test_a_record_whose_file_is_gone_does_not_count_and_the_photo_is_taken_again(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = send(c)["id"]
        album = c.post("/api/albums", json={"name": "Phone", "ids": [first]}).json()["id"]
        c.post("/api/favorites", json={"ids": [first], "value": True})
        files(root)[0].unlink()                 # deleted in Windows; no scan has noticed yet
        assert rows(cfg)[0][3] is None          # (the record is still an ordinary one)
        assert not check(c) and not check(c, kept=True)
        again = send(c)
        assert again["status"] == "added" and again["id"] == first      # the same item carries on
        assert len(files(root)) == 1 and len(rows(cfg)) == 1
        assert check(c) and check(c, kept=True)
        assert c.get(f"/api/assets/{first}").json()["favorite"]
        assert c.get("/api/albums").json()["albums"][0]["count"] == 1 and album
        assert rescan(c)["removed"] == 0 and len(rows(cfg)) == 1


def test_an_emptied_library_keeps_its_records_but_never_says_it_has_the_photos(tmp_path):
    """What happened on the PC: the library folder was emptied by mistake. The records stay (an empty
    folder removes nothing), and the phone must neither free its copies nor have a re-sent photo refused."""
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = send(c)["id"]
        shutil.rmtree(root)
        root.mkdir()
        s = rescan(c)
        assert s["removed"] == 0 and c.get("/api/status").json()["photos"] == 4     # nothing was forgotten
        assert not check(c) and not check(c, kept=True)
        again = send(c)
        assert again["status"] == "added" and again["id"] == first
        assert len(files(root)) == 1 and len(rows(cfg)) == 1
        assert check(c, kept=True)


def test_the_item_moves_when_the_photo_is_filed_somewhere_else(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = send(c)["id"]
        old = files(root)[0]
        c.post("/api/favorites", json={"ids": [first], "value": True})
        old.unlink()
        again = send(c, name="renamed-on-phone.jpg")
        assert again["status"] == "added" and again["id"] == first
        (path,) = [r[1] for r in rows(cfg)]
        assert path.endswith("renamed-on-phone.jpg") and (root / path).is_file()
        assert c.get(f"/api/assets/{first}").json()["favorite"] and check(c, kept=True)


def test_a_drive_that_is_away_counts_as_sent_but_never_as_kept(tmp_path):
    extra = tmp_path / "Drive E"
    make_jpeg(extra / "x.jpg", "2020:01:01 10:00:00")
    (extra / "phone.jpg").write_bytes(DATA)
    c, cfg, root = start(tmp_path, extra_libraries=[extra])
    with c:
        setup_admin(c)
        wait_scan(c)
        assert check(c) and check(c, kept=True)
        away = tmp_path / "Drive E unplugged"
        extra.rename(away)
        assert check(c)                         # expected back: the phone does not send everything again
        assert not check(c, kept=True)          # but its own copy stays until the file is seen
        assert send(c)["status"] == "duplicate" and files(root) == []
        away.rename(extra)
        assert check(c, kept=True)


def test_a_file_saved_again_is_not_trusted_until_it_is_read(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        send(c)
        path = files(root)[0]
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 5_000_000_000))   # same content, a new time
        assert not check(c) and not check(c, kept=True)
        assert send(c)["status"] == "duplicate" and len(files(root)) == 1     # read, and found the same
        rescan(c)
        assert check(c, kept=True)


def test_another_file_under_the_photos_name_does_not_count(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = send(c)["id"]
        path = files(root)[0]
        path.write_bytes(jpeg_bytes((250, 250, 0)))       # a different picture now has its name
        assert not check(c) and not check(c, kept=True)
        again = send(c)
        assert again["status"] == "added" and again["id"] != first
        assert len(files(root)) == 2 and check(c, kept=True)
        rescan(c)                                         # the old record now describes the other picture
        assert [r[0] for r in rows(cfg)] == [again["id"]]


def test_the_trash(tmp_path):
    c, cfg, root = start(tmp_path)
    with c:
        setup_admin(c)
        wait_scan(c)
        first = send(c)["id"]
        c.post("/api/trash", json={"ids": [first]})
        assert check(c) and not check(c, kept=True)       # sent, but it could still be emptied out
        files(root)[0].unlink()
        assert not check(c)
        again = send(c)             # stored again, and out of the trash: emptying it must not delete this file
        assert again["status"] == "added" and again["id"] == first
        assert [r[2] for r in rows(cfg)] == [None] and check(c, kept=True)
        assert c.post("/api/trash/empty", json={}).json()["deleted"] == 0
        assert len(files(root)) == 1 and check(c, kept=True)


def test_many_at_once_and_other_accounts(tmp_path):
    c, cfg, root = start(tmp_path, n=0)
    pics = [jpeg_bytes((i, 2 * i, 255 - i)) for i in range(40)]
    sums = [hashlib.sha1(p).hexdigest() for p in pics]
    for i, p in enumerate(pics):
        (root / "all").mkdir(parents=True, exist_ok=True)
        (root / "all" / f"p{i:02d}.jpg").write_bytes(p)
    with c:
        setup_admin(c)
        wait_scan(c)
        for i in range(0, 40, 4):
            (root / "all" / f"p{i:02d}.jpg").unlink()
        add_member(c)
        m = TestClient(c.app)
        m.headers["X-Snapshots-Request"] = "1"
        login(m, "member", "member password 1")
        r = m.post("/api/upload/check", json={"sha1": sums, "kept": True}).json()
        assert r["verified"] and set(r["have"]) == {s for i, s in enumerate(sums) if i % 4}
        assert len(ids_by_name(c)) == 40                  # asking changed nothing
