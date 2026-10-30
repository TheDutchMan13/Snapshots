"""0.9: stars, labels and flags; the library health check; the storage report; the year in review."""

import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from snapshots import db, health, review, xmp
from helpers import ADMIN, add_member, ids_by_name, login, make_jpeg, new_client, rescan, setup_admin, wait_for, wait_scan

HAS_FFMPEG = bool(shutil.which("ffmpeg") and shutil.which("ffprobe"))
_open = []


@pytest.fixture(autouse=True)
def _close_clients(monkeypatch):
    monkeypatch.setattr(xmp, "CHECK_SECONDS", 0.2)
    yield
    while _open:
        _open.pop().__exit__(None, None, None)


def pattern(seed: int, size=(640, 480)) -> Image.Image:
    """A picture that looks different from every other seed (so photos aren't taken for copies)."""
    img = Image.new("RGB", size)
    px = img.load()
    for x in range(size[0]):
        for y in range(size[1]):
            px[x, y] = ((x * (seed + 3) + y * seed) % 256, (y * (seed + 5) ^ x) % 256, (x * y * (seed + 1)) % 256)
    return img


def library(tmp_path) -> Path:
    root = tmp_path / "Photos"
    make_jpeg(root / "2023" / "beach.jpg", "2023:07:04 12:00:00", image=pattern(1))
    make_jpeg(root / "2024" / "hike.jpg", "2024:05:01 09:30:00", image=pattern(2))
    make_jpeg(root / "2024" / "dog.jpg", "2024:06:15 16:00:00", image=pattern(3))
    return root


def started(tmp_path, root, **kw):
    c, cfg = new_client(tmp_path, root, **kw)
    c.__enter__()
    _open.append(c)
    return c, cfg


def ready(tmp_path, root=None, **kw):
    root = root or library(tmp_path)
    c, cfg = started(tmp_path, root, **kw)
    setup_admin(c)
    wait_scan(c)
    return c, cfg, root


def member_client(c):
    add_member(c)
    m = TestClient(c.app)
    m.headers["X-Snapshots-Request"] = "1"
    login(m, "member", "member password 1")
    return m


# ------------------------------------------------------------------ stars, labels, flags

def test_marks_and_filters(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    assert c.post("/api/marks", json={"ids": [ids["dog.jpg"], ids["hike.jpg"]], "rating": 4}).json()["updated"] == 2
    c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "rating": 5, "label": "red", "flag": 1})
    c.post("/api/marks", json={"ids": [ids["beach.jpg"]], "flag": -1})
    item = c.get(f"/api/assets/{ids['dog.jpg']}").json()
    assert (item["rating"], item["label"], item["flag"]) == (5, "red", 1)
    assert sorted(ids_by_name(c, rating=4)) == ["dog.jpg", "hike.jpg"]
    assert list(ids_by_name(c, rating=5)) == ["dog.jpg"]
    assert list(ids_by_name(c, label="red")) == ["dog.jpg"]
    assert list(ids_by_name(c, flag="pick")) == ["dog.jpg"]
    assert list(ids_by_name(c, flag="reject")) == ["beach.jpg"]
    assert list(ids_by_name(c, flag="unflagged")) == ["hike.jpg"]
    c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "label": ""})
    assert c.get(f"/api/assets/{ids['dog.jpg']}").json()["label"] is None
    assert c.post("/api/marks", json={"ids": [ids["dog.jpg"]]}).status_code == 400
    assert c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "rating": 6}).status_code == 422
    assert c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "label": "pink"}).status_code == 422
    assert c.get("/api/assets", params={"label": "pink"}).status_code == 422

    # A smart album of the best ones
    r = c.post("/api/albums", json={"name": "Best", "rule": {"rating": 4}})
    assert r.status_code == 201 and "4 stars or more" in r.json()["rule_text"]
    assert sorted(ids_by_name(c, album=r.json()["id"])) == ["dog.jpg", "hike.jpg"]


def test_marks_leave_locked_items_alone(tmp_path):
    c, cfg, _ = ready(tmp_path)
    ids = ids_by_name(c)
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE assets SET locked_by = 1 WHERE id = ?", (ids["dog.jpg"],))
    assert c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "rating": 3}).json()["updated"] == 0
    with db.session(cfg.db_path) as conn:
        assert conn.execute("SELECT rating FROM assets WHERE id = ?", (ids["dog.jpg"],)).fetchone()[0] == 0


