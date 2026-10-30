"""Copies, bursts, blurry and dark photos, events, and the Clean up page."""

import io
import random
from datetime import datetime, timedelta

import pytest
from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

from snapshots import analysis, db


def wait_measured(cfg, column="quality_done"):
    """Until the background job has measured every photo."""
    def done():
        conn = db.connect(cfg.db_path)
        try:
            return conn.execute(f"SELECT COUNT(*) FROM assets WHERE {column} = 0 AND trashed_at IS NULL").fetchone()[0] == 0
        finally:
            conn.close()
    wait_for(done)
from snapshots.analysis import find_events, hamming, measure, similar_pairs
from helpers import ids_by_name, make_jpeg, new_client, rescan, setup_admin, wait_for, wait_scan


def scene(seed: int, size=(1200, 900)) -> Image.Image:
    """A detailed made-up picture: shapes and lines, different for every seed."""
    rng = random.Random(seed)
    img = Image.new("RGB", size, tuple(rng.randrange(256) for _ in range(3)))
    d = ImageDraw.Draw(img)
    for _ in range(60):
        x, y = rng.randrange(size[0]), rng.randrange(size[1])
        w, h = rng.randrange(20, 300), rng.randrange(20, 300)
        colour = tuple(rng.randrange(256) for _ in range(3))
        if rng.random() < 0.5:
            d.rectangle((x, y, x + w, y + h), fill=colour)
        else:
            d.ellipse((x, y, x + w, y + h), fill=colour)
    for _ in range(80):
        d.line([(rng.randrange(size[0]), rng.randrange(size[1])) for _ in range(2)],
               fill=tuple(rng.randrange(256) for _ in range(3)), width=rng.randrange(1, 4))
    return img


def thumb(img: Image.Image) -> Image.Image:
    t = img.copy()
    t.thumbnail((480, 480))
    buf = io.BytesIO()
    t.save(buf, "JPEG", quality=85)
    return Image.open(io.BytesIO(buf.getvalue()))


# ------------------------------------------------------------------ measuring

def test_copies_have_nearly_the_same_fingerprint_and_others_do_not():
    a = scene(1)
    smaller = a.resize((600, 450))
    buf = io.BytesIO()
    smaller.save(buf, "JPEG", quality=50)
    copy = Image.open(io.BytesIO(buf.getvalue()))
    ha, hc = measure(thumb(a))["dhash"], measure(thumb(copy))["dhash"]
    assert hamming(ha, hc) <= 8
    others = [measure(thumb(scene(s)))["dhash"] for s in range(2, 30)]
    assert min(hamming(ha, h) for h in others) > 8


def test_flat_pictures_get_no_fingerprint():
    assert measure(Image.new("RGB", (400, 300), (0, 0, 0)))["dhash"] is None
    assert measure(Image.new("RGB", (400, 300), (250, 250, 250)))["dhash"] is None


def test_sharpness_blur_and_camera_shake():
    sharp = measure(thumb(scene(3)))["sharpness"]
    blurred = measure(thumb(scene(3).filter(ImageFilter.GaussianBlur(8))))["sharpness"]
    assert sharp > 200 > 50 > blurred
    # camera shake: smeared sideways only
    sideways = ImageFilter.Kernel((5, 5), [0] * 10 + [1] * 5 + [0] * 10, 5)
    shaken = scene(3)
    for _ in range(9):
        shaken = shaken.filter(sideways)
    assert measure(thumb(shaken))["sharpness"] < sharp / 5


def test_small_photos_are_not_called_blurry_for_being_small():
    small = scene(4).resize((240, 180))
    assert measure(small)["sharpness"] > 50


def test_dark_needs_dim_highlights_too():
    cfg = type("C", (), {"dark_threshold": 30, "blur_threshold": 50})()
    under = ImageEnhance.Brightness(scene(5)).enhance(0.12)
    m = measure(thumb(under))
    assert analysis.quality_label(cfg, m["sharpness"], m["brightness"], m["highlights"]) == "dark"
    night = Image.new("RGB", (800, 600), (8, 8, 12))
    d = ImageDraw.Draw(night)
    for x in range(60, 760, 100):  # a night street: lit windows, a few percent of the picture
        d.rectangle((x, 250, x + 30, 300), fill=(255, 220, 150))
    m = measure(night)
    assert m["brightness"] < 30 and analysis.quality_label(cfg, 999, m["brightness"], m["highlights"]) is None


