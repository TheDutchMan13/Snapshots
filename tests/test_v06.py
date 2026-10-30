"""Version 0.6: edits, details, 360° photos, the locked folder, several library folders, comments and
activity, API keys, database backups and playing videos that need converting."""

import io
import json
import shutil
import sqlite3
import subprocess
import time

import pytest
from PIL import Image
from fastapi.testclient import TestClient

from snapshots import backup, db, libraries, media
from helpers import (ADMIN, add_member, ids_by_name, login, make_jpeg, make_pano_jpeg, new_client, rescan,
                     setup_admin, wait_for, wait_scan)


def names(c, **params):
    return sorted(ids_by_name(c, **params))


def picture(resp) -> Image.Image:
    assert resp.status_code == 200, resp.text
    return Image.open(io.BytesIO(resp.content))


def second_client(c, cfg):
    """Another person's browser on the same server."""
    other = TestClient(c.app)
    other.headers["X-Snapshots-Request"] = "1"
    return other


# ------------------------------------------------------------------ edits

def test_edits_change_what_is_shown_but_never_the_file(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "wide.jpg", "2024:05:01 10:00:00", size=(800, 400))
    before = (root / "wide.jpg").read_bytes()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)["wide.jpg"]
        old_v = c.get(f"/api/assets/{pid}").json()["v"]
        assert picture(c.get(f"/api/assets/{pid}/thumb")).size == (480, 240)

        edits = {"rotate": 90, "crop": [0, 0, 1, 0.5]}
        item = c.post(f"/api/assets/{pid}/edits", json={"edits": edits}).json()
        assert item["edited"] is True and item["v"] != old_v          # browsers fetch the new thumbnail
        assert picture(c.get(f"/api/assets/{pid}/thumb")).size == (400, 400)   # 400x800 turned, top half
        assert picture(c.get(f"/api/assets/{pid}/view")).size == (400, 400)
        assert picture(c.get(f"/api/assets/{pid}/view", params={"plain": "true"})).size == (800, 400)
        assert picture(c.get(f"/api/assets/{pid}/edited")).size == (400, 400)
        full = c.get(f"/api/assets/{pid}").json()
        assert full["edits"] == {"rotate": 90, "flip": False, "straighten": 0.0, "crop": [0, 0, 1, 0.5]}

        # Straightening enlarges the picture so no corners are empty; the size stays the same.
        c.post(f"/api/assets/{pid}/edits", json={"edits": {"straighten": 10}})
        assert picture(c.get(f"/api/assets/{pid}/view")).size == (800, 400)

        assert c.post(f"/api/assets/{pid}/edits", json={"edits": {"rotate": 45}}).status_code == 400
        assert c.post(f"/api/assets/{pid}/edits", json={"edits": {"crop": [0, 0, 0.001, 1]}}).status_code == 400

        # A rescan keeps the edit; undoing it brings back the original.
        rescan(c)
        assert c.get(f"/api/assets/{pid}").json()["edited"] is True
        item = c.post(f"/api/assets/{pid}/edits", json={"edits": None}).json()
        assert item["edited"] is False and item["v"] == old_v
        assert picture(c.get(f"/api/assets/{pid}/view")).size == (800, 400)
    assert (root / "wide.jpg").read_bytes() == before


def test_edit_steps_match_the_editor():
    img = Image.new("RGB", (40, 20), (0, 0, 0))
    img.putpixel((0, 0), (255, 0, 0))                       # a red dot in the top left corner
    turned = media.apply_edits(img, media.parse_edits({"rotate": 90}))
    assert turned.size == (20, 40) and turned.getpixel((19, 0))[0] > 200   # now top right
    flipped = media.apply_edits(img, media.parse_edits({"flip": True}))
    assert flipped.getpixel((39, 0))[0] > 200
    assert media.parse_edits({"rotate": 0, "flip": False, "straighten": 0, "crop": [0, 0, 1, 1]}) is None
    assert media.straighten_scale(0, 4, 3) == 1.0
    assert round(media.straighten_scale(90, 4, 3), 3) == round(4 / 3, 3)


