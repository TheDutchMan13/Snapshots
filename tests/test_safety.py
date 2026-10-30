"""Regression tests for ways photos (or what people did with them) could be lost.

Each test is a scenario an independent review found and proved against version 0.2 before release.
"""

import shutil

import pytest
from fastapi.testclient import TestClient

from snapshots import scanner as scanner_module
from snapshots.scanner import Scanner, index_file
from helpers import ids_by_name, jpeg_bytes, login, make_jpeg, new_client, rescan, setup_admin, wait_scan


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "beach.jpg", "2024:07:04 10:30:00", color=(10, 120, 200))
    make_jpeg(root / "other.jpg", "2024:07:05 10:30:00", color=(200, 120, 10))
    return root


@pytest.fixture
def client(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        setup_admin(c)
        wait_scan(c)
        yield c, cfg, lib


def test_a_moved_photo_keeps_its_own_record_not_a_trashed_copy(client):
    """beach.jpg is a favourite in an album; an identical copy was trashed. The copy is then deleted
    in Explorer and beach.jpg moved. The moved file must keep beach.jpg's record, and emptying
    the trash must not delete it."""
    c, cfg, lib = client
    shutil.copy(lib / "beach.jpg", lib / "beach (1).jpg")
    rescan(c)
    ids = ids_by_name(c)
    keep, copy = ids["beach.jpg"], ids["beach (1).jpg"]
    c.post("/api/favorites", json={"ids": [keep]})
    album = c.post("/api/albums", json={"name": "Holiday", "ids": [keep]}).json()
    c.post("/api/trash", json={"ids": [copy]})

    (lib / "beach (1).jpg").unlink()
    (lib / "Best").mkdir()
    shutil.move(lib / "beach.jpg", lib / "Best" / "beach.jpg")
    rescan(c)

    moved = c.get(f"/api/assets/{keep}").json()
    assert moved["path"] == "Best/beach.jpg" and moved["favorite"] is True and not moved["trashed"]
    assert moved["albums"] == [{"id": album["id"], "name": "Holiday"}]
    c.post("/api/trash/empty", json={})
    assert (lib / "Best" / "beach.jpg").exists()


def test_a_moved_photo_never_becomes_trashed(client):
    c, cfg, lib = client
    ids = ids_by_name(c)
    c.post("/api/trash", json={"ids": [ids["beach.jpg"]]})
    (lib / "Moved").mkdir()
    shutil.move(lib / "beach.jpg", lib / "Moved" / "beach.jpg")
    rescan(c)
    assert "beach.jpg" in ids_by_name(c)  # back in the library as a photo, not in the trash
    c.post("/api/trash/empty", json={})
    assert (lib / "Moved" / "beach.jpg").exists()


def test_an_upload_during_a_scan_is_not_forgotten(tmp_path, lib, monkeypatch):
    c, cfg = new_client(tmp_path, lib, index_on_start=False)
    with c:
        setup_admin(c)
        real_list = scanner_module.list_media

        def slow_walk(root, skip_dir=None, prefix=""):
            found, errors = real_list(root, skip_dir, prefix)
            # While the folder is being walked, someone uploads a photo and puts it in an album.
            r = c.post("/api/upload", files=[("files", ("phone.jpg", jpeg_bytes((5, 6, 7)), "image/jpeg"))])
            new_id = r.json()["results"][0]["id"]
            c.post("/api/albums", json={"name": "Phone", "ids": [new_id]})
            return found, errors  # the walk had already passed the Uploads folder

        monkeypatch.setattr(scanner_module, "list_media", slow_walk)
        s = rescan(c)
        assert s["removed"] == 0
        assert "phone.jpg" in ids_by_name(c)
        album = c.get("/api/albums").json()["albums"][0]
        assert album["count"] == 1


def test_new_content_at_a_trashed_path_is_not_in_the_trash(client):
    """A camera reuses IMG_0001.jpg for a new photo after the old one was trashed."""
    c, cfg, lib = client
    ids = ids_by_name(c)
    c.post("/api/trash", json={"ids": [ids["other.jpg"]]})
    make_jpeg(lib / "other.jpg", "2025:01:01 09:00:00", color=(1, 250, 1))  # different picture, same name
    rescan(c)
    assert "other.jpg" in ids_by_name(c) and ids_by_name(c, trashed=True) == {}
    c.post("/api/trash/empty", json={})
    assert (lib / "other.jpg").exists()


def test_uploads_with_the_same_name_never_overwrite(client, monkeypatch):
    """Even if two uploads both think a name is free, each file gets its own name."""
    c, cfg, lib = client
    first = jpeg_bytes((10, 20, 30))
    second = jpeg_bytes((30, 20, 10))
    for data in (first, second):
        assert c.post("/api/upload", files=[("files", ("IMG_1234.jpg", data, "image/jpeg"))]).json()["results"][0]["status"] == "added"
    stored = sorted(p.read_bytes() for p in (lib / "Uploads").rglob("IMG_1234*.jpg"))
    assert stored == sorted([first, second])


def test_picking_the_wrong_folder_does_not_forget_the_library(tmp_path):
    root = tmp_path / "Photos"
    for i in range(30):
        make_jpeg(root / ("Holiday" if i < 25 else "Garden") / f"p{i}.jpg", f"2024:01:{i % 28 + 1:02d} 10:00:00",
                  color=(i * 8, 50, 100))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/albums", json={"name": "Trip", "ids": list(ids.values())[:10]})

        cfg.library_path = root / "Garden"  # oops: a subfolder
        s = rescan(c)
        # the 5 Garden photos are simply found at shorter paths; the other 25 are kept, not forgotten
        assert (s["removed"], s["moved"], s["skipped_removals"]) == (0, 5, 25) and "were kept" in s["message"]
        assert c.get("/api/albums").json()["albums"][0]["count"] == 10

        # a member cannot confirm forgetting them; an administrator can
        c.post("/api/users", json={"username": "kid", "password": "kid password"})
        kid = TestClient(c.app)
        kid.headers["X-Snapshots-Request"] = "1"
        login(kid, "kid", "kid password")
        assert kid.post("/api/scan", json={"remove_missing": True}).status_code == 403

        cfg.library_path = root  # back to the right folder: nothing was lost
        s = rescan(c)
        assert s["removed"] == 0 and len(ids_by_name(c)) == 30

        shutil.rmtree(root / "Holiday")  # a real clear-out of most of the library
        s = rescan(c)
        assert s["removed"] == 0 and s["skipped_removals"] == 25
        assert c.post("/api/scan", json={"remove_missing": True}).status_code == 202
        s = wait_scan(c)
        assert s["removed"] == 25 and len(ids_by_name(c)) == 5


def test_small_clear_outs_still_happen_automatically(client):
    c, cfg, lib = client
    (lib / "other.jpg").unlink()  # 1 of 2 is half, but far below the "many at once" size
    assert rescan(c)["removed"] == 1


def test_api_documentation_is_not_public(client):
    c, *_ = client
    anon = TestClient(c.app)
    for url in ("/docs", "/redoc", "/openapi.json"):
        assert anon.get(url).status_code == 404
