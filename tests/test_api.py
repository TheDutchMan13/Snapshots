import io
import sqlite3

import pytest
from PIL import Image

from snapshots import db, media
from snapshots.app import safe_filename
from helpers import (jpeg_bytes, make_jpeg, new_client, rescan, setup_admin, wait_scan)


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "2024" / "beach.jpg", "2024:07:04 10:30:00", camera=("Google", "Pixel 8"))
    make_jpeg(root / "2024" / "dinner.jpg", "2024:07:04 19:00:00")
    make_jpeg(root / "2023" / "tree.jpg", "2023:12:25 08:00:00", size=(300, 900))
    make_jpeg(root / "IMG_20220102_030405.jpg")  # no EXIF: date comes from the name
    Image.new("RGBA", (100, 100), (0, 255, 0, 128)).save(root / "logo.png")
    (root / "clip.mp4").write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"0" * 5000)
    (root / "notes.txt").write_text("not media")
    (root / ".hidden").mkdir()
    make_jpeg(root / ".hidden" / "skip.jpg", "2020:01:01 00:00:00")
    return root


@pytest.fixture
def client(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        setup_admin(c)
        wait_scan(c)
        yield c, cfg, lib


# ------------------------------------------------------------------ scanning

def test_scan_indexes_media_only(client):
    c, cfg, lib = client
    items = c.get("/api/assets").json()["items"]
    names = {i["filename"] for i in items}
    assert names == {"beach.jpg", "dinner.jpg", "tree.jpg", "IMG_20220102_030405.jpg", "logo.png", "clip.mp4"}
    st = c.get("/api/status").json()
    assert st["photos"] == 5 and st["videos"] == 1


def test_dates_exif_filename_and_order(client):
    c, *_ = client
    items = c.get("/api/assets").json()["items"]
    by_name = {i["filename"]: i for i in items}
    assert by_name["beach.jpg"]["taken_at"] == "2024-07-04T10:30:00"
    assert by_name["IMG_20220102_030405.jpg"]["taken_at"] == "2022-01-02T03:04:05"
    assert by_name["beach.jpg"]["camera"] == "Google Pixel 8"
    taken = [i["taken_at"] for i in items]
    assert taken == sorted(taken, reverse=True)


def test_thumbnails_and_dimensions(client):
    c, cfg, _ = client
    items = {i["filename"]: i for i in c.get("/api/assets").json()["items"]}
    tree = items["tree.jpg"]
    assert (tree["width"], tree["height"]) == (300, 900)
    r = c.get(f"/api/assets/{tree['id']}/thumb")
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"
    with Image.open(io.BytesIO(r.content)) as t:
        assert max(t.size) <= media.THUMB_SIZE
    assert items["logo.png"]["has_thumb"] is True  # alpha PNG converted cleanly


def test_originals_untouched(client):
    c, cfg, lib = client
    before = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in lib.rglob("*") if p.is_file()}
    rescan(c)
    after = {p: (p.stat().st_size, p.stat().st_mtime_ns) for p in lib.rglob("*") if p.is_file()}
    assert before == after


def test_rescan_picks_up_new_and_removed(client):
    c, cfg, lib = client
    make_jpeg(lib / "new.jpg", "2025:01:01 12:00:00")
    (lib / "2023" / "tree.jpg").unlink()
    s = rescan(c)
    assert s["added"] == 1 and s["removed"] == 1
    names = {i["filename"] for i in c.get("/api/assets").json()["items"]}
    assert "new.jpg" in names and "tree.jpg" not in names


def test_changed_content_is_reread(client):
    c, cfg, lib = client
    beach = next(i for i in c.get("/api/assets").json()["items"] if i["filename"] == "beach.jpg")
    make_jpeg(lib / "2024" / "beach.jpg", "2019:05:05 05:05:05", color=(1, 2, 3))
    s = rescan(c)
    assert s["updated"] == 1
    again = c.get(f"/api/assets/{beach['id']}").json()
    assert again["taken_at"] == "2019-05-05T05:05:05" and again["v"] != beach["v"]