def test_stars_and_labels_in_sidecars(tmp_path):
    c, cfg, root = ready(tmp_path)
    ids = ids_by_name(c)
    c.put("/api/settings/xmp", json={"enabled": True})
    c.post("/api/marks", json={"ids": [ids["dog.jpg"]], "rating": 3, "label": "green"})
    side = root / "2024" / "dog.jpg.xmp"
    got = wait_for(lambda: side.is_file() and (s := xmp.read(side))["rating"] == 3 and s, timeout=30)
    assert got["label"] == "green"
    assert "<xmp:Label>Green</xmp:Label>" in side.read_text()

    # A new photo with a Lightroom sidecar: its stars and label come in
    make_jpeg(root / "new" / "cat.jpg", "2020:01:01 10:00:00", image=pattern(9))
    (root / "new" / "cat.jpg.xmp").write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="4" xmp:Label="Blue"/>'
        '</rdf:RDF></x:xmpmeta>', encoding="utf-8")
    rescan(c)
    cat = c.get(f"/api/assets/{ids_by_name(c)['cat.jpg']}").json()
    assert (cat["rating"], cat["label"]) == (4, "blue")


# ------------------------------------------------------------------ library health

def run_check(c):
    assert c.post("/api/library-health/run").status_code == 200
    return wait_for(lambda: (s := c.get("/api/library-health").json())["last"] and s["last"]["finished_at"]
                    and not s["running"] and s, timeout=60)


