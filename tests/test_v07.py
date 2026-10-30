"""0.7: smart albums, exports, trimming videos, webhooks, offsite copies, Immich and Google Photos imports."""

import base64
import hashlib
import hmac
import io
import json
import os
import re
import shutil
import sqlite3
import stat
import subprocess
import sys
import time
import zipfile
from pathlib import Path

import pytest
from PIL import Image

from snapshots import db, faces, immich, offsite, takeout, webhooks
from helpers import (ADMIN, add_member, ids_by_name, jpeg_bytes, login, make_jpeg, new_client, rescan, setup_admin,
                     wait_for, wait_scan)

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))


def names(c, **params):
    return sorted(ids_by_name(c, **params))


def sha1(path: Path) -> str:
    return hashlib.sha1(path.read_bytes()).hexdigest()


def library(tmp_path) -> Path:
    root = tmp_path / "Photos"
    make_jpeg(root / "2023" / "beach.jpg", "2023:07:04 12:00:00", color=(10, 120, 200))
    make_jpeg(root / "2023" / "party.jpg", "2023:12:31 23:00:00", color=(200, 20, 20))
    make_jpeg(root / "2024" / "hike.jpg", "2024:05:01 09:30:00", color=(20, 160, 40), gps=(33.45, -112.07))
    make_jpeg(root / "2024" / "dog.jpg", "2024:06:15 16:00:00", color=(120, 90, 30))
    return root


def add_face(cfg, asset_id: int, box, person_name=None) -> tuple[int, int]:
    """A face in a photo, as face recognition would have stored it (the tests don't run the models)."""
    with db.session(cfg.db_path) as conn:
        person = conn.execute("INSERT INTO people (name, created_at) VALUES (?, '2024-01-01')", (person_name,)).lastrowid
        face = conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                            " VALUES (?, ?, ?, ?, ?, ?, 0.99, ?, '2024-01-01')",
                            (asset_id, person, *box, bytes(512))).lastrowid
        conn.execute("UPDATE assets SET faces_done = 1 WHERE id = ?", (asset_id,))
    return face, person


# ------------------------------------------------------------------ smart albums

