"""Archive, tags, folders, memories, the map, stacks, Live Photos, motion photos and RAW files."""

import sqlite3

from PIL import Image

from snapshots import db, linking, media
from snapshots.app import tile_source
from helpers import (ids_by_name, make_dng, make_jpeg, make_keywords_jpeg, make_motion_jpeg, mp4_bytes, new_client,
                     rescan, setup_admin, wait_scan)


def names(c, **params):
    return sorted(ids_by_name(c, **params))


# ------------------------------------------------------------------ archive

def test_archive_leaves_the_timeline_but_not_the_library(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "receipt.jpg", "2024:05:01 10:00:00")
    make_jpeg(root / "beach.jpg", "2024:05:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        assert c.post("/api/archive", json={"ids": [ids["receipt.jpg"]]}).json() == {"ok": True}
        assert names(c, archived="hide") == ["beach.jpg"]
        assert names(c, archived="only") == ["receipt.jpg"]
        assert names(c, q="receipt") == ["receipt.jpg"]          # search still finds it
        months = c.get("/api/timeline", params={"archived": "hide"}).json()["months"]
        assert months == [{"month": "2024-05", "count": 1}]
        item = c.get(f"/api/assets/{ids['receipt.jpg']}").json()
        assert item["archived"] is True
        c.post("/api/archive", json={"ids": [ids["receipt.jpg"]], "value": False})
        assert names(c, archived="hide") == ["beach.jpg", "receipt.jpg"]
        assert c.get("/api/assets", params={"archived": "nope"}).status_code == 422


# ------------------------------------------------------------------ tags

def test_tags(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:05:02 10:00:00")
    make_keywords_jpeg(root / "c.jpg", ["Holiday", "Beach & sun"], "2024:05:03 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        # keywords written by other programs become tags
        tags = {t["name"]: t for t in c.get("/api/tags").json()["tags"]}
        assert set(tags) == {"Holiday", "Beach & sun"} and tags["Holiday"]["count"] == 1
        made = c.post("/api/tags", json={"name": "  holiday ", "ids": [ids["a.jpg"]]}).json()
        assert made["id"] == tags["Holiday"]["id"] and made["count"] == 2   # same tag, any case
        assert names(c, tag=made["id"]) == ["a.jpg", "c.jpg"]
        assert names(c, q="holiday") == ["a.jpg", "c.jpg"]                 # tags are searched
        family = c.post("/api/tags", json={"name": "Family", "ids": [ids["b.jpg"], ids["a.jpg"]]}).json()
        assert [t["name"] for t in c.get(f"/api/assets/{ids['a.jpg']}").json()["tags"]] == ["Family", "Holiday"]
        c.post(f"/api/tags/{family['id']}/remove", json={"ids": [ids["a.jpg"]]})
        assert names(c, tag=family["id"]) == ["b.jpg"]
        # renaming onto another tag's name puts them together
        merged = c.patch(f"/api/tags/{family['id']}", json={"name": "HOLIDAY"}).json()
        assert merged["id"] == made["id"] and merged["count"] == 3
        assert c.get(f"/api/tags/{family['id']}").status_code == 404
        c.delete(f"/api/tags/{made['id']}")
        assert names(c) == ["a.jpg", "b.jpg", "c.jpg"]                      # photos stay
        assert [t["name"] for t in c.get("/api/tags").json()["tags"]] == ["Beach & sun"]


# ------------------------------------------------------------------ folders

def test_folders(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "top.jpg", "2024:05:01 10:00:00")
    make_jpeg(root / "2023" / "Summer" / "s1.jpg", "2023:07:01 10:00:00")
    make_jpeg(root / "2023" / "Summer" / "s2.jpg", "2023:07:02 10:00:00")
    make_jpeg(root / "2023" / "x.jpg", "2023:01:01 10:00:00")
    make_jpeg(root / "2023 b" / "y_1.jpg", "2023:01:01 10:00:00")   # names with LIKE wildcards
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        top = c.get("/api/folders").json()
        assert top["items"] == 1 and [(f["name"], f["count"]) for f in top["folders"]] == [("2023", 3), ("2023 b", 1)]
        assert names(c, folder="") == ["top.jpg"]
        sub = c.get("/api/folders", params={"path": "2023"}).json()
        assert sub["items"] == 1 and [(f["path"], f["count"]) for f in sub["folders"]] == [("2023/Summer", 2)]
        assert names(c, folder="2023") == ["x.jpg"]
        assert names(c, folder="2023/Summer") == ["s1.jpg", "s2.jpg"]
        assert names(c, folder="2023 b") == ["y_1.jpg"]
        assert c.get("/api/folders", params={"path": "nope"}).status_code == 404
        assert c.get(f"/api/assets/{ids_by_name(c)['s1.jpg']}").json()["folder"] == "2023/Summer"


# ------------------------------------------------------------------ memories

def test_memories_on_this_day(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2023:10:29 10:00:00")
    make_jpeg(root / "b.jpg", "2023:10:29 11:00:00")
    make_jpeg(root / "c.jpg", "2020:10:29 09:00:00")
    make_jpeg(root / "d.jpg", "2020:10:30 09:00:00")   # another day
    make_jpeg(root / "e.jpg", "2026:10:29 08:00:00")   # this year: not a memory
    make_jpeg(root / "f.jpg", "2019:10:29 08:00:00")   # archived
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/favorites", json={"ids": [ids["a.jpg"]]})
        c.post("/api/archive", json={"ids": [ids["f.jpg"]]})
        mem = c.get("/api/memories", params={"today": "2026-10-29"}).json()["memories"]
        assert [(m["year"], m["years_ago"], m["count"]) for m in mem] == [(2023, 3, 2), (2020, 6, 1)]
        assert mem[0]["cover"] == ids["a.jpg"]   # the favourite
        assert c.get("/api/memories", params={"today": "2026-13-40"}).status_code in (400, 422)


# ------------------------------------------------------------------ the map

def test_map_points_and_areas(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "sedona.jpg", "2024:05:01 10:00:00", gps=(34.87, -111.76))
    make_jpeg(root / "fiji.jpg", "2024:05:02 10:00:00", gps=(-17.7, 179.9))
    make_jpeg(root / "samoa.jpg", "2024:05:03 10:00:00", gps=(-13.8, -171.8))
    make_jpeg(root / "nowhere.jpg", "2024:05:04 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        data = c.get("/api/map").json()
        assert sorted(p[0] for p in data["points"]) == sorted(i for n, i in ids_by_name(c).items() if n != "nowhere.jpg")
        assert data["tiles"].startswith("https://tile.openstreetmap.org/")
        assert names(c, bbox="30,-115,40,-105") == ["sedona.jpg"]
        assert names(c, bbox="-20,170,-10,-170") == ["fiji.jpg", "samoa.jpg"]   # across the date line
        assert c.get("/api/assets", params={"bbox": "1,2,3"}).status_code == 422
        assert c.get("/api/assets", params={"bbox": "50,0,40,10"}).status_code == 400
        csp = c.get("/api/map").headers["content-security-policy"]
        assert "img-src 'self' data: https://tile.openstreetmap.org;" in csp


def test_map_without_online_pictures(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    c, cfg = new_client(tmp_path, root, map_tiles="")
    with c:
        setup_admin(c)
        data = c.get("/api/map").json()
        assert data["tiles"] is None
        assert "img-src 'self' data:;" in c.get("/api/map").headers["content-security-policy"]
    assert tile_source("https://{s}.tile.example.org/{z}/{x}/{y}.png") == "https://*.tile.example.org"
    assert tile_source("https://a.org;script-src x/{z}") == ""


# ------------------------------------------------------------------ stacks

def test_stacks_show_one_tile(tmp_path):
    root = tmp_path / "Photos"
    for n in range(4):
        make_jpeg(root / f"burst{n}.jpg", f"2024:05:01 10:00:0{n}", color=(200, 80 + n, 60))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        stack = c.post("/api/stacks", json={"ids": [ids["burst2.jpg"], ids["burst0.jpg"], ids["burst1.jpg"]]}).json()
        assert stack["count"] == 3
        shown = c.get("/api/assets", params={"stacks": True}).json()["items"]
        assert [i["filename"] for i in shown] == ["burst3.jpg", "burst2.jpg"]   # the cover stands for the stack
        assert shown[1]["stack"] == stack["id"] and shown[1]["stack_count"] == 3
        assert c.get("/api/timeline", params={"stacks": True}).json()["months"][0]["count"] == 2
        assert len(names(c)) == 4                                              # albums etc. show them all
        detail = c.get(f"/api/assets/{ids['burst2.jpg']}").json()
        assert [(i["filename"], i["cover"]) for i in detail["stack_items"]] == [
            ("burst2.jpg", True), ("burst0.jpg", False), ("burst1.jpg", False)]
        # change the cover
        c.post(f"/api/stacks/{stack['id']}/cover", json={"cover": ids["burst0.jpg"]})
        assert names(c, stacks=True) == ["burst0.jpg", "burst3.jpg"]
        assert c.post(f"/api/stacks/{stack['id']}/cover", json={"cover": ids["burst3.jpg"]}).status_code == 404
        # trash the cover: the next one takes its place, nothing disappears
        c.post("/api/trash", json={"ids": [ids["burst0.jpg"]]})
        stand_in = min(("burst1.jpg", "burst2.jpg"), key=ids.get)
        assert names(c, stacks=True) == [stand_in, "burst3.jpg"]
        c.post("/api/trash/restore", json={"ids": [ids["burst0.jpg"]]})
        res = c.post("/api/stacks/unstack", json={"ids": [ids["burst1.jpg"]]}).json()
        assert res == {"stacks": 1, "photos": 3}
        assert len(names(c, stacks=True)) == 4
        assert c.post("/api/stacks", json={"ids": [ids["burst1.jpg"], 99999]}).status_code == 400


def test_raw_and_jpeg_are_stacked_and_raw_files_work(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "DSC_0001.JPG", "2024:06:01 12:00:00", color=(200, 80, 60))
    make_dng(root / "DSC_0001.DNG", "2024:06:01 12:00:00", color=(200, 80, 60), orientation=6, size=(96, 64))
    make_dng(root / "solo.dng", "2024:06:02 08:00:00", preview=False)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        assert set(ids) == {"DSC_0001.JPG", "DSC_0001.DNG", "solo.dng"}
        assert names(c, stacks=True) == ["DSC_0001.JPG", "solo.dng"]
        raw = c.get(f"/api/assets/{ids['DSC_0001.DNG']}").json()
        assert (raw["taken_at"], raw["camera"], raw["raw"]) == ("2024-06-01T12:00:00", "Testcam R1", True)
        assert (raw["width"], raw["height"]) == (64, 96)                      # turned the right way
        thumb = c.get(f"/api/assets/{ids['DSC_0001.DNG']}/thumb")
        assert thumb.status_code == 200 and thumb.headers["content-type"] == "image/jpeg"
        view = c.get(f"/api/assets/{ids['solo.dng']}/view")                   # developed from the RAW data
        assert view.status_code == 200 and view.headers["content-type"] == "image/jpeg"
        # the pair is not offered as copies in Clean up
        c.get("/api/cleanup")
        from helpers import wait_for
        wait_for(lambda: c.get("/api/status").json()["checks"]["pending"] == 0)
        assert c.get("/api/cleanup").json()["groups"] == 0
        # taken apart, it stays apart after a rescan
        c.post("/api/stacks/unstack", json={"ids": [ids["DSC_0001.JPG"]]})
        rescan(c)
        assert len(names(c, stacks=True)) == 3


# ------------------------------------------------------------------ Live Photos and motion photos

def test_live_photo_pairs(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "IMG_0001.JPG", "2024:06:01 12:00:00")
    (root / "IMG_0001.MOV").write_bytes(mp4_bytes(b"live clip"))
    (root / "IMG_0002.MOV").write_bytes(mp4_bytes(b"ordinary video"))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        shown = c.get("/api/assets").json()["items"]
        assert sorted(i["filename"] for i in shown) == ["IMG_0001.JPG", "IMG_0002.MOV"]   # the clip is hidden
        photo = next(i for i in shown if i["filename"] == "IMG_0001.JPG")
        assert photo["live"] is True
        clip = c.get(f"/api/assets/{photo['id']}/motion")
        assert clip.status_code == 200 and b"live clip" in clip.content
        assert clip.headers["content-type"] == "video/mp4"
        assert c.get("/api/status").json()["videos"] == 1
        # the clip goes to the trash, and comes back, with its photo
        c.post("/api/trash", json={"ids": [photo["id"]]})
        conn = db.connect(cfg.db_path)
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE trashed_at IS NOT NULL").fetchone()[0] == 2
        assert [i["filename"] for i in c.get("/api/assets", params={"trashed": True}).json()["items"]] == ["IMG_0001.JPG"]
        c.post("/api/trash/restore", json={"ids": [photo["id"]]})
        assert conn.execute("SELECT COUNT(*) FROM assets WHERE trashed_at IS NOT NULL").fetchone()[0] == 0
        c.post("/api/trash", json={"ids": [photo["id"]]})
        assert c.post("/api/trash/empty", json={"ids": [photo["id"]]}).json()["deleted"] == 2
        assert not (root / "IMG_0001.MOV").exists()
        assert c.get(f"/api/assets/{ids_by_name(c)['IMG_0002.MOV']}/motion").status_code == 404


def test_motion_photos(tmp_path):
    root = tmp_path / "Photos"
    video = mp4_bytes(b"inside the photo" * 10)
    for style in ("google", "micro", "samsung"):
        make_motion_jpeg(root / f"PXL_{style}.jpg", style=style, video=video)
    make_jpeg(root / "still.jpg", "2024:06:01 12:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        items = {i["filename"]: i for i in c.get("/api/assets").json()["items"]}
        assert {n for n, i in items.items() if i["live"]} == {"PXL_google.jpg", "PXL_micro.jpg", "PXL_samsung.jpg"}
        for name in ("PXL_google.jpg", "PXL_samsung.jpg"):
            r = c.get(f"/api/assets/{items[name]['id']}/motion")
            assert r.status_code == 200 and r.content == video and r.headers["content-type"] == "video/mp4"
        # the photo itself still opens
        assert c.get(f"/api/assets/{items['PXL_google.jpg']['id']}/view").status_code == 200
        assert c.get(f"/api/assets/{items['still.jpg']['id']}/motion").status_code == 404


def test_hevc_clips_are_converted_once(tmp_path):
    import shutil
    import subprocess

    import pytest
    if not shutil.which("ffmpeg") or "libx265" not in subprocess.run(["ffmpeg", "-hide_banner", "-encoders"],
                                                                      capture_output=True, text=True).stdout:
        pytest.skip("needs ffmpeg with an HEVC encoder")
    root = tmp_path / "Photos"
    make_jpeg(root / "IMG_0007.jpg", "2024:06:01 12:00:00")
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "testsrc=size=320x240:rate=15", "-t", "1",
                    "-c:v", "libx265", "-tag:v", "hvc1", str(root / "IMG_0007.MOV")], check=True)
    assert media.video_codec(root / "IMG_0007.MOV") == "hevc"
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        photo = c.get("/api/assets").json()["items"][0]
        r = c.get(f"/api/assets/{photo['id']}/motion")
        assert r.status_code == 200
        assert cfg.motion_path(photo["id"]).is_file()
        assert media.video_codec(cfg.motion_path(photo["id"])) == "h264"
        assert r.content == cfg.motion_path(photo["id"]).read_bytes()


def test_motion_detection_rejects_lookalikes(tmp_path):
    p = tmp_path / "x.jpg"
    make_jpeg(p, "2024:06:01 12:00:00")
    data = p.read_bytes()
    # says it is a motion photo, but there is no video where it says
    p.write_bytes(data.replace(b"\xff\xd9", b"\xff\xd9") + b"GCamera:MotionPhoto=\"1\" junk" * 3)
    assert media.find_motion_video(p) is None
    assert media.find_motion_video(tmp_path / "missing.jpg") is None


# ------------------------------------------------------------------ upgrading an older library

def test_upgrade_finds_motion_photos_already_indexed(tmp_path):
    root = tmp_path / "Photos"
    video = mp4_bytes(b"old phone")
    make_motion_jpeg(root / "PXL_1.jpg", style="google", video=video)
    make_keywords_jpeg(root / "lr.jpg", ["Old keyword"])
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
    # pretend it was indexed by version 0.4, which didn't look for videos
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE assets SET motion_offset = NULL, motion_length = NULL, motion_done = 0")
    conn.execute("DELETE FROM tags")
    conn.commit()
    conn.close()
    c, cfg = new_client(tmp_path, root, data_dir=cfg.data_dir, index_on_start=False)
    with c:
        from helpers import login, ADMIN, wait_for
        login(c, *ADMIN)
        wait_for(lambda: any(i["live"] for i in c.get("/api/assets").json()["items"]))
        item = next(i for i in c.get("/api/assets").json()["items"] if i["live"])
        assert c.get(f"/api/assets/{item['id']}/motion").content == video
        assert [t["name"] for t in c.get("/api/tags").json()["tags"]] == ["Old keyword"]


def test_migration_from_version_3(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for step in db.MIGRATIONS[:3]:
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 3")
    conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at) VALUES"
                 " (1, 'a/IMG.jpg', 'IMG.jpg', 'photo', 1, 1, 'x', '2020-01-01T00:00:00', 'x'),"
                 " (2, 'a/IMG.mov', 'IMG.mov', 'video', 1, 1, 'y', '2020-01-01T00:00:00', 'x'),"
                 " (3, 'a/b.png', 'b.png', 'photo', 1, 1, 'z', '2020-01-01T00:00:00', 'x')")
    conn.commit()
    conn.close()
    db.init(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert dict(conn.execute("SELECT id, motion_done FROM assets").fetchall()) == {1: 0, 2: 1, 3: 1}
    assert linking.link_pairs(conn)["live"] == 1
    assert conn.execute("SELECT live_video FROM assets WHERE id = 1").fetchone()[0] == 2