def test_health_check_finds_problems(tmp_path):
    root = library(tmp_path)
    extra = tmp_path / "Drive E"
    make_jpeg(extra / "old.jpg", "2010:01:01 10:00:00", image=pattern(7))
    c, cfg, _ = ready(tmp_path, root, extra_libraries=[extra])
    s = run_check(c)
    assert s["last"]["problems"] == 0 and s["last"]["checked"] == 4 and s["last"]["bytes"] > 0

    # Damage: the bytes change, the size and date don't
    dog = root / "2024" / "dog.jpg"
    st = dog.stat()
    data = bytearray(dog.read_bytes())
    data[len(data) // 2] ^= 0xFF
    dog.write_bytes(bytes(data))
    os.utime(dog, ns=(st.st_atime_ns, st.st_mtime_ns))
    # Changed: saved again by another program
    hike = root / "2024" / "hike.jpg"
    make_jpeg(hike, "2024:05:01 09:30:00", image=pattern(4))
    os.utime(hike, (time.time() + 5, time.time() + 5))
    # Missing
    (root / "2023" / "beach.jpg").unlink()
    # Unavailable: the other drive is unplugged
    shutil.move(str(extra), str(tmp_path / "unplugged"))

    s = run_check(c)
    assert s["last"]["counts"] == {"damaged": 1, "unreadable": 0, "missing": 1, "changed": 1, "unavailable": 1}
    assert s["last"]["problems"] == 2
    issues = c.get("/api/library-health/issues").json()
    assert [(i["item"]["filename"], i["status"]) for i in issues["issues"]][:2] == [("dog.jpg", "damaged"),
                                                                                     ("beach.jpg", "missing")]
    assert all(not i["restorable"] for i in issues["issues"])
    only = c.get("/api/library-health/issues", params={"status": "changed"}).json()["issues"]
    assert [i["item"]["filename"] for i in only] == ["hike.jpg"]


def test_health_restore_from_offsite_copy(tmp_path):
    root = library(tmp_path)
    copy = tmp_path / "Backup drive" / "Photo copy"
    c, cfg, _ = ready(tmp_path, root, offsite_method="folder", offsite_target=str(copy))
    good = (root / "2024" / "dog.jpg").read_bytes()
    (copy / "Library" / "2024").mkdir(parents=True)
    (copy / "Library" / "2024" / "dog.jpg").write_bytes(good)
    dog = root / "2024" / "dog.jpg"
    st = dog.stat()
    bad = bytearray(good)
    bad[100] ^= 0x55
    dog.write_bytes(bytes(bad))
    os.utime(dog, ns=(st.st_atime_ns, st.st_mtime_ns))
    run_check(c)
    issue = c.get("/api/library-health/issues").json()["issues"][0]
    assert issue["status"] == "damaged" and issue["restorable"]
    r = c.post(f"/api/library-health/restore/{issue['item']['id']}")
    assert r.status_code == 200, r.text
    assert dog.read_bytes() == good
    assert (root / "2024" / "dog.jpg.damaged").read_bytes() == bytes(bad)
    assert c.get("/api/library-health/issues").json()["total"] == 0
    # Nothing to restore from for the others
    ids = ids_by_name(c)
    assert c.post(f"/api/library-health/restore/{ids['hike.jpg']}").status_code == 404


def test_health_is_for_administrators_and_hides_locked(tmp_path):
    c, cfg, root = ready(tmp_path)
    ids = ids_by_name(c)
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE assets SET locked_by = 1 WHERE id = ?", (ids["dog.jpg"],))
    (root / "2024" / "dog.jpg").unlink()
    s = run_check(c)
    assert s["last"]["problems"] == 0 and s["last"]["locked_problems"] == 1   # counted apart, never listed
    assert c.get("/api/library-health/issues").json()["issues"] == []
    assert c.post(f"/api/library-health/restore/{ids['dog.jpg']}").status_code == 404
    m = member_client(c)
    for call in (m.get("/api/library-health"), m.post("/api/library-health/run"),
                 m.get("/api/library-health/issues"), m.put("/api/library-health/settings", json={"days": 7})):
        assert call.status_code == 403
    assert c.put("/api/library-health/settings", json={"days": 30}).json()["days"] == 30


def test_health_check_on_a_timer(tmp_path):
    c, cfg, root = ready(tmp_path, health_check_days=7)
    wait_for(lambda: (s := c.get("/api/library-health").json())["last"] and s["last"]["finished_at"], timeout=60)
    with db.session(cfg.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM health_runs").fetchone()[0] == 1
        assert not health.HealthJob(cfg).due(conn, time.time())        # not again for a week


# ------------------------------------------------------------------ storage

def test_storage_report(tmp_path):
    root = library(tmp_path)
    (root / "Videos").mkdir()
    (root / "Videos" / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + bytes(500_000))
    c, cfg, _ = ready(tmp_path, root)
    ids = ids_by_name(c)
    c.post("/api/trash", json={"ids": [ids["beach.jpg"]]})
    s = c.get("/api/storage", params={"fresh": True}).json()
    assert s["kinds"]["videos"]["items"] == 1 and s["kinds"]["photos"]["items"] == 2
    assert s["trash"]["items"] == 1 and s["trash"]["bytes"] > 0
    assert {f["folder"] for f in s["folders"]} == {"2024", "Videos"}
    assert s["folders"][0]["folder"] == "Videos"                       # biggest first
    assert s["biggest"][0]["filename"] == "clip.mp4"
    assert [y["year"] for y in s["years"]][0] == "2024" or any(y["year"] == "2024" for y in s["years"])
    assert s["data"]["thumbnails"] > 0 and s["data"]["database"] > 0
    assert s["disks"] and s["disks"][0]["available"] and s["disks"][0]["free"] > 0
    # Locked items are not in anyone's report
    with db.session(cfg.db_path) as conn:
        conn.execute("UPDATE assets SET locked_by = 1 WHERE kind = 'video'")
    s = c.get("/api/storage", params={"fresh": True}).json()
    assert s["kinds"]["videos"]["items"] == 0 and all(b["filename"] != "clip.mp4" for b in s["biggest"])


# ------------------------------------------------------------------ year in review

def review_library(tmp_path) -> Path:
    root = tmp_path / "Photos"
    n = 0
    for month in range(1, 13):
        for day in (3, 17):
            n += 1
            make_jpeg(root / "2024" / f"{month:02d}-{day}.jpg", f"2024:{month:02d}:{day:02d} 12:00:00", image=pattern(n))
    # a burst: three shots seconds apart, the same picture
    for k in range(3):
        make_jpeg(root / "2024" / f"burst{k}.jpg", f"2024:08:10 10:00:0{k}", image=pattern(100))
    make_jpeg(root / "2023" / "old.jpg", "2023:05:05 10:00:00", image=pattern(200))
    return root


def test_review_picks_spread_and_skips_copies(tmp_path):
    c, cfg, root = ready(tmp_path, review_library(tmp_path))
    wait_for(lambda: c.get("/api/cleanup").json()["status"]["pending"] == 0, timeout=90)
    ids = ids_by_name(c)
    c.post("/api/favorites", json={"ids": [ids["burst1.jpg"]], "value": True})
    c.post("/api/marks", json={"ids": [ids["06-3.jpg"]], "flag": -1})       # a reject never makes it
    c.post("/api/archive", json={"ids": [ids["07-3.jpg"]], "value": True})  # nor does an archived one
    years = c.get("/api/review").json()["years"]
    assert [y["year"] for y in years] == [2024, 2023]
    r = c.post("/api/review/2024", json={"count": 12}).json()
    names = [i["filename"] for i in r["items"]]
    assert 10 <= len(names) <= 12
    assert "burst1.jpg" in names and "burst0.jpg" not in names and "burst2.jpg" not in names
    assert "06-3.jpg" not in names and "07-3.jpg" not in names and "old.jpg" not in names
    months = {n[:2] for n in names if n[0].isdigit()}
    assert len(months) >= 10                                                 # spread over the year
    assert [i["taken_at"] for i in r["items"]] == sorted(i["taken_at"] for i in r["items"])
    big = c.post("/api/review/2024", json={"count": 200}).json()
    assert len(big["items"]) == 24 + 1 - 2                                   # all, less a reject, an archived, a burst


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_review_video(tmp_path):
    c, cfg, root = ready(tmp_path, review_library(tmp_path))
    picked = c.post("/api/review/2024", json={"count": 10}).json()["ids"][:3]
    r = c.post("/api/review/2024/video", json={"ids": picked, "seconds": 2, "size": "720p", "save": True})
    assert r.status_code == 202, r.text
    task = r.json()
    job = wait_for(lambda: (j := c.get(f"/api/tasks/{task['id']}").json())["done"] and j, timeout=240)
    assert job["error"] is None, job["error"]
    assert job["result"]["photos"] == 3
    f = c.get(f"/api/tasks/{task['id']}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "video/mp4"
    out = tmp_path / "review.mp4"
    out.write_bytes(f.content)
    probe = json.loads(subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_streams",
                                       "-show_format", str(out)], capture_output=True, check=True).stdout)
    v = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (v["width"], v["height"]) == (1280, 720)
    assert 10 <= float(probe["format"]["duration"]) <= 14                    # 3 + 3 x 2 + 3 seconds
    saved = root / "Year in review" / "2024 in review.mp4"
    assert saved.is_file()
    item = c.get(f"/api/assets/{job['result']['id']}").json()
    assert item["taken_at"] == "2024-12-31T23:59:00" and item["kind"] == "video"


def test_title_card_draws_text():
    img = review.title_card((640, 360), "2024", ["12 moments", "Phoenix · Rome"])
    assert img.size == (640, 360)
    assert len(img.getcolors(1 << 16) or []) > 2   # there is text on it


# ------------------------------------------------------------------ found in review

def _damage(path: Path):
    st = path.stat()
    data = bytearray(path.read_bytes())
    data[len(data) // 2] ^= 0xFF
    path.write_bytes(bytes(data))
    os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))