def test_missing_library_removes_nothing(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        setup_admin(c)
        wait_scan(c)
        total = c.get("/api/status").json()["photos"]
        cfg.library_path = lib.with_name("unplugged")  # drive disappears
        assert c.get("/api/status").json()["library_ok"] is False
        assert c.get("/api/status").json()["photos"] == total
        assert c.post("/api/scan").status_code == 409


def test_empty_walk_does_not_wipe_database(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        setup_admin(c)
        wait_scan(c)
        total = c.get("/api/status").json()["photos"]
        empty = tmp_path / "empty"
        empty.mkdir()
        cfg.library_path = empty  # e.g. an empty mount point
        s = rescan(c)
        assert s["removed"] == 0 and "Skipped removing" in (s["message"] or "")
        assert c.get("/api/status").json()["photos"] == total


# ------------------------------------------------------------------ browsing

def test_pagination_covers_everything_once(client):
    c, *_ = client
    seen, cursor = [], ""
    while True:
        page = c.get("/api/assets", params={"limit": 2, "cursor": cursor}).json()
        seen += [i["id"] for i in page["items"]]
        cursor = page["next"]
        if not cursor:
            break
    assert len(seen) == len(set(seen)) == 6


@pytest.mark.parametrize("q,expected", [
    ("beach", {"beach.jpg"}),
    ("pixel", {"beach.jpg"}),
    ("2024", {"beach.jpg", "dinner.jpg"}),
    ("2024-07", {"beach.jpg", "dinner.jpg"}),
    ("2023-12-25", {"tree.jpg"}),
    ("december", {"tree.jpg"}),
    ("july 2024", {"beach.jpg", "dinner.jpg"}),
    ("beach dinner", set()),
    ("100%", set()),  # LIKE wildcards are escaped
    ("_", {"IMG_20220102_030405.jpg"}),  # a literal underscore, not "any character"
])
def test_search(client, q, expected):
    c, *_ = client
    names = {i["filename"] for i in c.get("/api/assets", params={"q": q}).json()["items"]}
    assert names == expected


def test_kind_filter_and_timeline(client):
    c, *_ = client
    videos = c.get("/api/assets", params={"kind": "video"}).json()["items"]
    assert [v["filename"] for v in videos] == ["clip.mp4"]
    months = c.get("/api/timeline").json()["months"]
    assert {"month": "2024-07", "count": 2} in months
    assert c.get("/api/timeline", params={"q": "2023"}).json()["months"] == [{"month": "2023-12", "count": 1}]


def test_bad_input_rejected(client):
    c, *_ = client
    assert c.get("/api/assets", params={"cursor": "garbage"}).status_code == 400
    assert c.get("/api/assets", params={"kind": "audio"}).status_code == 422
    assert c.get("/api/assets/99999").status_code == 404
    assert c.get("/api/assets/99999/original").status_code == 404


# ------------------------------------------------------------------ serving files

def test_original_download_and_range(client):
    c, cfg, lib = client
    video = next(i for i in c.get("/api/assets", params={"kind": "video"}).json()["items"])
    full = (lib / "clip.mp4").read_bytes()
    r = c.get(f"/api/assets/{video['id']}/original")
    assert r.status_code == 200 and r.content == full
    r = c.get(f"/api/assets/{video['id']}/original", headers={"Range": "bytes=0-9"})
    assert r.status_code == 206 and r.content == full[:10]
    r = c.get(f"/api/assets/{video['id']}/original", params={"download": "true"})
    assert "attachment" in r.headers["content-disposition"]


def test_heic_gets_jpeg_view(tmp_path):
    pytest.importorskip("pillow_heif")
    root = tmp_path / "lib"
    root.mkdir()
    Image.new("RGB", (400, 300), (10, 120, 200)).save(root / "iphone.heic", format="HEIF")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        item = c.get("/api/assets").json()["items"][0]
        assert item["has_thumb"] is True
        r = c.get(f"/api/assets/{item['id']}/view")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg"


def test_security_headers(client):
    c, *_ = client
    r = c.get("/")
    assert r.headers["x-content-type-options"] == "nosniff"
    assert "default-src 'self'" in r.headers["content-security-policy"]
    assert c.get("/api/status").headers["cache-control"] == "no-store"


# ------------------------------------------------------------------ upload

def test_upload_dedupe_and_rejection(client):
    c, cfg, lib = client
    data = jpeg_bytes((9, 9, 9))
    r = c.post("/api/upload", files=[("files", ("new photo.jpg", data, "image/jpeg"))]).json()["results"][0]
    assert r["status"] == "added"
    stored = list((lib / "Uploads").rglob("new photo.jpg"))
    assert len(stored) == 1 and stored[0].read_bytes() == data

    again = c.post("/api/upload", files=[("files", ("copy.jpg", data, "image/jpeg"))]).json()["results"][0]
    assert again["status"] == "duplicate" and again["id"] == r["id"]

    bad = c.post("/api/upload", files=[("files", ("evil.html", b"<script>", "text/html"))]).json()["results"][0]
    assert bad["status"] == "rejected"

    # same name, different content: keeps both
    r2 = c.post("/api/upload", files=[("files", ("new photo.jpg", jpeg_bytes((50, 50, 50)), "image/jpeg"))]).json()["results"][0]
    assert r2["status"] == "added"
    assert len(list((lib / "Uploads").rglob("new photo*.jpg"))) == 2
    assert not list((lib / "Uploads" / ".incoming").glob("*"))  # no temp files left behind


def test_upload_cannot_escape_library(client):
    c, cfg, lib = client
    r = c.post("/api/upload", files=[("files", ("../../evil.jpg", jpeg_bytes((7, 7, 7)), "image/jpeg"))])
    assert r.json()["results"][0]["status"] == "added"
    assert not (lib.parent / "evil.jpg").exists() and not (lib.parent.parent / "evil.jpg").exists()
    assert list((lib / "Uploads").rglob("evil.jpg"))


@pytest.mark.parametrize("raw,clean", [
    ("../../x.JPG", "x.jpg"),
    ("C:\\Users\\me\\a:b.png", "a_b.png"),
    ("CON.jpg", "_CON.jpg"),
    ("bad<>name?.jpg", "bad__name_.jpg"),
    (None, "upload"),
])
def test_safe_filename(raw, clean):
    assert safe_filename(raw) == clean


# ------------------------------------------------------------------ upgrading an old database

def test_version_01_database_is_upgraded(tmp_path, lib):
    data = tmp_path / "data"
    data.mkdir()
    old = sqlite3.connect(data / "snapshots.db")
    old.executescript(db.MIGRATIONS[0])  # exactly what 0.1 created (user_version stays 0)
    old.execute(
        "INSERT INTO assets (path, filename, kind, size, mtime_ns, sha1, taken_at, has_thumb, added_at)"
        " VALUES ('old.jpg', 'old.jpg', 'photo', 1, 1, 'abc', '2020-01-01T00:00:00', 0, '2020-01-01T00:00:00')")
    old.commit()
    old.close()

    db.init(data / "snapshots.db")
    conn = db.connect(data / "snapshots.db")
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT path, trashed_at, geo_done, faces_done FROM assets").fetchone()
    assert tuple(row) == ("old.jpg", None, 0, 0)
    db.init(data / "snapshots.db")  # running again changes nothing
    conn.close()


def test_newer_database_is_refused(tmp_path):
    path = tmp_path / "x.db"
    conn = sqlite3.connect(path)
    conn.execute(f"PRAGMA user_version = {db.SCHEMA_VERSION + 1}")
    conn.close()
    with pytest.raises(RuntimeError, match="newer version"):
        db.init(path)