def test_smart_albums_fill_themselves(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        _, sam = add_face(cfg, ids["beach.jpg"], (0.1, 0.1, 0.2, 0.2), "Sam")
        add_face(cfg, ids["hike.jpg"], (0.1, 0.1, 0.2, 0.2), None)
        with db.session(cfg.db_path) as conn:   # the same person in the hike photo
            conn.execute("UPDATE faces SET person_id = ? WHERE asset_id = ?", (sam, ids["hike.jpg"]))

        r = c.post("/api/albums", json={"name": "Sam", "rule": {"people": [sam]}})
        assert r.status_code == 201, r.text
        album = r.json()
        assert album["smart"] and album["count"] == 2 and "Sam" in album["rule_text"]
        assert names(c, album=album["id"]) == ["beach.jpg", "hike.jpg"]

        # rules combine: Sam in 2024
        r = c.patch(f"/api/albums/{album['id']}", json={"rule": {"people": [sam], "start": "2024-01-01"}})
        assert r.status_code == 200 and r.json()["count"] == 1
        assert names(c, album=album["id"]) == ["hike.jpg"]
        assert c.get("/api/timeline", params={"album": album["id"]}).json()["months"] == [{"month": "2024-05", "count": 1}]

        # new photos join by themselves; photos can't be added by hand
        words = c.post("/api/albums", json={"name": "2023", "rule": {"words": "2023"}}).json()
        assert words["count"] == 2
        make_jpeg(root / "2023" / "late.jpg", "2023:11:01 10:00:00")
        rescan(c)
        assert c.get(f"/api/albums/{words['id']}").json()["count"] == 3
        r = c.post(f"/api/albums/{words['id']}/add", json={"ids": [ids["dog.jpg"]]})
        assert r.status_code == 400
        # searching inside a smart album narrows it
        assert names(c, album=words["id"], q="party") == ["party.jpg"]

        # favourites in a rule are each person's own
        fav = c.post("/api/albums", json={"name": "Loved", "rule": {"favorite": True}}).json()
        assert fav["count"] == 0
        c.post("/api/favorites", json={"ids": [ids["dog.jpg"]], "value": True})
        assert names(c, album=fav["id"]) == ["dog.jpg"]

        # nonsense rules are refused
        for rule in ({}, {"kind": "gif"}, {"start": "yesterday"}, {"start": "2024-02-01", "end": "2024-01-01"},
                     {"people": ["x"]}):
            assert c.post("/api/albums", json={"name": "Bad", "rule": rule}).status_code == 400, rule
        # an ordinary album isn't turned into a smart one
        plain = c.post("/api/albums", json={"name": "Plain", "ids": [ids["dog.jpg"]]}).json()
        assert not plain["smart"]
        assert c.patch(f"/api/albums/{plain['id']}", json={"rule": {"words": "x"}}).status_code == 400

        # locked items never show in a smart album
        c.post("/api/locked/pin", json={"pin": "2468", "password": ADMIN[1]})
        c.post("/api/locked/open", json={"pin": "2468"})
        assert c.post("/api/locked", json={"ids": [ids["party.jpg"]], "value": True}).status_code == 200
        assert "party.jpg" not in names(c, album=words["id"])
        assert c.get(f"/api/albums/{words['id']}").json()["count"] == 2


def test_smart_album_by_content_waits_for_search(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        album = c.post("/api/albums", json={"name": "Beach", "rule": {"look": "beach"}}).json()
        assert album["count"] == 0 and album["waiting"] is True
        assert names(c, album=album["id"]) == []


# ------------------------------------------------------------------ exports

def test_zip_export(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        album = c.post("/api/albums", json={"name": "Trip / 2023", "ids": [ids["beach.jpg"], ids["hike.jpg"]]}).json()
        c.post(f"/api/assets/{ids['hike.jpg']}/edits", json={"edits": {"rotate": 90}})
        r = c.post("/api/exports", json={"album": album["id"], "format": "zip"})
        assert r.status_code == 201, r.text
        url = r.json()["url"]
        got = c.get(url)
        assert got.status_code == 200 and got.headers["content-type"] == "application/zip"
        assert "Trip _ 2023.zip" in got.headers["content-disposition"]
        zf = zipfile.ZipFile(io.BytesIO(got.content))
        assert zf.namelist() == ["2023-07-04 12.00.00 beach.jpg", "2024-05-01 09.30.00 hike (edited).jpg"]
        assert zf.read("2023-07-04 12.00.00 beach.jpg") == (root / "2023" / "beach.jpg").read_bytes()
        turned = Image.open(io.BytesIO(zf.read("2024-05-01 09.30.00 hike (edited).jpg")))
        assert turned.size == (480, 640)
        assert zf.getinfo("2023-07-04 12.00.00 beach.jpg").date_time == (2023, 7, 4, 12, 0, 0)

        # originals instead of edits; a selection instead of an album
        r = c.post("/api/exports", json={"ids": [ids["hike.jpg"], ids["dog.jpg"]], "edited": False})
        zf = zipfile.ZipFile(io.BytesIO(c.get(r.json()["url"]).content))
        assert zf.namelist() == ["2024-05-01 09.30.00 hike.jpg", "2024-06-15 16.00.00 dog.jpg"]

        # someone else can't use the download; missing files are listed, not fatal
        add_member(c, "anna", "anna password 1")
        other = c.__class__(c.app)
        other.headers["X-Snapshots-Request"] = "1"
        login(other, "anna", "anna password 1")
        assert other.get(url).status_code == 404
        (root / "2023" / "beach.jpg").unlink()
        zf = zipfile.ZipFile(io.BytesIO(c.get(url).content))
        assert "Not included.txt" in zf.namelist() and b"beach.jpg" in zf.read("Not included.txt")

        # locked items stay out of other people's exports
        assert c.post("/api/exports", json={"ids": [999999]}).status_code == 400


def _pdf_pages(data: bytes) -> int:
    assert data.startswith(b"%PDF-1.4") and data.rstrip().endswith(b"%%EOF")
    # every object the cross-reference table points at really starts there
    xref = int(re.search(rb"startxref\n(\d+)", data).group(1))
    table = data[xref:].split(b"trailer")[0].split(b"\n")[2:]
    offsets = [int(line[:10]) for line in table if line.strip()][1:]
    for n, off in enumerate(offsets, start=1):
        assert data[off:off + 20].startswith(f"{n} 0 obj".encode()), n
    return int(re.search(rb"/Type /Pages /Kids \[[^\]]*\] /Count (\d+)", data).group(1))


def test_pdf_photo_book(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/assets/details", json={"ids": [ids["beach.jpg"]], "description": "Swimming all day"})
        album = c.post("/api/albums", json={"name": "Année 2023 ✓", "ids": list(ids.values())}).json()
        r = c.post("/api/exports", json={"album": album["id"], "format": "pdf", "per_page": 2, "paper": "a4"})
        assert r.status_code == 201, r.text
        task = r.json()
        done = wait_for(lambda: (t := c.get(f"/api/tasks/{task['id']}").json())["done"] and t)
        assert done["error"] is None, done
        assert done["result"]["photos"] == 4 and done["result"]["pages"] == 3
        got = c.get(f"/api/tasks/{task['id']}/file")
        assert got.status_code == 200 and got.headers["content-type"] == "application/pdf"
        assert _pdf_pages(got.content) == 3
        assert (b"/MediaBox [0 0 595.44 841.68]") in got.content          # A4
        if shutil.which("pdfinfo"):
            path = tmp_path / "book.pdf"
            path.write_bytes(got.content)
            info = subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True).stdout
            assert re.search(r"Pages:\s+3\n", info) and "Année 2023 ✓" in info
        assert c.post("/api/exports", json={"album": album["id"], "format": "pdf", "per_page": 5}).status_code == 400


# ------------------------------------------------------------------ trimming videos and stills

@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_trim_video_and_save_a_still(tmp_path):
    root = tmp_path / "Photos"
    root.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=s=320x240:d=4:r=10", "-f", "lavfi",
                    "-i", "sine=d=4", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    str(root / "clip.mp4")], check=True, capture_output=True)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        vid = ids_by_name(c)["clip.mp4"]
        c.post("/api/assets/details", json={"ids": [vid], "taken_at": "2024-08-01T10:00:00", "position": [51.5, -0.12],
                                           "description": "Fireworks"})
        album = c.post("/api/albums", json={"name": "Summer", "ids": [vid]}).json()
        before = (root / "clip.mp4").read_bytes()

        r = c.post(f"/api/assets/{vid}/trim", json={"start": 1, "end": 2.5})
        assert r.status_code == 202, r.text
        done = wait_for(lambda: (t := c.get(f"/api/tasks/{r.json()['id']}").json())["done"] and t, timeout=120)
        assert done["error"] is None, done
        new = done["result"]["id"]
        item = c.get(f"/api/assets/{new}").json()
        assert item["filename"] == "clip (trimmed 0m01s-0m02s).mp4" and item["kind"] == "video"
        assert item["taken_at"] == "2024-08-01T10:00:01"
        assert item["position"] == [51.5, -0.12] and item["description"] == "Fireworks"
        assert [a["name"] for a in item["albums"]] == ["Summer"]
        from snapshots import media
        length = media.probe_video(root / item["filename"])["duration"]
        assert 1.3 <= length <= 1.8
        assert (root / "clip.mp4").read_bytes() == before        # the original is untouched

        r = c.post(f"/api/assets/{vid}/still", json={"at": 2})
        assert r.status_code == 201, r.text
        still = r.json()
        assert still["filename"] == "clip (still 0m02s).jpg" and still["kind"] == "photo"
        assert still["taken_at"] == "2024-08-01T10:00:02" and (still["width"], still["height"]) == (320, 240)
        exif = Image.open(root / still["filename"]).getexif()
        assert exif.get_ifd(0x8769)[36867] == "2024:08:01 10:00:02"
        assert c.get(f"/api/albums/{album['id']}").json()["count"] == 3

        assert c.post(f"/api/assets/{vid}/trim", json={"start": 2, "end": 2.2}).status_code == 400
        assert c.post(f"/api/assets/{vid}/trim", json={"start": 9, "end": 12}).status_code == 400
        assert c.post(f"/api/assets/{still['id']}/still", json={"at": 0}).status_code == 400   # not a video


# ------------------------------------------------------------------ webhooks

class Receiver:
    def __init__(self, status=200):
        self.got = []
        self.status = status

    def __call__(self, url, body, headers):
        self.got.append((url, json.loads(body), headers, body))
        return self.status


def test_webhooks(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        sender = c.app.state.ctx.webhooks
        sender.stop()   # deliveries are made by hand below
        sender._thread.join(5)
        sender._stop = False
        assert c.post("/api/webhooks", json={"url": "ftp://x", "events": ["assets.added"]}).status_code == 400
        assert c.post("/api/webhooks", json={"url": "http://ha.local/hook", "events": ["nope"]}).status_code == 400
        r = c.post("/api/webhooks", json={"url": "http://ha.local/api/webhook/abc",
                                          "events": ["assets.added", "album.added", "comment.added", "person.named"]})
        assert r.status_code == 201, r.text
        hook = r.json()
        secret = hook["secret"]
        assert secret.startswith("whsec_") and "secret" not in c.get("/api/webhooks").json()["webhooks"][0]

        files = {"files": ("new.jpg", jpeg_bytes((9, 9, 9)), "image/jpeg")}
        c.post("/api/upload", files=files)
        album = c.post("/api/albums", json={"name": "Holiday", "ids": [ids["beach.jpg"], ids["hike.jpg"]]}).json()
        c.post(f"/api/assets/{ids['beach.jpg']}/comments", json={"text": "Lovely"})
        _, person = add_face(cfg, ids["dog.jpg"], (0.1, 0.1, 0.3, 0.3))
        c.patch(f"/api/people/{person}", json={"name": "Rex"})

        rec = Receiver()
        sender.opener = rec
        while sender.deliver_due():
            pass
        events = [g[1]["event"] for g in rec.got]
        assert events == ["assets.added", "album.added", "comment.added", "person.named"]
        added = rec.got[0][1]["data"]
        assert added["how"] == "upload" and added["items"][0]["filename"] == "new.jpg"
        assert rec.got[1][1]["data"]["album"]["name"] == "Holiday" and rec.got[1][1]["data"]["count"] == 2
        assert rec.got[3][1]["data"] == {"id": person, "name": "Rex", "before": None}
        url, payload, headers, body = rec.got[0]
        assert url == "http://ha.local/api/webhook/abc" and headers["X-Snapshots-Event"] == "assets.added"
        expected = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        assert headers["X-Snapshots-Signature"] == expected

        # a receiver that is down: tried again later, then given up
        down = Receiver(status=503)
        sender.opener = down
        c.post(f"/api/webhooks/{hook['id']}/test")
        sender.deliver_due()
        listed = c.get("/api/webhooks").json()["webhooks"][0]
        assert listed["last_status"] == "503" and listed["waiting"] == 1
        with db.session(cfg.db_path) as conn:
            for _ in range(len(webhooks.RETRY_AFTER)):
                conn.execute("UPDATE webhook_deliveries SET next_try_at = 0 WHERE delivered_at IS NULL")
                conn.commit()
                sender.deliver_due()
        listed = c.get("/api/webhooks").json()["webhooks"][0]
        assert listed["failed"] == 1 and listed["waiting"] == 0
        assert len(down.got) == len(webhooks.RETRY_AFTER) + 1

        # turned off: nothing is queued
        c.patch(f"/api/webhooks/{hook['id']}", json={"enabled": False})
        c.post(f"/api/assets/{ids['beach.jpg']}/comments", json={"text": "Again"})
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM webhook_deliveries WHERE event = 'comment.added'").fetchone()[0] == 1

        # only administrators manage webhooks
        add_member(c, "anna", "anna password 1")
        other = c.__class__(c.app)
        other.headers["X-Snapshots-Request"] = "1"
        login(other, "anna", "anna password 1")
        assert other.get("/api/webhooks").status_code == 403
        assert c.delete(f"/api/webhooks/{hook['id']}").status_code == 200


def test_webhooks_never_mention_locked_items(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.app.state.ctx.webhooks.stop()
        c.post("/api/webhooks", json={"url": "http://x.local/", "events": ["*"]})
        c.post("/api/locked/pin", json={"pin": "2468", "password": ADMIN[1]})
        c.post("/api/locked/open", json={"pin": "2468"})
        c.post("/api/locked", json={"ids": [ids["dog.jpg"]], "value": True})
        with db.session(cfg.db_path) as conn:
            webhooks.emit_added(conn, [ids["dog.jpg"]], "scan")
            album = c.post("/api/albums", json={"name": "A", "ids": [ids["dog.jpg"]]}).json()
            payloads = [r[0] for r in conn.execute("SELECT payload FROM webhook_deliveries")]
        assert album["count"] == 0
        assert not any("dog.jpg" in p or f'"id": {ids["dog.jpg"]}' in p for p in payloads)


# ------------------------------------------------------------------ offsite copies

def test_offsite_folder_copy(tmp_path):
    root = library(tmp_path)
    extra = tmp_path / "Old phone"
    make_jpeg(extra / "old.jpg", "2015:01:01 10:00:00")
    (root / "notes.txt").write_text("keep me")
    dest = tmp_path / "Backup drive" / "Photo copy"
    (tmp_path / "Backup drive").mkdir()
    c, cfg = new_client(tmp_path, root, extra_libraries=[extra])
    with c:
        setup_admin(c)
        wait_scan(c)
        # refused: inside the library, or not a full path
        assert c.post("/api/offsite", json={"method": "folder", "target": str(root / "copy")}).status_code == 400
        assert c.post("/api/offsite", json={"method": "folder", "target": "copy"}).status_code == 400
        assert c.post("/api/offsite", json={"method": "zip", "target": str(dest)}).status_code == 400
        r = c.post("/api/offsite", json={"method": "folder", "target": str(dest), "hours": 12})
        assert r.status_code == 200, r.text
        assert 'offsite_method = "folder"' in cfg.config_file.read_text()
        job = c.app.state.ctx.worker.offsite
        wait_for(lambda: not job.running() and job.state.get("last"))
        assert job.state["result"]["copied"] == 6 and job.state["message"] is None
        assert (dest / "Library" / "2023" / "beach.jpg").read_bytes() == (root / "2023" / "beach.jpg").read_bytes()
        assert (dest / "Library" / "notes.txt").read_text() == "keep me"
        assert (dest / "Old phone" / "old.jpg").is_file()
        assert sqlite3.connect(dest / "Database" / "snapshots.db").execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 5
        assert (dest / "README.txt").is_file()

        # second run: only what changed; deleted files stay in the copy
        (root / "2024" / "dog.jpg").unlink()
        make_jpeg(root / "2024" / "cat.jpg", "2024:09:09 09:00:00")
        assert c.post("/api/offsite/run").status_code == 202
        wait_for(lambda: not job.running())
        result = c.get("/api/settings").json()["offsite"]["state"]["result"]
        assert result["copied"] == 1 and result["unchanged"] == 5
        assert (dest / "Library" / "2024" / "dog.jpg").is_file() and (dest / "Library" / "2024" / "cat.jpg").is_file()

        # an unplugged drive: a clear message, nothing else happens
        cfg.offsite_target = str(tmp_path / "Z-drive" / "copy")
        c.post("/api/offsite/run")
        wait_for(lambda: not job.running())
        assert "can't be reached" in job.state["message"]


def _fake_tool(folder: Path, name: str, log: Path, code: int = 0):
    folder.mkdir(parents=True, exist_ok=True)
    script = folder / name
    script.write_text(f"#!{sys.executable}\nimport sys, json\n"
                      f"open({str(log)!r}, 'a').write(json.dumps(sys.argv[1:]) + '\\n')\n"
                      f"sys.exit({code} if 'init' not in sys.argv and 'config' not in sys.argv else 0)\n")
    script.chmod(script.stat().st_mode | stat.S_IEXEC)


@pytest.mark.skipif(os.name == "nt", reason="fake tools are shell scripts")
def test_offsite_with_rclone_and_restic(tmp_path, monkeypatch):
    root = library(tmp_path)
    bin_dir, log = tmp_path / "bin", tmp_path / "calls.log"
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.delenv("RESTIC_PASSWORD", raising=False)
    monkeypatch.delenv("RESTIC_PASSWORD_FILE", raising=False)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert "isn't installed" in c.post("/api/offsite", json={"method": "rclone", "target": "b2:photos"}).json()["detail"]
        _fake_tool(bin_dir, "rclone", log)
        _fake_tool(bin_dir, "restic", log)
        assert c.post("/api/offsite", json={"method": "rclone", "target": "b2:photos"}).status_code == 200
        job = c.app.state.ctx.worker.offsite
        wait_for(lambda: not job.running() and job.state.get("last"))
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert calls[0][:3] == ["copy", str(root), "b2:photos/Library"] and "--exclude" in calls[0]
        assert calls[1][0] == "copyto" and calls[1][2] == "b2:photos/Database/snapshots.db"

        assert "password" in c.post("/api/offsite", json={"method": "restic", "target": "/r"}).json()["detail"]
        pw = tmp_path / "pw.txt"
        pw.write_text("secret")
        cfg.offsite_password_file = pw
        log.unlink()
        assert c.post("/api/offsite", json={"method": "restic", "target": str(tmp_path / "repo")}).status_code == 200
        c.post("/api/offsite/run")
        wait_for(lambda: not job.running() and log.exists() and len(log.read_text().splitlines()) >= 2)
        wait_for(lambda: not job.running())
        calls = [json.loads(x) for x in log.read_text().splitlines()]
        assert calls[0][-2:] == ["cat", "config"] and calls[1][2] == "backup" and str(root) in calls[1]

        _fake_tool(bin_dir, "rclone", log, code=7)
        c.post("/api/offsite", json={"method": "rclone", "target": "b2:photos"})
        c.post("/api/offsite/run")
        wait_for(lambda: not job.running() and job.state.get("message"))
        assert "rclone failed (7)" in job.state["message"]


# ------------------------------------------------------------------ names from imports

def test_import_names_find_their_faces(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        left, p1 = add_face(cfg, ids["party.jpg"], (0.10, 0.20, 0.20, 0.25))
        right, p2 = add_face(cfg, ids["party.jpg"], (0.60, 0.20, 0.20, 0.25))
        _, named = add_face(cfg, ids["dog.jpg"], (0.3, 0.3, 0.2, 0.2), "Grandma")
        with db.session(cfg.db_path) as conn:
            conn.execute("INSERT INTO face_hints (asset_id, name, x, y, w, h, source) VALUES"
                         " (?, 'Ben', 0.62, 0.18, 0.15, 0.3, 'test'), (?, 'grandma', 0.12, 0.22, 0.2, 0.2, 'test')",
                         (ids["party.jpg"], ids["party.jpg"]))
            moves = faces.apply_hints(conn, ids["party.jpg"])
            assert moves == [(left, p1, named)]                      # joins the person who has the name
            assert conn.execute("SELECT name FROM people WHERE id = ?", (p2,)).fetchone()[0] == "Ben"  # names the group
            assert conn.execute("SELECT 1 FROM people WHERE id = ?", (p1,)).fetchone() is None    # empty group gone
            assert conn.execute("SELECT COUNT(*) FROM face_hints").fetchone()[0] == 0
            # without a position, a name is only used for a photo with exactly one face
            conn.execute("INSERT INTO face_hints (asset_id, name, source) VALUES (?, 'Ann', 't'), (?, 'Bo', 't')",
                         (ids["party.jpg"], ids["dog.jpg"]))
            faces.apply_hints(conn, ids["party.jpg"])
            faces.apply_hints(conn, ids["dog.jpg"])
            assert conn.execute("SELECT COUNT(*) FROM people WHERE name IN ('Ann', 'Bo')").fetchone()[0] == 1


# ------------------------------------------------------------------ Immich

class FakeImmich:
    """Answers like Immich's API, from a list of assets."""

    def __init__(self, assets, albums=(), tags=(), files=None, version=(1, 140, 0)):
        self.assets = {a["id"]: a for a in assets}
        self.albums = {a["id"]: a for a in albums}
        self.tags = list(tags)
        self.files = files or {}
        self.version = version
        self.calls = []
        self.key = "good-key"

    def __call__(self, client_self, method, path, body=None, out=None):
        self.calls.append((method, path))
        if client_self.key != self.key:
            return 401, {"message": "Invalid API key"}
        if path == "/server/version":
            return 200, dict(zip(("major", "minor", "patch"), self.version))
        if path == "/users/me":
            return 200, {"name": "Hans", "email": "h@example.org"}
        if path.startswith("/albums"):
            if path in ("/albums", "/albums?shared=true"):
                return 200, [{"id": a["id"], "albumName": a["albumName"]} for a in self.albums.values()
                             if bool(a.get("shared")) == path.endswith("true")]
            album = self.albums[path.split("/")[2]]
            return 200, {**album, "assets": [{"id": i} for i in album["ids"]], "assetCount": len(album["ids"])}
        if path == "/tags":
            return 200, [{"id": t["id"], "name": t["name"], "value": t["value"]} for t in self.tags]
        if path == "/search/metadata":
            items = list(self.assets.values())
            vis = body.get("visibility")
            items = [a for a in items if (a.get("visibility") == "archive") == (vis == "archive")]
            if body.get("tagIds"):
                tag = next(t for t in self.tags if t["id"] == body["tagIds"][0])
                items = [a for a in items if a["id"] in tag["ids"]]
            page, size = body["page"], body["size"]
            chunk = items[(page - 1) * size: page * size]
            more = len(items) > page * size
            return 200, {"albums": {}, "assets": {"items": chunk, "count": len(chunk), "total": len(items),
                                                  "nextPage": str(page + 1) if more else None}}
        if path.endswith("/original"):
            out.write(self.files[path.split("/")[2]])
            return 200, None
        if path.startswith("/assets/"):
            return 200, self.assets.get(path.split("/")[2]) or self.files.get("meta:" + path.split("/")[2])
        return 404, {"message": "Not found"}


def _checksum(data: bytes) -> str:
    return base64.b64encode(hashlib.sha1(data).digest()).decode()


def test_import_from_immich(tmp_path, monkeypatch):
    root = library(tmp_path)
    beach, hike, party = (root / "2023" / "beach.jpg"), (root / "2024" / "hike.jpg"), (root / "2023" / "party.jpg")
    newbie = io.BytesIO()
    Image.new("RGB", (300, 200), (1, 200, 1)).save(newbie, "JPEG")
    newbie = newbie.getvalue()
    assets = [
        {"id": "a1", "type": "IMAGE", "checksum": _checksum(beach.read_bytes()), "originalFileName": "beach.jpg",
         "isFavorite": True, "isTrashed": False, "visibility": "timeline", "localDateTime": "2023-07-04T12:00:00.000Z",
         "exifInfo": {"description": "Our beach", "latitude": 36.97, "longitude": -122.03},
         "people": [{"id": "p1", "name": "Sam", "faces": [{"boundingBoxX1": 100, "boundingBoxY1": 50, "boundingBoxX2": 200,
                                                           "boundingBoxY2": 170, "imageWidth": 640, "imageHeight": 480}]},
                    {"id": "p2", "name": "", "faces": []}]},
        {"id": "a2", "type": "IMAGE", "checksum": _checksum(hike.read_bytes()), "originalFileName": "hike.jpg",
         "isFavorite": False, "isTrashed": False, "visibility": "archive", "localDateTime": "2024-05-02T08:00:00.000Z",
         "exifInfo": {"latitude": 33.45, "longitude": -112.07}, "people": []},
        {"id": "a3", "type": "IMAGE", "checksum": _checksum(party.read_bytes()), "originalFileName": "party.jpg",
         "isFavorite": False, "isTrashed": True, "visibility": "timeline", "localDateTime": "2023-12-31T23:00:00.000Z",
         "exifInfo": {}, "people": []},
        {"id": "a4", "type": "IMAGE", "checksum": _checksum(newbie), "originalFileName": "IMG_0001.jpg",
         "isFavorite": True, "isTrashed": False, "visibility": "timeline", "localDateTime": "2022-03-05T10:00:00.000Z",
         "exifInfo": {"fileSizeInByte": len(newbie)}, "people": []},
        {"id": "a5", "type": "OTHER", "checksum": _checksum(b"pdf"), "originalFileName": "doc.pdf",
         "visibility": "timeline", "localDateTime": "2022-03-05T10:00:00.000Z", "exifInfo": {}, "people": []},
    ]
    fake = FakeImmich(assets, albums=[{"id": "al1", "albumName": "Summer", "ids": ["a1", "a2", "a4"]},
                                      {"id": "al2", "albumName": "Shared with me", "ids": ["a1"], "shared": True}],
                      tags=[{"id": "t1", "name": "Kids", "value": "Family/Kids", "ids": ["a1"]}],
                      files={"a4": newbie})
    monkeypatch.setattr(immich.Client, "_urllib", lambda self, *a, **k: fake(self, *a, **k))
    monkeypatch.setattr(immich, "PAGE", 2)    # several pages
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        face, _ = add_face(cfg, ids["beach.jpg"], (0.16, 0.1, 0.15, 0.25))
        c.post("/api/albums", json={"name": "summer", "ids": [ids["dog.jpg"]]})   # an album of that name exists

        assert c.post("/api/imports/immich", json={"url": "localhost:2283", "key": "x"}).status_code == 400
        r = c.post("/api/imports/immich", json={"url": "http://localhost:2283", "key": "bad", "dry_run": True})
        assert r.status_code == 202
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert "refused the API key" in state["error"]

        r = c.post("/api/imports/immich", json={"url": "http://localhost:2283/", "key": "good-key", "dry_run": True})
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert state["error"] is None, state
        s = state["summary"]
        assert (s["assets"], s["matched"], s["missing"], s["skipped"]) == (5, 3, 1, 1)
        assert s["immich_version"] == "1.140.0" and s["missing_bytes"] == len(newbie)
        assert names(c, favorite="true") == []       # a dry run changes nothing

        c.post("/api/imports/immich", json={"url": "http://localhost:2283", "key": "good-key"})
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert state["error"] is None, state
        s = state["summary"]
        assert s["copied"] == 1 and s["favourites"] == 2 and s["archived"] == 1 and s["trashed"] == 1
        new_file = root / "From Immich" / "2022" / "03" / "IMG_0001.jpg"
        assert new_file.read_bytes() == newbie
        ids = ids_by_name(c)
        assert names(c, favorite="true") == ["IMG_0001.jpg", "beach.jpg"]
        beach_item = c.get(f"/api/assets/{ids['beach.jpg']}").json()
        assert beach_item["description"] == "Our beach" and beach_item["position"] == [36.97, -122.03]
        assert sorted(a["name"] for a in beach_item["albums"]) == ["Shared with me", "summer"]
        assert [t["name"] for t in beach_item["tags"]] == ["Family/Kids"]
        assert [p["name"] for p in beach_item["people"]] == ["Sam"]
        hike_item = c.get(f"/api/assets/{ids['hike.jpg']}").json()
        assert hike_item["archived"] and hike_item["taken_at"] == "2024-05-02T08:00:00" and hike_item["date_edited"]
        assert hike_item["position_edited"] is False          # the same place as the file says: left alone
        assert names(c, trashed="true") == ["party.jpg"]
        albums = {a["name"]: a for a in c.get("/api/albums").json()["albums"]}
        assert set(albums) == {"summer", "Shared with me"} and albums["summer"]["count"] == 4   # added to, not doubled
        hist = c.get("/api/imports").json()["history"]
        assert hist[0]["kind"] == "immich" and hist[0]["summary"]["copied"] == 1

        # running it again changes nothing and copies nothing
        c.post("/api/imports/immich", json={"url": "http://localhost:2283", "key": "good-key"})
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        s = state["summary"]
        assert (s["copied"], s["favourites"], s["album_items"], s["albums"], s["dates"]) == (0, 0, 0, 0, 0)


def test_immich_import_is_for_administrators(tmp_path):
    root = library(tmp_path)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        add_member(c, "anna", "anna password 1")
        other = c.__class__(c.app)
        other.headers["X-Snapshots-Request"] = "1"
        login(other, "anna", "anna password 1")
        assert other.post("/api/imports/immich", json={"url": "http://x", "key": "k"}).status_code == 403
        assert other.post("/api/imports/takeout", json={"path": "/x"}).status_code == 403
        assert other.get("/api/imports").status_code == 403


# ------------------------------------------------------------------ Google Takeout

def test_takeout_sidecar_names():
    cars = {
        "IMG_0001.jpg.supplemental-metadata.json": {"n": 1},
        "IMG_0002.jpg.json": {"n": 2},
        "IMG_0003.jpg.supplemental-metadata(1).json": {"n": 3},
        "IMG_0003.jpg.supplemental-metadata.json": {"n": 4},
        "Screenshot_20210101-101010_Some Long App Na.json": {"n": 5},
        "PXL_20230101_120000000.MP.jpg.supplemental-met.json": {"n": 6},
        "IMG_0004.json": {"n": 7},
        "weird.json": {"n": 8, "title": "Holiday photo.jpg"},
    }
    m = lambda name: (takeout.match_sidecar(name, cars) or {}).get("n")
    assert m("IMG_0001.jpg") == 1 and m("IMG_0001-edited.jpg") == 1
    assert m("IMG_0002.jpg") == 2
    assert m("IMG_0003(1).jpg") == 3 and m("IMG_0003.jpg") == 4
    assert m("Screenshot_20210101-101010_Some Long App Name.jpg") == 5
    assert m("PXL_20230101_120000000.MP.jpg") == 6
    assert m("IMG_0004.jpg") == 7
    assert m("Holiday photo.jpg") == 8
    assert m("IMG_00.jpg") is None and m("IMG_0001.png") is None


def _sidecar(title, when, **extra):
    import calendar
    from datetime import datetime
    ts = calendar.timegm(datetime.strptime(when, "%Y-%m-%d %H:%M:%S").timetuple())
    return json.dumps({"title": title, "photoTakenTime": {"timestamp": str(ts)}, "url": "https://photos.google.com/x",
                       "geoData": {"latitude": 0.0, "longitude": 0.0}, **extra})


def test_import_from_google_takeout(tmp_path, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    time.tzset()
    root = library(tmp_path)
    beach = (root / "2023" / "beach.jpg").read_bytes()
    dog = (root / "2024" / "dog.jpg").read_bytes()
    nodate = io.BytesIO()
    Image.new("RGB", (200, 100), (5, 5, 250)).save(nodate, "JPEG")
    nodate = nodate.getvalue()
    export = tmp_path / "Downloads"
    export.mkdir()
    # one zip, plus a folder unpacked from a second zip
    with zipfile.ZipFile(export / "takeout-001.zip", "w") as zf:
        zf.writestr("Takeout/Google Photos/Photos from 2023/beach.jpg", beach)
        zf.writestr("Takeout/Google Photos/Photos from 2023/beach.jpg.supplemental-metadata.json",
                    _sidecar("beach.jpg", "2023-07-04 12:00:00", favorited=True, description="Sandy",
                             people=[{"name": "Sam"}]))
        zf.writestr("Takeout/Google Photos/Rome trip/metadata.json", json.dumps({"title": "Rome trip"}))
        zf.writestr("Takeout/Google Photos/Rome trip/beach.jpg", beach)
        zf.writestr("Takeout/Google Photos/Rome trip/beach.jpg.supplemental-metadata.json",
                    _sidecar("beach.jpg", "2023-07-04 12:00:00", favorited=True))
    unpacked = export / "takeout-002" / "Takeout" / "Google Photos" / "Photos from 2019"
    unpacked.mkdir(parents=True)
    (unpacked / "scan.jpg").write_bytes(nodate)
    (unpacked / "scan.jpg.json").write_text(_sidecar("scan.jpg", "2019-02-03 04:05:06", archived=True,
                                                     geoData={"latitude": 41.9, "longitude": 12.5}))
    (unpacked / "dog.jpg").write_bytes(dog)
    (unpacked / "dog.jpg.supplemental-metadata.json").write_text(
        _sidecar("dog.jpg", "2024-06-15 18:00:00", geoData={"latitude": 1.0, "longitude": 2.0}))
    (unpacked / "lonely.jpg").write_bytes(jpeg_bytes((77, 1, 1)))
    rome = export / "takeout-002" / "Takeout" / "Google Photos" / "Rome trip"
    rome.mkdir(parents=True)
    (rome / "scan.jpg").write_bytes(nodate)
    (rome / "scan.jpg.json").write_text(_sidecar("scan.jpg", "2019-02-03 04:05:06"))

    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        add_face(cfg, ids["beach.jpg"], (0.2, 0.2, 0.2, 0.2))

        assert c.post("/api/imports/takeout", json={"path": "relative"}).status_code == 202
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert "whole path" in state["error"]

        c.post("/api/imports/takeout", json={"path": str(export), "dry_run": True})
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert state["error"] is None, state
        s = state["summary"]
        assert (s["files"], s["matched"], s["missing"], s["without_details"], s["albums_found"]) == (6, 2, 2, 1, 1)

        c.post("/api/imports/takeout", json={"path": str(export)})
        state = wait_for(lambda: (s := c.get("/api/imports").json()["current"]) and not s["running"] and s)
        assert state["error"] is None, state
        s = state["summary"]
        assert s["copied"] == 2 and s["favourites"] == 1 and s["archived"] == 1 and s["names"] == 1
        copied = root / "From Google Photos" / "Photos from 2019" / "scan.jpg"
        assert copied.read_bytes() == nodate
        ids = ids_by_name(c)
        scan = c.get(f"/api/assets/{ids['scan.jpg']}").json()
        assert scan["taken_at"] == "2019-02-03T04:05:06" and scan["archived"] and scan["position"] == [41.9, 12.5]
        assert [a["name"] for a in scan["albums"]] == ["Rome trip"]
        beach_item = c.get(f"/api/assets/{ids['beach.jpg']}").json()
        assert beach_item["favorite"] and beach_item["description"] == "Sandy"
        assert [a["name"] for a in beach_item["albums"]] == ["Rome trip"]
        assert [p["name"] for p in beach_item["people"]] == ["Sam"]
        dog_item = c.get(f"/api/assets/{ids['dog.jpg']}").json()
        assert dog_item["taken_at"] == "2024-06-15T16:00:00"      # two hours off: the time zone, not a change
        assert dog_item["position"] == [1.0, 2.0]                 # the file had none


def test_upgrade_from_06_database(tmp_path):
    path = tmp_path / "old.db"
    conn = sqlite3.connect(path)
    for n, step in enumerate(db.MIGRATIONS[:5], start=1):
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 5")
    conn.execute("INSERT INTO albums (name, created_at, updated_at) VALUES ('Old', '2024', '2024')")
    conn.commit()
    conn.close()
    db.init(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 6
    assert conn.execute("SELECT name, rule FROM albums").fetchall() == [("Old", None)]
    for table in ("face_hints", "imports", "webhooks", "webhook_deliveries"):
        conn.execute(f"SELECT * FROM {table}")