def test_restore_only_what_is_still_broken(tmp_path):
    root = library(tmp_path)
    copy = tmp_path / "copy"
    c, cfg, _ = ready(tmp_path, root, offsite_method="folder", offsite_target=str(copy))
    for name in ("dog.jpg", "hike.jpg"):
        (copy / "Library" / "2024").mkdir(parents=True, exist_ok=True)
        shutil.copy(root / "2024" / name, copy / "Library" / "2024" / name)
        _damage(root / "2024" / name)
    run_check(c)
    ids = ids_by_name(c)
    # Saved again since the check (by an editor, say): left alone
    hike = root / "2024" / "hike.jpg"
    hike.write_bytes(hike.read_bytes() + b"edited")
    r = c.post(f"/api/library-health/restore/{ids['hike.jpg']}")
    assert r.status_code == 409 and "saved again" in r.json()["detail"]
    assert hike.read_bytes().endswith(b"edited")
    # A second restore keeps the first .damaged file
    dog = root / "2024" / "dog.jpg"
    (root / "2024" / "dog.jpg.damaged").write_bytes(b"older")
    assert c.post(f"/api/library-health/restore/{ids['dog.jpg']}").status_code == 200
    assert (root / "2024" / "dog.jpg.damaged").read_bytes() == b"older"
    assert (root / "2024" / "dog.jpg.damaged-2").is_file()
    assert not list((root / "2024").glob(".*restore*"))