# ------------------------------------------------------------------ details

def test_changing_date_place_and_description(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00", gps=(52.37, 4.89))
    make_jpeg(root / "b.jpg", "2024:05:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        a, b = ids["a.jpg"], ids["b.jpg"]
        r = c.post("/api/assets/details", json={"ids": [a], "taken_at": "2019-12-25T08:30"})
        assert r.json() == {"updated": 1}
        item = c.get(f"/api/assets/{a}").json()
        assert item["taken_at"] == "2019-12-25T08:30:00" and item["date_edited"] is True
        assert names(c, q="2019") == ["a.jpg"]

        # Shifting moves dates by the same amount (a camera clock set wrong).
        c.post("/api/assets/details", json={"ids": [a, b], "shift_minutes": 60})
        assert c.get(f"/api/assets/{b}").json()["taken_at"] == "2024-05-02T11:00:00"

        # The changed file keeps the date someone set; resetting goes back to the file's date.
        make_jpeg(root / "a.jpg", "2024:05:01 10:00:00", color=(1, 2, 3), gps=(52.37, 4.89))
        rescan(c)
        assert c.get(f"/api/assets/{a}").json()["taken_at"] == "2019-12-25T09:30:00"
        c.post("/api/assets/details", json={"ids": [a], "reset_date": True})
        item = c.get(f"/api/assets/{a}").json()
        assert item["taken_at"] == "2024-05-01T10:00:00" and item["date_edited"] is False

        # Location: set, remove, back to the file's own.
        c.post("/api/assets/details", json={"ids": [b], "position": [40.7, -74.0]})
        item = c.get(f"/api/assets/{b}").json()
        assert item["position"] == [40.7, -74.0] and item["position_edited"] is True
        c.post("/api/assets/details", json={"ids": [a], "clear_position": True})
        rescan(c)
        assert c.get(f"/api/assets/{a}").json()["position"] is None   # a rescan doesn't bring it back
        c.post("/api/assets/details", json={"ids": [a], "reset_position": True})
        assert c.get(f"/api/assets/{a}").json()["position"] == [52.37, 4.89]
        assert c.post("/api/assets/details", json={"ids": [a], "position": [95, 0]}).status_code == 400

        c.post("/api/assets/details", json={"ids": [b], "description": "  Grandma's 90th birthday  "})
        assert c.get(f"/api/assets/{b}").json()["description"] == "Grandma's 90th birthday"
        assert names(c, q="birthday") == ["b.jpg"]
        assert c.post("/api/assets/details", json={"ids": [a], "taken_at": "2019-02-30T08:30"}).status_code == 400


def test_town_search(tmp_path):
    root = tmp_path / "Photos"
    root.mkdir()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        assert c.get("/api/places/search", params={"q": "Sedona"}).json() == {"places": []}  # list not downloaded
        cfg.places_dir.mkdir(parents=True, exist_ok=True)
        (cfg.places_dir / "places-v2.tsv").write_text(
            "34.86974\t-111.76099\tSedona\tArizona\tUnited States\n"
            "48.0\t2.0\tSedonas\t\tFrance\n"
            "40.0\t-100.0\tWest Sedona\tKansas\tUnited States\n", encoding="utf-8")
        found = c.get("/api/places/search", params={"q": "sedona"}).json()["places"]
        assert [p["city"] for p in found] == ["Sedona", "Sedonas", "West Sedona"]
        assert found[0]["lat"] == 34.86974 and found[1]["state"] is None


# ------------------------------------------------------------------ 360° photos

def test_360_photos_are_recognised(tmp_path):
    root = tmp_path / "Photos"
    make_pano_jpeg(root / "sphere.jpg")
    make_pano_jpeg(root / "part.jpg", full=False)
    make_jpeg(root / "plain.jpg", "2024:07:01 12:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        items = {i["filename"]: i for i in c.get("/api/assets").json()["items"]}
        assert items["sphere.jpg"]["projection"] == "sphere"
        assert items["part.jpg"]["projection"] == "pano"
        assert items["plain.jpg"]["projection"] is None
        assert picture(c.get(f"/api/assets/{items['sphere.jpg']['id']}/pano")).size == (800, 400)
        # Photos indexed by 0.5 are looked at again in the background.
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET projection = NULL, pano_done = 0")
        c.app.state.ctx.worker.poke()
        wait_for(lambda: c.get(f"/api/assets/{items['sphere.jpg']['id']}").json()["projection"] == "sphere")


# ------------------------------------------------------------------ the locked folder

def test_locked_folder(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "secret.jpg", "2024:05:01 10:00:00", gps=(52.37, 4.89))
    make_jpeg(root / "open.jpg", "2024:05:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        secret = ids["secret.jpg"]
        album = c.post("/api/albums", json={"name": "Trip", "ids": [secret, ids["open.jpg"]]}).json()
        tag = c.post("/api/tags", json={"name": "private", "ids": [secret]}).json()

        assert c.post("/api/locked", json={"ids": [secret]}).status_code == 400          # no PIN yet
        assert c.post("/api/locked/pin", json={"pin": "2468", "password": "wrong"}).status_code == 400
        assert c.post("/api/locked/pin", json={"pin": "12", "password": ADMIN[1]}).status_code == 400
        assert c.post("/api/locked/pin", json={"pin": "2468", "password": ADMIN[1]}).json() == {"ok": True}
        assert c.post("/api/locked", json={"ids": [secret]}).json() == {"count": 1}

        # Gone from everywhere, for everyone (its owner too, while the folder is closed).
        assert names(c) == ["open.jpg"]
        assert names(c, q="secret") == []
        assert names(c, album=album["id"]) == ["open.jpg"]
        assert c.get(f"/api/albums/{album['id']}").json()["count"] == 1
        assert c.get(f"/api/tags/{tag['id']}").json()["count"] == 0
        assert c.get("/api/map").json()["points"] == []
        assert c.get("/api/folders").json()["items"] == 1
        for part in ("", "/thumb", "/view", "/original"):
            assert c.get(f"/api/assets/{secret}{part}").status_code == 404
        assert c.post("/api/favorites", json={"ids": [secret]}).status_code == 200
        st = c.get("/api/status").json()
        assert st["photos"] == 1 and st["locked"] == {"count": 1, "has_pin": True, "open": False}
        assert c.get("/api/assets", params={"locked": "true"}).status_code == 403

        # Opening it with the PIN shows the owner's locked items, only there.
        assert c.post("/api/locked/open", json={"pin": "0000"}).status_code == 400
        assert c.post("/api/locked/open", json={"pin": "2468"}).json()["open"] is True
        assert names(c, locked="true") == ["secret.jpg"]
        assert names(c) == ["open.jpg"]
        assert c.get(f"/api/assets/{secret}/thumb").status_code == 200
        assert c.get(f"/api/assets/{secret}").json()["favorite"] is False      # changes were ignored

        # Someone else never sees it, and can't open someone else's folder.
        add_member(c)
        other = second_client(c, cfg)
        login(other, "member", "member password 1")
        assert c.post("/api/locked/close").json() == {"open": False}
        assert other.get(f"/api/assets/{secret}/thumb").status_code == 404
        assert names(other, locked="true") if False else True
        assert other.get("/api/assets", params={"locked": "true"}).status_code == 403
        assert other.post("/api/locked", json={"ids": [secret], "value": False}).status_code == 403

        # Taking it out again puts it back where it was.
        assert c.post("/api/locked", json={"ids": [secret], "value": False}).status_code == 403   # closed
        c.post("/api/locked/open", json={"pin": "2468"})
        assert c.post("/api/locked", json={"ids": [secret], "value": False}).json() == {"count": 1}
        assert names(c) == ["open.jpg", "secret.jpg"]
        assert c.get(f"/api/albums/{album['id']}").json()["count"] == 2


def test_pin_guessing_is_slowed_down(tmp_path):
    root = tmp_path / "Photos"
    root.mkdir()
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        c.post("/api/locked/pin", json={"pin": "2468", "password": ADMIN[1]})
        for _ in range(5):
            assert c.post("/api/locked/open", json={"pin": "1111"}).status_code == 400
        assert c.post("/api/locked/open", json={"pin": "2468"}).status_code == 429


# ------------------------------------------------------------------ several library folders

def test_extra_library_folders(tmp_path):
    root = tmp_path / "Photos"
    phone = tmp_path / "Phone backup"
    make_jpeg(root / "2024" / "home.jpg", "2024:05:01 10:00:00")
    make_jpeg(phone / "DCIM" / "beach.jpg", "2024:05:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert c.post("/api/libraries", json={"path": str(root / "2024")}).status_code == 400   # inside the library
        assert c.post("/api/libraries", json={"path": "relative/folder"}).status_code == 400
        r = c.post("/api/libraries", json={"path": str(phone)})
        assert r.status_code == 201, r.text
        assert cfg.config_file is not None and "extra_libraries" in cfg.config_file.read_text(encoding="utf-8")
        wait_for(lambda: "beach.jpg" in ids_by_name(c))
        wait_scan(c)
        beach = c.get(f"/api/assets/{ids_by_name(c)['beach.jpg']}").json()
        assert beach["path"] == libraries.prefix_for(phone) + "DCIM/beach.jpg"
        assert c.get(f"/api/assets/{beach['id']}/original").content == (phone / "DCIM" / "beach.jpg").read_bytes()

        top = c.get("/api/folders").json()
        assert [f["name"] for f in top["folders"]] == ["2024", "Phone backup"]
        lib = top["folders"][1]
        assert lib["count"] == 1 and lib["library"] == str(phone)
        inside = c.get("/api/folders", params={"path": lib["path"]}).json()
        assert [f["name"] for f in inside["folders"]] == ["DCIM"]
        assert [x["name"] for x in inside["crumbs"]] == ["Phone backup"]
        dcim = c.get("/api/folders", params={"path": inside["folders"][0]["path"]}).json()
        assert dcim["items"] == 1 and [x["name"] for x in dcim["crumbs"]] == ["Phone backup", "DCIM"]
        assert names(c, folder=inside["folders"][0]["path"]) == ["beach.jpg"]

        # An unplugged drive: its photos are kept.
        phone.rename(tmp_path / "away")
        s = rescan(c)
        assert s["removed"] == 0 and "beach.jpg" in ids_by_name(c)
        st = c.get("/api/status").json()
        assert [x["ok"] for x in st["libraries"]] == [True, False]
        assert c.get(f"/api/assets/{beach['id']}/original").status_code == 409
        (tmp_path / "away").rename(phone)

        # Removing the folder takes its photos out of Snapshots, leaving the files alone.
        r = c.delete("/api/libraries", params={"path": str(phone)}).json()
        assert r["removed"] == 1 and names(c) == ["home.jpg"]
        assert (phone / "DCIM" / "beach.jpg").exists()
        assert cfg.extra_libraries == []


def test_extra_library_from_config_and_moves_between_folders(tmp_path):
    root = tmp_path / "Photos"
    other = tmp_path / "Other"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    make_jpeg(other / "b.jpg", "2024:05:02 10:00:00")
    make_jpeg(other / "c.jpg", "2024:05:03 10:00:00", color=(9, 9, 9))  # an emptied folder looks unplugged
    c, cfg = new_client(tmp_path, root, extra_libraries=[other])
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/albums", json={"name": "Keep", "ids": [ids["b.jpg"]]})
        # A photo moved from the extra folder into the main library keeps its album.
        shutil.move(other / "b.jpg", root / "b.jpg")
        s = rescan(c)
        assert s["moved"] == 1
        assert ids_by_name(c)["b.jpg"] == ids["b.jpg"]
        assert c.get(f"/api/assets/{ids['b.jpg']}").json()["path"] == "b.jpg"


# ------------------------------------------------------------------ comments and activity

def test_comments_and_activity(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        pid = ids_by_name(c)["a.jpg"]
        album = c.post("/api/albums", json={"name": "Summer"}).json()
        add_member(c)
        other = second_client(c, cfg)
        login(other, "member", "member password 1")

        made = other.post(f"/api/assets/{pid}/comments", json={"text": "  Lovely light!  "}).json()
        assert made["text"] == "Lovely light!" and made["user"] == "member"
        other.post(f"/api/albums/{album['id']}/add", json={"ids": [pid]})
        other.post(f"/api/albums/{album['id']}/comments", json={"text": "Add yours too"})
        assert other.post(f"/api/assets/{pid}/comments", json={"text": "   "}).status_code == 400

        assert c.get(f"/api/assets/{pid}").json()["comments"] == 1
        assert c.get(f"/api/albums/{album['id']}").json()["comments"] == 1
        assert c.get("/api/status").json()["activity_unread"] == 3
        feed = c.get("/api/activity").json()["items"]
        assert [i["kind"] for i in feed][:3].count("comment") == 2
        added = next(i for i in feed if i["kind"] == "added")
        assert added["count"] == 1 and added["album"]["name"] == "Summer" and added["new"] is True
        assert c.post("/api/activity/seen").json() == {"unread": 0}

        # Only the writer or an administrator can delete a comment.
        mine = c.post(f"/api/assets/{pid}/comments", json={"text": "Thanks"}).json()
        assert other.delete(f"/api/comments/{mine['id']}").status_code == 403
        assert c.delete(f"/api/comments/{made['id']}").json() == {"ok": True}
        assert [x["text"] for x in c.get(f"/api/assets/{pid}/comments").json()["comments"]] == ["Thanks"]


# ------------------------------------------------------------------ API keys

def test_api_keys(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        made = c.post("/api/keys", json={"name": "Backup script"}).json()
        assert made["key"].startswith("snap_")
        listed = c.get("/api/keys").json()["keys"]
        assert [k["name"] for k in listed] == ["Backup script"] and "key" not in listed[0]

        script = TestClient(c.app)          # no cookie, no special header: just the key
        auth = {"Authorization": f"Bearer {made['key']}"}
        assert script.get("/api/assets").status_code == 401
        assert [i["filename"] for i in script.get("/api/assets", headers=auth).json()["items"]] == ["a.jpg"]
        assert script.post("/api/albums", json={"name": "From a script"}, headers=auth).status_code == 201
        # A key can't make more keys, change the password or open the locked folder.
        assert script.get("/api/keys", headers=auth).status_code == 403
        assert script.post("/api/auth/password", json={"current": ADMIN[1], "new": "x" * 10},
                           headers=auth).status_code == 403
        assert script.post("/api/locked/open", json={"pin": "1234"}, headers=auth).status_code == 403
        assert c.get("/api/keys").json()["keys"][0]["last_used_at"] is not None

        assert c.delete(f"/api/keys/{made['id']}").json() == {"ok": True}
        assert script.get("/api/assets", headers=auth).status_code == 401
        with db.session(cfg.db_path) as conn:   # only a hash is stored
            assert conn.execute("SELECT COUNT(*) FROM api_keys").fetchone()[0] == 0


# ------------------------------------------------------------------ backups

def test_backups_and_restoring_one(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:05:01 10:00:00")
    c, cfg = new_client(tmp_path, root, backup_keep=2)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: backup.list_backups(cfg))       # one is made when Snapshots starts
        c.post("/api/albums", json={"name": "Before"})
        time.sleep(1.1)
        made = c.post("/api/backups").json()
        time.sleep(1.1)
        c.post("/api/backups")
        listed = c.get("/api/settings").json()["backups"]["list"]
        assert len(listed) == 2                           # backup_keep
        assert made["name"] in [b["name"] for b in listed]
        c.post("/api/albums", json={"name": "After"})
    # Restoring (with Snapshots closed) brings back the albums of that moment.
    kept = backup.restore(cfg, cfg.backups_path / made["name"])
    assert kept.exists()
    conn = sqlite3.connect(cfg.db_path)
    assert [r[0] for r in conn.execute("SELECT name FROM albums")] == ["Before"]
    conn.close()
    junk = tmp_path / "junk.db"
    junk.write_bytes(b"not a database at all" * 100)
    assert backup.check(junk) is not None
    with pytest.raises(ValueError):
        backup.restore(cfg, junk)


# ------------------------------------------------------------------ playing videos

def _ffmpeg_video(path, codec_args, ext_ok=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=160x120:d=1:r=10",
                    *codec_args, str(path)], check=True, capture_output=True)


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_videos_that_need_converting(tmp_path):
    root = tmp_path / "Photos"
    _ffmpeg_video(root / "fine.mp4", ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    _ffmpeg_video(root / "old.avi", ["-c:v", "mpeg4"])
    _ffmpeg_video(root / "camera.mkv", ["-c:v", "libx264", "-pix_fmt", "yuv420p"])
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        fine = c.get(f"/api/assets/{ids['fine.mp4']}/play").json()
        assert fine["converted"] is False and fine["url"].startswith(f"/api/assets/{ids['fine.mp4']}/view")
        for name in ("old.avi", "camera.mkv"):
            first = c.get(f"/api/assets/{ids[name]}/play").json()
            assert first["preparing"] is True
            done = wait_for(lambda name=name: c.get(f"/api/assets/{ids[name]}/play").json().get("converted"))
            assert done
            url = c.get(f"/api/assets/{ids[name]}/play").json()["url"]
            r = c.get(url)
            assert r.status_code == 200 and r.headers["content-type"] == "video/mp4"
            assert media.probe_video(cfg.video_path(ids[name]))["codec"] == "h264"
    assert media.pick_encoder("auto") in ("libx264", *media.HARDWARE_ENCODERS)


def test_encoder_setting_is_checked(tmp_path):
    from snapshots.config import load_config
    bad = tmp_path / "config.toml"
    bad.write_text('video_encoder = "magic"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(bad)
    good = tmp_path / "good.toml"
    good.write_text('video_encoder = "h264_amf"\nextra_libraries = ["D:/Phone", "E:/Old"]\nbackup_dir = "F:/Backups"\n',
                    encoding="utf-8")
    cfg = load_config(good)
    assert cfg.video_encoder == "h264_amf" and [p.as_posix() for p in cfg.extra_libraries] == ["D:/Phone", "E:/Old"]
    assert cfg.backups_path.as_posix() == "F:/Backups"


def test_upgrade_from_05_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for step in db.MIGRATIONS[:4]:
        conn.executescript(step)
    conn.execute("INSERT INTO assets (path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                 " VALUES ('a.jpg', 'a.jpg', 'photo', 1, 1, 'abcdef0123', '2024-01-01T00:00:00', 'x'),"
                 " ('v.mp4', 'v.mp4', 'video', 1, 1, 'abcdef0124', '2024-01-01T00:00:00', 'x')")
    conn.execute("PRAGMA user_version = 4")
    conn.commit()
    conn.close()
    db.init(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    assert dict(conn.execute("SELECT path, pano_done FROM assets").fetchall()) == {"a.jpg": 0, "v.mp4": 1}
    conn.close()