def test_similar_pairs_finds_every_pair_within_the_distance():
    rng = random.Random(7)
    base = [rng.getrandbits(64) for _ in range(300)]
    items = [(i, analysis._signed(h)) for i, h in enumerate(base)]
    # near copies: flip up to 6 bits
    for j, h in enumerate(base[:40]):
        flipped = h
        for bit in rng.sample(range(64), rng.randrange(0, 7)):
            flipped ^= 1 << bit
        items.append((1000 + j, analysis._signed(flipped)))
    found = similar_pairs(items, 8)
    brute = {(a, b) for x, (a, ha) in enumerate(items) for (b, hb) in items[x + 1:] if hamming(ha, hb) <= 8}
    brute = {(min(a, b), max(a, b)) for a, b in brute}
    assert found == brute and len(found) >= 40


# ------------------------------------------------------------------ events

def row(i, when, place=None, city=None, state="Arizona", country="United States"):
    lat, lon = place if place else (None, None)
    return {"id": i, "taken_at": when.isoformat(timespec="seconds"), "lat": lat, "lon": lon,
            "city": city, "state": state if city else None, "country": country if city else None, "sharpness": 100.0}


PHX, FLAG, SEDONA, PARIS = (33.45, -112.07), (35.20, -111.65), (34.87, -111.76), (48.86, 2.35)


def test_days_at_home_and_a_trip_away():
    rows, i = [], 0
    for day in range(5):  # five days at home, ten photos each
        for k in range(10):
            rows.append(row(i, datetime(2025, 3, 1 + day * 3, 10) + timedelta(minutes=10 * k), PHX, "Phoenix")); i += 1
    t = datetime(2025, 6, 1, 9)
    for d, (pos, city) in enumerate([(FLAG, "Flagstaff"), (SEDONA, "Sedona")]):
        for k in range(6):
            rows.append(row(i, t + timedelta(days=d, minutes=30 * k), pos, city)); i += 1
    events = find_events(rows, min_items=8)
    assert [e["kind"] for e in events] == ["trip"] + ["day"] * 5
    trip = events[0]
    assert trip["place"] == "Flagstaff and Sedona" and trip["days"] == 2 and trip["count"] == 12
    assert {e["place"] for e in events[1:]} == {"Phoenix"}


def test_a_long_pause_or_a_long_drive_starts_a_new_event():
    rows = [row(k, datetime(2025, 1, 5, 9) + timedelta(minutes=5 * k), PHX, "Phoenix") for k in range(10)]
    rows += [row(10 + k, datetime(2025, 1, 5, 19) + timedelta(minutes=5 * k), PHX, "Phoenix") for k in range(10)]
    # same day at home: one event
    assert len(find_events(rows, min_items=8)) == 1
    # photos far away on the same day are their own (trip) event
    rows += [row(20 + k, datetime(2025, 1, 5, 21) + timedelta(minutes=5 * k), PARIS, "Paris", "Île-de-France", "France")
             for k in range(10)]
    kinds = [e["kind"] for e in find_events(rows, min_items=8)]
    assert kinds == ["trip", "day"]


def test_small_events_are_left_out_and_no_places_is_fine():
    rows = [row(k, datetime(2025, 2, 1, 12) + timedelta(minutes=k)) for k in range(5)]
    assert find_events(rows, min_items=8) == []
    rows = [row(k, datetime(2025, 2, 1, 12) + timedelta(minutes=k)) for k in range(9)]
    (e,) = find_events(rows, min_items=8)
    assert e["kind"] == "day" and e["place"] is None and e["count"] == 9


# ------------------------------------------------------------------ through the app

@pytest.fixture
def library(tmp_path):
    root = tmp_path / "Photos"
    t = datetime(2025, 5, 1, 10)
    for k in range(10):  # an event: ten different photos one morning
        make_jpeg(root / "Day" / f"p{k}.jpg", (t + timedelta(minutes=k)).strftime("%Y:%m:%d %H:%M:%S"), image=scene(100 + k))
    original = scene(200)
    make_jpeg(root / "orig.jpg", "2025:01:01 12:00:00", image=original)
    make_jpeg(root / "Backup" / "orig_small.jpg", "2025:01:01 12:00:00", image=original.resize((600, 450)), quality=60)
    for k in range(3):  # a burst
        shifted = scene(300).crop((k * 8, 0, 1176 + k * 8, 900))
        make_jpeg(root / "burst" / f"b{k}.jpg", f"2025:02:02 15:00:0{k}", image=shifted)
    make_jpeg(root / "blurry.jpg", "2025:03:03 10:00:00", image=scene(400).filter(ImageFilter.GaussianBlur(10)))
    make_jpeg(root / "dark.jpg", "2025:03:04 22:00:00", image=ImageEnhance.Brightness(scene(401)).enhance(0.1))
    return root


@pytest.fixture
def client(tmp_path, library):
    c, cfg = new_client(tmp_path, library)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_measured(cfg)
        yield c, cfg