def test_missing_items_wait_for_restore(tmp_path):
    root = library(tmp_path)
    copy = tmp_path / "copy"
    c, cfg, _ = ready(tmp_path, root, offsite_method="folder", offsite_target=str(copy))
    (copy / "Library" / "2024").mkdir(parents=True, exist_ok=True)
    shutil.copy(root / "2024" / "dog.jpg", copy / "Library" / "2024" / "dog.jpg")
    (copy / "Library" / "2024" / "hike.jpg").unlink(missing_ok=True)   # the automatic copy may have made one
    ids = ids_by_name(c)
    c.post("/api/favorites", json={"ids": [ids["dog.jpg"]], "value": True})
    (root / "2024" / "dog.jpg").unlink()
    (root / "2024" / "hike.jpg").unlink()           # no copy of this one: forgotten as usual
    run_check(c)
    rescan(c)
    names = ids_by_name(c)
    assert "dog.jpg" in names and "hike.jpg" not in names
    assert c.post(f"/api/library-health/restore/{ids['dog.jpg']}").status_code == 200
    assert (root / "2024" / "dog.jpg").is_file()
    assert c.get(f"/api/assets/{ids['dog.jpg']}").json()["favorite"]


def test_health_lists_live_photo_clips(tmp_path):
    root = library(tmp_path)
    c, cfg, _ = ready(tmp_path, root)
    ids = ids_by_name(c)
    with db.session(cfg.db_path) as conn:   # make hike.jpg the hidden clip of dog.jpg
        conn.execute("UPDATE assets SET live_video = ? WHERE id = ?", (ids["hike.jpg"], ids["dog.jpg"]))
    (root / "2024" / "hike.jpg").unlink()
    run_check(c)
    issues = c.get("/api/library-health/issues").json()
    assert issues["total"] == 1 and [i["item"]["filename"] for i in issues["issues"]] == ["hike.jpg"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="needs ffmpeg")
def test_review_video_title_is_a_safe_file_name(tmp_path):
    c, cfg, root = ready(tmp_path, review_library(tmp_path))
    picked = c.post("/api/review/2024", json={"count": 10}).json()["ids"][:2]
    r = c.post("/api/review/2024/video", json={"ids": picked, "seconds": 2, "title": "../../Italy: 2023/24", "save": True})
    job = wait_for(lambda: (j := c.get(f"/api/tasks/{r.json()['id']}").json())["done"] and j, timeout=240)
    assert job["error"] is None, job["error"]
    made = list((root / "Year in review").iterdir())
    assert len(made) == 1 and made[0].suffix == ".mp4" and ":" not in made[0].name and "/" not in made[0].name
    assert not list(tmp_path.glob("*.mp4")) and not list(root.glob("*.mp4"))


def test_storage_for_members_hides_paths(tmp_path):
    c, cfg, root = ready(tmp_path)
    m = member_client(c)
    s = m.get("/api/storage", params={"fresh": True}).json()
    assert all(str(tmp_path) not in d["label"] for d in s["disks"])
    assert any(str(tmp_path) in d["label"] for d in c.get("/api/storage", params={"fresh": True}).json()["disks"])


def test_xmp_reject_both_ways(tmp_path):
    c, cfg, root = ready(tmp_path)
    c.put("/api/settings/xmp", json={"enabled": True})
    make_jpeg(root / "new" / "bad.jpg", "2020:01:01 10:00:00", image=pattern(11))
    (root / "new" / "bad.jpg.xmp").write_text(
        '<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
        '<rdf:Description rdf:about="" xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:Rating="-1"/>'
        '</rdf:RDF></x:xmpmeta>', encoding="utf-8")
    rescan(c)
    bad = ids_by_name(c)["bad.jpg"]
    assert c.get(f"/api/assets/{bad}").json()["flag"] == -1
    side = root / "new" / "bad.jpg.xmp"
    wait_for(lambda: xmp.read(side)["ours"], timeout=30)
    assert xmp.read(side)["rating"] == -1
