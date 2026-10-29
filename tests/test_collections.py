import shutil
import time

import pytest
from fastapi.testclient import TestClient

from helpers import (ids_by_name, jpeg_bytes, login, make_jpeg, new_client, rescan, setup_admin, wait_scan)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00", color=(10, 10, 10))
    make_jpeg(root / "b.jpg", "2024:02:01 10:00:00", color=(20, 20, 20))
    make_jpeg(root / "c.jpg", "2024:03:01 10:00:00", color=(30, 30, 30))
    return root


@pytest.fixture
def client(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        setup_admin(c)
        wait_scan(c)
        yield c, cfg, lib


def second_user(c, name="kid", password="kid's password"):
    assert c.post("/api/users", json={"username": name, "password": password}).status_code == 201
    other = TestClient(c.app)
    other.headers["X-Snapshots-Request"] = "1"
    login(other, name, password)
    return other


# ------------------------------------------------------------------ favourites

def test_favourites_are_per_person(client):
    c, *_ = client
    ids = ids_by_name(c)
    kid = second_user(c)
    assert c.post("/api/favorites", json={"ids": [ids["a.jpg"], ids["b.jpg"]]}).status_code == 200
    assert set(ids_by_name(c, favorite=True)) == {"a.jpg", "b.jpg"}
    assert ids_by_name(kid, favorite=True) == {}
    assert c.get(f"/api/assets/{ids['a.jpg']}").json()["favorite"] is True
    assert kid.get(f"/api/assets/{ids['a.jpg']}").json()["favorite"] is False
    c.post("/api/favorites", json={"ids": [ids["a.jpg"]], "value": False})
    assert set(ids_by_name(c, favorite=True)) == {"b.jpg"}
    c.post("/api/favorites", json={"ids": [999999]})  # unknown ids are ignored, not an error


# ------------------------------------------------------------------ trash

def test_trash_hides_and_restore_brings_back(client):
    c, *_ = client
    ids = ids_by_name(c)
    c.post("/api/favorites", json={"ids": [ids["a.jpg"]]})
    album = c.post("/api/albums", json={"name": "Trip", "ids": [ids["a.jpg"], ids["b.jpg"]]}).json()
    c.post("/api/trash", json={"ids": [ids["a.jpg"]]})

    assert set(ids_by_name(c)) == {"b.jpg", "c.jpg"}
    assert set(ids_by_name(c, trashed=True)) == {"a.jpg"}
    assert ids_by_name(c, favorite=True) == {}
    assert set(ids_by_name(c, album=album["id"])) == {"b.jpg"}
    assert c.get(f"/api/albums/{album['id']}").json()["count"] == 1
    assert c.get("/api/status").json()["trashed"] == 1

    c.post("/api/trash/restore", json={"ids": [ids["a.jpg"]]})
    assert set(ids_by_name(c)) == {"a.jpg", "b.jpg", "c.jpg"}
    assert set(ids_by_name(c, favorite=True)) == {"a.jpg"}  # nothing was lost while it was in the trash
    assert c.get(f"/api/albums/{album['id']}").json()["count"] == 2


def test_trashed_photos_stay_trashed_after_rescan(client):
    c, *_ = client
    ids = ids_by_name(c)
    c.post("/api/trash", json={"ids": [ids["c.jpg"]]})
    rescan(c)
    assert set(ids_by_name(c, trashed=True)) == {"c.jpg"}


def test_emptying_the_trash_deletes_files(client):
    c, cfg, lib = client
    ids = ids_by_name(c)
    kid = second_user(c)
    c.post("/api/trash", json={"ids": [ids["a.jpg"], ids["b.jpg"]]})

    assert kid.post("/api/trash/empty", json={}).status_code == 403  # only administrators
    assert (lib / "a.jpg").exists()

    r = c.post("/api/trash/empty", json={"ids": [ids["a.jpg"], ids["c.jpg"]]}).json()  # c.jpg is not in the trash
    assert r == {"deleted": 1, "failed": []}
    assert not (lib / "a.jpg").exists() and (lib / "b.jpg").exists() and (lib / "c.jpg").exists()
    assert not cfg.thumb_path(ids["a.jpg"]).exists()

    r = c.post("/api/trash/empty", json={}).json()  # everything left in the trash
    assert r["deleted"] == 1 and not (lib / "b.jpg").exists()
    assert set(ids_by_name(c)) == {"c.jpg"} and ids_by_name(c, trashed=True) == {}


def test_trash_is_not_emptied_when_the_drive_is_missing(client):
    c, cfg, lib = client
    ids = ids_by_name(c)
    c.post("/api/trash", json={"ids": [ids["a.jpg"]]})
    real = cfg.library_path
    cfg.library_path = real.with_name("unplugged")
    assert c.post("/api/trash/empty", json={}).status_code == 409
    cfg.library_path = real
    assert set(ids_by_name(c, trashed=True)) == {"a.jpg"} and (lib / "a.jpg").exists()


def test_uploading_a_trashed_photo_restores_it(client):
    c, cfg, lib = client
    data = jpeg_bytes((77, 77, 77))
    first = c.post("/api/upload", files=[("files", ("x.jpg", data, "image/jpeg"))]).json()["results"][0]
    c.post("/api/trash", json={"ids": [first["id"]]})
    again = c.post("/api/upload", files=[("files", ("x again.jpg", data, "image/jpeg"))]).json()["results"][0]
    assert again == {"name": "x again.jpg", "status": "restored", "id": first["id"]}
    assert "x.jpg" in ids_by_name(c)


# ------------------------------------------------------------------ albums

def test_albums(client):
    c, cfg, lib = client
    ids = ids_by_name(c)
    kid = second_user(c)
    album = c.post("/api/albums", json={"name": "  Summer  ", "ids": [ids["a.jpg"]]}).json()
    assert album["name"] == "Summer" and album["count"] == 1 and album["cover"] == ids["a.jpg"]

    # albums are shared: everyone sees and can add to them
    assert [a["name"] for a in kid.get("/api/albums").json()["albums"]] == ["Summer"]
    kid.post(f"/api/albums/{album['id']}/add", json={"ids": [ids["b.jpg"], ids["c.jpg"], ids["a.jpg"]]})
    detail = c.get(f"/api/albums/{album['id']}").json()
    assert detail["count"] == 3 and detail["first"].startswith("2024-01") and detail["last"].startswith("2024-03")
    assert detail["cover"] == ids["c.jpg"]  # the newest photo
    assert set(ids_by_name(c, album=album["id"])) == {"a.jpg", "b.jpg", "c.jpg"}
    assert c.get(f"/api/assets/{ids['b.jpg']}").json()["albums"] == [{"id": album["id"], "name": "Summer"}]

    c.post(f"/api/albums/{album['id']}/remove", json={"ids": [ids["b.jpg"]]})
    assert set(ids_by_name(c, album=album["id"])) == {"a.jpg", "c.jpg"}
    assert c.patch(f"/api/albums/{album['id']}", json={"name": "Summer 2024"}).json()["name"] == "Summer 2024"
    assert c.patch(f"/api/albums/{album['id']}", json={"name": "   "}).status_code == 400
    assert c.post("/api/albums", json={"name": ""}).status_code == 422

    assert c.delete(f"/api/albums/{album['id']}").status_code == 200
    assert c.get("/api/albums").json()["albums"] == []
    assert len(ids_by_name(c)) == 3 and (lib / "a.jpg").exists()  # the photos are still there
    assert c.get(f"/api/albums/{album['id']}").status_code == 404


# ------------------------------------------------------------------ moving files around

def test_moved_and_renamed_files_keep_everything(client):
    c, cfg, lib = client
    ids = ids_by_name(c)
    c.post("/api/favorites", json={"ids": [ids["a.jpg"]]})
    album = c.post("/api/albums", json={"name": "Keep", "ids": [ids["a.jpg"]]}).json()

    (lib / "sorted" / "2024").mkdir(parents=True)
    shutil.move(lib / "a.jpg", lib / "sorted" / "2024" / "renamed.jpg")
    s = rescan(c)
    assert (s["moved"], s["added"], s["removed"]) == (1, 0, 0)

    moved = c.get(f"/api/assets/{ids['a.jpg']}").json()  # same id as before
    assert moved["path"] == "sorted/2024/renamed.jpg" and moved["filename"] == "renamed.jpg"
    assert moved["favorite"] is True and moved["albums"][0]["id"] == album["id"]
    assert c.get(f"/api/assets/{ids['a.jpg']}/original").status_code == 200


def test_copies_are_separate_photos(client):
    c, cfg, lib = client
    shutil.copy(lib / "a.jpg", lib / "a copy.jpg")  # the original stays: this is a copy, not a move
    s = rescan(c)
    assert (s["added"], s["moved"]) == (1, 0)
    assert {"a.jpg", "a copy.jpg"} <= set(ids_by_name(c))