def test_cleanup_finds_copies_bursts_blurry_and_dark(client):
    c, cfg = client
    s = c.get("/api/cleanup").json()
    assert (s["copies"], s["bursts"], s["low_quality"]) == (1, 1, 2)
    groups = c.get("/api/cleanup/groups").json()["groups"]
    by_kind = {g["kind"]: g for g in groups}
    copies = by_kind["copies"]
    names = {i["id"]: i["filename"] for i in copies["items"]}
    assert sorted(names.values()) == ["orig.jpg", "orig_small.jpg"]
    assert names[copies["keep"]] == "orig.jpg"  # more pixels
    assert len(by_kind["burst"]["items"]) == 3
    low = [i["filename"] for i in c.get("/api/assets?quality=true").json()["items"]]
    assert sorted(low) == ["blurry.jpg", "dark.jpg"]
    detail = c.get(f"/api/assets/{ids_by_name(c)['blurry.jpg']}").json()
    assert detail["quality"] == "blurry"


def test_resolving_a_group_trashes_the_rest_and_not_duplicates_is_remembered(client):
    c, cfg = client
    groups = {g["kind"]: g for g in c.get("/api/cleanup/groups").json()["groups"]}
    burst = groups["burst"]
    keep = [burst["items"][0]["id"], burst["items"][1]["id"]]
    trash = [burst["items"][2]["id"]]
    assert c.post("/api/cleanup/resolve", json={"keep": keep, "trash": trash}).json() == {"trashed": 1}
    assert set(ids_by_name(c, trashed=True).values()) == set(trash)
    copies = groups["copies"]
    c.post("/api/cleanup/not-duplicates", json={"ids": [i["id"] for i in copies["items"]]})
    assert c.get("/api/cleanup").json()["groups"] == 0  # the 2 kept frames are no longer suggested either
    rescan(c)
    assert c.get("/api/cleanup").json()["groups"] == 0
    assert c.post("/api/cleanup/resolve", json={"keep": [1], "trash": [1]}).status_code == 400


def test_looks_fine_stops_suggesting_a_photo(client):
    c, cfg = client
    blurry = ids_by_name(c)["blurry.jpg"]
    c.post("/api/cleanup/fine", json={"ids": [blurry]})
    assert [i["filename"] for i in c.get("/api/assets?quality=true").json()["items"]] == ["dark.jpg"]
    assert c.get(f"/api/assets/{blurry}").json()["quality"] is None


def test_events_through_the_app(client):
    c, cfg = client
    events = c.get("/api/events").json()["events"]
    (e,) = [e for e in events if e["count"] == 10]
    assert e["start"] == "2025-05-01T10:00:00" and e["end"] == "2025-05-01T10:09:00"
    items = c.get(f"/api/assets?start={e['start']}&end={e['end']}&limit=50").json()["items"]
    assert len(items) == 10
    assert c.get("/api/assets?start=yesterday").status_code == 422


def test_changed_content_is_measured_again(client):
    c, cfg = client
    path = cfg.library_path / "blurry.jpg"
    make_jpeg(path, "2025:03:03 10:00:00", image=scene(500))  # replaced by a sharp photo
    rescan(c)
    wait_measured(cfg)
    assert [i["filename"] for i in c.get("/api/assets?quality=true").json()["items"]] == ["dark.jpg"]


def test_photo_checks_can_be_turned_off(tmp_path, library):
    c, cfg = new_client(tmp_path, library, photo_checks=False)
    with c:
        setup_admin(c)
        wait_scan(c)
        s = c.get("/api/cleanup").json()
        assert s["enabled"] is False and s["groups"] == 0


def test_upgrade_from_version_02_keeps_everything(tmp_path, library):
    path = tmp_path / "data" / "snapshots.db"
    path.parent.mkdir(parents=True)
    conn = db.connect(path)
    for step in db.MIGRATIONS[:2]:
        conn.executescript(step)
    conn.execute("PRAGMA user_version = 2")
    conn.execute("INSERT INTO assets (path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                 " VALUES ('orig.jpg', 'orig.jpg', 'photo', 1, 1, 'x', '2025-01-01T12:00:00', 'x')")
    conn.execute("INSERT INTO albums (name, created_at, updated_at) VALUES ('Kept', 'x', 'x')")
    conn.execute("INSERT INTO album_assets VALUES (1, 1, 'x')")
    conn.commit()
    conn.close()
    db.init(path)
    conn = db.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION
    row = conn.execute("SELECT quality_done, clip_done, ocr_done, dhash FROM assets").fetchone()
    assert tuple(row) == (0, 0, 0, None)  # will be measured by the background jobs
    assert conn.execute("SELECT COUNT(*) FROM album_assets").fetchone()[0] == 1
