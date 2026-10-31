"""0.11: date words, best matches, lookalike copies, the best shot of a burst, sorting (screenshots,
documents, receipts, highlights), suggested albums, guessed dates, video frames and face-aware crops."""

import os
from datetime import date, datetime

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFilter

from helpers import ids_by_name, make_jpeg, new_client, rescan, setup_admin, wait_for, wait_scan
from snapshots import analysis, clipsearch, dateguess, dates, db, faces, media, ranking, sorting
from test_search_models import FakeEngine

TODAY = date(2026, 10, 31)   # a Saturday


# ------------------------------------------------------------------ date words

def ranges(text):
    conds, rest, used = dates.parse(text, TODAY)
    return [(k, v if k != "ranges" else v[0]) for c in conds for k, v in c.items()
            if k in ("ranges", "months", "days")], rest


def test_date_words():
    assert ranges("dog last summer") == ([("ranges", ("2026-06-01T00:00:00", "2026-08-31T23:59:59"))], "dog")
    assert ranges("christmas 2023")[0] == [("ranges", ("2023-12-24T00:00:00", "2023-12-26T23:59:59"))]
    assert ranges("beach 2019") == ([("ranges", ("2019-01-01T00:00:00", "2019-12-31T23:59:59"))], "beach")
    assert ranges("yesterday")[0] == [("ranges", ("2026-10-30T00:00:00", "2026-10-30T23:59:59"))]
    assert ranges("last weekend")[0] == [("ranges", ("2026-10-24T00:00:00", "2026-10-25T23:59:59"))]
    assert ranges("this weekend")[0] == [("ranges", ("2026-10-31T00:00:00", "2026-11-01T23:59:59"))]
    assert ranges("3 years ago")[0] == [("ranges", ("2023-01-01T00:00:00", "2023-12-31T23:59:59"))]
    assert ranges("winter 2022")[0] == [("ranges", ("2022-12-01T00:00:00", "2023-02-28T23:59:59"))]
    assert ranges("last winter")[0] == [("ranges", ("2025-12-01T00:00:00", "2026-02-28T23:59:59"))]
    assert ranges("last june")[0] == [("ranges", ("2026-06-01T00:00:00", "2026-06-30T23:59:59"))]
    assert ranges("last december")[0] == [("ranges", ("2025-12-01T00:00:00", "2025-12-31T23:59:59"))]
    assert ranges("thanksgiving 2025")[0] == [("ranges", ("2025-11-27T00:00:00", "2025-11-30T23:59:59"))]
    assert ranges("easter 2024")[0] == [("ranges", ("2024-03-30T00:00:00", "2024-04-01T23:59:59"))]
    assert ranges("between 2018 and 2020")[0] == [("ranges", ("2018-01-01T00:00:00", "2020-12-31T23:59:59"))]
    assert ranges("2024-07-04")[0] == [("ranges", ("2024-07-04T00:00:00", "2024-07-04T23:59:59"))]
    assert ranges("halloween") == ([("days", [(10, 31)])], "")
    assert ranges("summer") == ([("months", [6, 7, 8])], "")
    assert ranges("june 2021 hiking") == ([("ranges", ("2021-01-01T00:00:00", "2021-12-31T23:59:59")),
                                          ("months", [6])], "hiking")
    # Not dates: kept as words
    assert ranges("2023-02-30 cake") == ([], "2023-02-30 cake")
    assert ranges("route 66") == ([], "route 66")


def test_word_tokens_drop_small_words_unless_that_is_all():
    assert dates.word_tokens("photos of mia from") == ["mia"]
    assert dates.word_tokens("the") == ["the"]


def test_every_easter_is_a_sunday():
    for y in range(1990, 2040):
        assert dates._easter(y).weekday() == 6
    assert dates._easter(2025) == date(2025, 4, 20)


def test_sql_and_python_agree():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    stamps = ["2023-12-25T10:00:00", "2024-07-04T09:00:00", "2026-06-15T12:00:00", "2021-10-31T20:00:00",
              "2022-01-01T00:30:00"]
    for text in ("christmas", "christmas 2023", "summer", "last summer", "halloween", "new year", "easter"):
        for cond in dates.parse(text, TODAY)[0]:
            sql, params = dates.sql(cond, "t")
            for t in stamps:
                got = conn.execute(f"SELECT {sql} FROM (SELECT ? AS t)", params + [t]).fetchone()[0]
                assert bool(got) == dates.matches(cond, t), (text, t)


def test_date_words_in_search(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "beach-a.jpg", "2019:07:01 10:00:00")
    make_jpeg(root / "beach-b.jpg", "2021:07:01 10:00:00")
    make_jpeg(root / "xmas.jpg", "2023:12:25 09:00:00")
    make_jpeg(root / "other.jpg", "2023:12:20 09:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert list(ids_by_name(c, q="beach 2019")) == ["beach-a.jpg"]
        assert list(ids_by_name(c, q="beach in july")) == ["beach-b.jpg", "beach-a.jpg"]
        assert list(ids_by_name(c, q="christmas")) == ["xmas.jpg"]
        assert list(ids_by_name(c, q="photos from christmas 2023")) == ["xmas.jpg"]
        assert ids_by_name(c, q="christmas 2022") == {}


# ------------------------------------------------------------------ best matches

def test_fuse_puts_items_found_both_ways_first():
    out = [i for i, _ in ranking.fuse([1, 2, 3], [4, 3, 5])]
    assert out[0] == 3
    assert set(out) == {1, 2, 3, 4, 5}
    assert out.index(1) < out.index(2) and out.index(4) < out.index(5)


def test_word_strength():
    row = {"people": ["Mia"], "tags": ["beach"], "description": None, "city": "Phoenix"}
    assert ranking.word_strength(row, ["mia", "phoenix", "img"]) == 2


def _colour_library(root, n_red=3):
    for k in range(n_red):
        make_jpeg(root / f"red{k}.jpg", f"2024:05:{k + 1:02d} 10:00:00", color=(220, 20, 20))
    for k in range(20):
        make_jpeg(root / f"blue{k:02d}.jpg", f"2024:06:{k + 1:02d} 10:00:00", color=(20, 20, 220))
    make_jpeg(root / "something-red-label.jpg", "2024:04:01 10:00:00", color=(20, 200, 20))


@pytest.fixture
def content_client(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    _colour_library(root)
    c, cfg = new_client(tmp_path, root, smart_search=True)
    monkeypatch.setattr(c.app.state.ctx.worker.clip, "load_engine", lambda: FakeEngine())
    monkeypatch.setattr(clipsearch.ClipJob, "ready", lambda self: True)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: db.connect(cfg.db_path).execute("SELECT COUNT(*) FROM clip_vectors").fetchone()[0] == 24)
        yield c, cfg


def test_best_matches_mix_words_and_content(content_client):
    c, cfg = content_client
    s = c.get("/api/search", params={"q": "something red"}).json()
    assert s["words"] == 1 and s["content"] >= 3
    assert s["mixed"] == s["words"] + s["content"]       # the green photo's name says "red"; it isn't red
    items = c.get("/api/assets", params={"q": "something red", "mix": True}).json()["items"]
    names = [i["filename"] for i in items]
    assert set(names[:4]) == {"red0.jpg", "red1.jpg", "red2.jpg", "something-red-label.jpg"}
    assert all("score" in i for i in items)
    # Date words still apply to content results
    items = c.get("/api/assets", params={"q": "2024 something red", "look": True}).json()["items"]
    assert {i["filename"] for i in items} <= {"red0.jpg", "red1.jpg", "red2.jpg"} and items
    assert c.get("/api/assets", params={"q": "something red 2019", "look": True}).json()["items"] == []
    # Only date words: nothing to search by content
    assert c.get("/api/search", params={"q": "may 2024"}).json()["content"] == 0


# ------------------------------------------------------------------ lookalike copies

def _vec(*head):
    v = np.zeros(512, np.float32)
    v[:len(head)] = head
    return (v / np.linalg.norm(v)).astype(np.float16).tobytes()


def test_lookalike_pairs():
    import sqlite3

    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE clip_vectors (asset_id INTEGER PRIMARY KEY, vec BLOB)")
    conn.executemany("INSERT INTO clip_vectors VALUES (?, ?)", [
        (1, _vec(1, 0.1, 0)), (2, _vec(1, 0.12, 0.01)), (3, _vec(0, 1, 0)), (4, _vec(0.7, 0.7, 0)),
        (5, _vec(1, 0.11, 0))])
    t = "2024-01-01T10:00:00"
    items = [(1, t, True), (2, t, True), (3, t, True), (4, t, True)]
    assert analysis.lookalike_pairs(conn, items, 0.97) == {(1, 2)}
    assert analysis.lookalike_pairs(conn, items, 0.0) == set()
    # The same view, a day later: not a copy...
    assert analysis.lookalike_pairs(conn, items + [(5, "2024-01-02T10:00:00", True)], 0.97) == {(1, 2)}
    # ...unless its date is only a guess (a copy saved without its date)
    assert analysis.lookalike_pairs(conn, items + [(5, "2025-06-01T10:00:00", False)], 0.97) == {(1, 2), (1, 5), (2, 5)}
    # many items, in several blocks
    many = [(i, _vec(1, i % 3, 0.5)) for i in range(10, 70)]
    conn.executemany("INSERT INTO clip_vectors VALUES (?, ?)", many)
    found = analysis.lookalike_pairs(conn, [(i, t, True) for i, _ in many], 0.999, block=7)
    assert len(found) == 3 * (20 * 19 // 2)


def test_lookalike_copies_in_clean_up(tmp_path):
    root = tmp_path / "Photos"
    rng = np.random.default_rng(1)
    pic = Image.fromarray(rng.integers(0, 255, (6, 8, 3), dtype=np.uint8)).resize((640, 480), Image.NEAREST)
    make_jpeg(root / "orig.jpg", "2024:01:01 10:00:00", image=pic)
    make_jpeg(root / "cropped.jpg", "2024:01:01 10:00:00", image=pic.crop((200, 0, 640, 480)).transpose(Image.FLIP_LEFT_RIGHT))
    make_jpeg(root / "other.jpg", "2024:01:01 10:00:30", image=pic.rotate(90))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/cleanup").json()["status"]["pending"] == 0)
        ids = ids_by_name(c)
        assert c.get("/api/cleanup").json()["groups"] == 0      # the fingerprints differ
        conn = db.connect(cfg.db_path)
        conn.executemany("INSERT INTO clip_vectors (asset_id, vec) VALUES (?, ?)", [
            (ids["orig.jpg"], _vec(1, 0.2, 0.1)), (ids["cropped.jpg"], _vec(1, 0.21, 0.1)),
            (ids["other.jpg"], _vec(0.2, 1, 0))])
        conn.execute("UPDATE assets SET clip_done = 1")
        conn.commit()
        groups = c.get("/api/cleanup/groups").json()["groups"]
        assert len(groups) == 1 and groups[0]["kind"] == "copies"
        assert {i["filename"] for i in groups[0]["items"]} == {"orig.jpg", "cropped.jpg"}
        assert groups[0]["keep"] == ids["orig.jpg"]     # more pixels
        assert c.get("/api/cleanup", params={"strictness": "strict"}).json()["groups"] == 1


# ------------------------------------------------------------------ faces: eyes and the best shot

def _face(eyes_open: bool, sharp: bool = True, turn: float = 0.0):
    """A grey cartoon face: skin, two eyes (white and a dark iris, or a closed lid line), nose, mouth."""
    img = Image.new("L", (400, 400), 70)
    d = ImageDraw.Draw(img)
    d.ellipse((100, 80, 300, 330), fill=180)
    eyes = [(160, 180), (240, 180)]
    for ex, ey in eyes:
        if eyes_open:
            d.ellipse((ex - 18, ey - 10, ex + 18, ey + 10), fill=245)
            d.ellipse((ex - 8, ey - 8, ex + 8, ey + 8), fill=30)
        else:
            d.line((ex - 18, ey + 2, ex + 18, ey + 2), fill=120, width=2)
    nose = (200 + turn * 80, 230)
    d.ellipse((nose[0] - 6, nose[1] - 6, nose[0] + 6, nose[1] + 6), fill=150)
    d.line((170, 280, 230, 280), fill=90, width=4)
    if not sharp:
        img = img.filter(ImageFilter.GaussianBlur(4))
    marks = [eyes[0][0], eyes[0][1], eyes[1][0], eyes[1][1], nose[0], nose[1], 170, 280, 230, 280]
    return np.asarray(img, np.float32), marks, (100, 80, 200, 250)


def test_face_quality_sees_closed_eyes_blur_and_turned_heads():
    open_ = faces.face_quality(*_face(True))
    shut = faces.face_quality(*_face(False))
    soft = faces.face_quality(*_face(True, sharp=False))
    turned = faces.face_quality(*_face(True, turn=0.5))
    assert shut["eyes"] < 0.65 * open_["eyes"]
    assert soft["sharp"] < open_["sharp"]
    assert open_["frontal"] == 1.0 and turned["frontal"] < 0.1


def test_best_shot_skips_the_blink():
    members = [{"id": i, "sharpness": s} for i, s in ((1, 300), (2, 320), (3, 280))]
    f = lambda aid, eyes, sharp, person=7: {"asset_id": aid, "person_id": person, "x": 0.4, "y": 0.3, "w": 0.2,  # noqa: E731
                                           "h": 0.3, "eyes": eyes, "sharp": sharp, "frontal": 1.0}
    found = {1: [f(1, 1.0, 50)], 2: [f(2, 0.4, 60)], 3: [f(3, 0.95, 45)]}
    keep, why = analysis.best_shot(members, found)
    assert keep == 1 and why == "Eyes open"
    # Nobody blinks: the sharpest faces win
    found[2] = [f(2, 1.0, 70)]
    assert analysis.best_shot(members, found) == (2, "Sharpest faces")
    # No measured faces: no opinion
    assert analysis.best_shot(members, {}) is None


# ------------------------------------------------------------------ sorting

def test_classify_without_the_model():
    assert sorting.classify(filename="Screenshot_20240101-101010.png", camera=None, width=1080, height=2400) == "screenshot"
    assert sorting.classify(filename="Screenshot 2024.png", camera="Pixel 8", width=1080, height=2400) is None
    assert sorting.classify(filename="image.png", camera=None, width=1170, height=2532) == "screenshot"
    assert sorting.classify(filename="image.png", camera=None, width=500, height=500) is None
    receipt = "STORE 12\nMilk 3.49\nBread 2.99\nEggs 4.19\nSUBTOTAL 10.67\nTAX 0.85\nTOTAL 11.52\nVISA"
    assert sorting.classify(filename="IMG_1.jpg", camera="iPhone", width=3000, height=4000, text=receipt) == "receipt"
    assert sorting.classify(filename="IMG_2.jpg", camera="iPhone", width=3000, height=4000, text="EXIT 12") is None


def test_classify_with_the_model():
    p = lambda **kw: {"photo": 0.0, "screenshot": 0.0, "document": 0.0, "receipt": 0.0, **kw}  # noqa: E731
    kw = dict(filename="IMG_5.jpg", camera="iPhone 15", width=3000, height=4000)
    assert sorting.classify(**kw, probs=p(document=0.9, photo=0.1)) == "document"
    assert sorting.classify(**kw, probs=p(document=0.5, photo=0.45)) is None          # not sure enough
    assert sorting.classify(**kw, probs=p(receipt=0.8, photo=0.2)) == "receipt"
    # A camera took it: a photo of a screen, not a screenshot
    assert sorting.classify(**kw, probs=p(screenshot=0.9, photo=0.1)) is None
    kw2 = dict(filename="20240101.png", camera=None, width=1080, height=1920)
    assert sorting.classify(**kw2, probs=p(screenshot=0.6, photo=0.4)) == "screenshot"   # its shape decides


def _png(path, size=(1080, 2400), color=(240, 240, 240)):
    path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", size, color).save(path)
    os.utime(path, (datetime(2024, 3, 1, 12).timestamp(),) * 2)


def test_sorting_through_the_app(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "IMG_0001.jpg", "2024:03:01 10:00:00", camera=("Canon", "EOS R6"))
    _png(root / "Screenshot_20240301-101010.png")
    _png(root / "chat.png", size=(1170, 2532))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/sorting").json()["pending"] == 0)
        so = c.get("/api/sorting").json()
        assert {x["category"]: x["count"] for x in so["categories"]} == {"screenshot": 2, "document": 0, "receipt": 0}
        assert set(ids_by_name(c, category="screenshot")) == {"Screenshot_20240301-101010.png", "chat.png"}
        assert set(ids_by_name(c, category="photos")) == {"IMG_0001.jpg"}
        item = c.get("/api/assets", params={"category": "screenshot"}).json()["items"][0]
        assert item["category"] == "screenshot"
        # Not a screenshot after all: set by hand, and it stays after sorting again
        chat = ids_by_name(c)["chat.png"]
        assert c.post("/api/assets/category", json={"ids": [chat], "category": "photo"}).json()["updated"] == 1
        conn = db.connect(cfg.db_path)
        conn.execute("UPDATE assets SET sort_done = 0")
        conn.commit()
        c.app.state.ctx.worker.poke()
        wait_for(lambda: c.get("/api/sorting").json()["pending"] == 0)
        assert set(ids_by_name(c, category="screenshot")) == {"Screenshot_20240301-101010.png"}
        # Leave them out of the timeline, when asked
        assert len(ids_by_name(c, archived="hide")) == 3
        r = c.put("/api/settings/sorting", json={"hide_clutter": True}).json()
        assert r["hide_clutter"] is True
        assert set(ids_by_name(c, archived="hide")) == {"IMG_0001.jpg", "chat.png"}
        assert len(ids_by_name(c, archived="hide", category="all")) == 3
        assert len(ids_by_name(c, archived="hide", q="2024")) == 3      # search still finds them


def test_sorting_with_the_model_scores_beauty(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00", color=(220, 20, 20))
    c, cfg = new_client(tmp_path, root, smart_search=True)
    monkeypatch.setattr(c.app.state.ctx.worker.clip, "load_engine", lambda: FakeEngine())
    monkeypatch.setattr(clipsearch.ClipJob, "ready", lambda self: True)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: db.connect(cfg.db_path).execute(
            "SELECT COUNT(*) FROM assets WHERE beauty IS NOT NULL AND sort_done = 1").fetchone()[0] == 1)


def test_highlights(tmp_path):
    root = tmp_path / "Photos"
    for k in range(30):
        make_jpeg(root / f"p{k:02d}.jpg", f"2024:01:{k % 28 + 1:02d} 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        assert ids_by_name(c, highlights=True) == {}
        conn = db.connect(cfg.db_path)
        for k, (aid,) in enumerate(conn.execute("SELECT id FROM assets ORDER BY filename").fetchall()):
            conn.execute("UPDATE assets SET beauty = ? WHERE id = ?", (k / 1000, aid))
        conn.commit()
        assert list(ids_by_name(c, highlights=True)) == ["p29.jpg"]       # the best 4% of 30: one
        conn.execute("UPDATE assets SET beauty = NULL WHERE filename = 'p29.jpg'")
        conn.commit()
        assert list(ids_by_name(c, highlights=True)) == ["p28.jpg"]
        assert c.get("/api/sorting").json()["highlights"] == 1


def test_beauty_picks_event_covers():
    items = [{"id": i, "sharpness": 100 + i, "beauty": b, "category": None}
             for i, b in enumerate([0.01, 0.03, -0.02, 0.02])]
    assert analysis._cover(items) == 1
    items[1]["category"] = "screenshot"
    assert analysis._cover(items) == 3


def test_suggested_albums(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    for k in range(14):
        make_jpeg(root / f"r{k:02d}.jpg", f"2024:01:{k + 1:02d} 10:00:00")
    c, cfg = new_client(tmp_path, root)
    worker = c.app.state.ctx.worker
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = sorted(ids_by_name(c).values())
        monkeypatch.setattr(clipsearch.ClipJob, "ready", lambda self: True)
        monkeypatch.setattr(clipsearch.ClipJob, "search",
                            lambda self, conn, text: [(i, 0.3) for i in ids] if text == "a dog" else [(ids[0], 0.3)])
        out = c.get("/api/suggestions/albums").json()
        assert [x["name"] for x in out["suggestions"]] == ["Dogs"]
        assert out["suggestions"][0]["count"] == 14
        a = c.post("/api/albums", json={"name": "My dog", "rule": {"look": "a dog"}}).json()
        assert a["id"]
        worker.clip.index._key = None
        assert c.get("/api/suggestions/albums").json()["suggestions"] == []


# ------------------------------------------------------------------ dates for files without one

def test_guess_between():
    near = [("IMG_0100.JPG", "2024-05-01T10:00:00"), ("IMG_0104.JPG", "2024-05-01T10:40:00"),
            ("DSC_0102.JPG", "2020-01-01T00:00:00")]
    assert dateguess.guess_between("IMG_0102.JPG", near) == "2024-05-01T10:20:00"
    assert dateguess.guess_between("IMG_0105.JPG", near) == "2024-05-01T10:40:00"
    assert dateguess.guess_between("IMG_0200.JPG", near) is None
    assert dateguess.guess_between("holiday.jpg", near) is None
    far = [("IMG_0100.JPG", "2024-05-01T10:00:00"), ("IMG_0104.JPG", "2024-06-01T10:00:00")]
    assert dateguess.guess_between("IMG_0102.JPG", far) is None


def test_dates_guessed_from_the_files_beside(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "trip" / "IMG_0001.jpg", "2020:05:01 10:00:00")
    make_jpeg(root / "trip" / "IMG_0002.jpg")                     # no date inside
    make_jpeg(root / "trip" / "IMG_0003.jpg", "2020:05:01 10:10:00")
    make_jpeg(root / "lonely.jpg")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: db.connect(cfg.db_path).execute(
            "SELECT COUNT(*) FROM assets WHERE date_checked = 0").fetchone()[0] == 0)
        ids = ids_by_name(c)
        one = c.get(f"/api/assets/{ids['IMG_0002.jpg']}").json()
        assert one["taken_at"] == "2020-05-01T10:05:00" and one["date_guess"] == "nearby"
        assert c.get(f"/api/assets/{ids['lonely.jpg']}").json()["date_guess"] == "file"
        assert c.get(f"/api/assets/{ids['IMG_0001.jpg']}").json()["date_guess"] is None
        # Changed by hand: no longer a guess
        c.post("/api/assets/details", json={"ids": [ids["lonely.jpg"]], "taken_at": "2019-01-01T12:00"})
        assert c.get(f"/api/assets/{ids['lonely.jpg']}").json()["date_guess"] is None


def test_gps_time():
    gps = {29: "2021:06:01", 7: (18.0, 30.0, 5.0)}
    utc = media._gps_time(gps)
    assert utc == datetime(2021, 6, 1, 18, 30, 5)
    assert media.local_from_gps(utc, -112.07) == datetime(2021, 6, 1, 11, 30, 5)   # Phoenix: UTC-7
    assert media._gps_time({29: "0000:00:00"}) is None and media._gps_time({}) is None


def test_gps_time_dates_a_photo_with_no_date(tmp_path):
    root = tmp_path / "Photos"
    root.mkdir()
    img = Image.new("RGB", (64, 48), (10, 120, 200))
    exif = Image.Exif()
    g = exif.get_ifd(0x8825)
    g[1], g[2], g[3], g[4] = "N", (33.0, 27.0, 0.0), "W", (112.0, 4.0, 0.0)
    g[29], g[7] = "2021:06:01", (18.0, 30.0, 5.0)
    img.save(root / "nodate.jpg", "JPEG", exif=exif)
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        one = c.get(f"/api/assets/{ids_by_name(c)['nodate.jpg']}").json()
        assert one["taken_at"] == "2021-06-01T11:30:05" and one["date_guess"] == "gps"


# ------------------------------------------------------------------ video frames and face-aware crops

def test_frame_score():
    rng = np.random.default_rng(3)
    sharp = Image.fromarray(rng.integers(40, 220, (12, 16), dtype=np.uint8)).resize((320, 240), Image.NEAREST).convert("RGB")
    blurred = sharp.filter(ImageFilter.GaussianBlur(5))
    black = Image.new("RGB", (320, 240), (2, 2, 2))
    dark = Image.fromarray((np.asarray(sharp) * 0.08).astype(np.uint8))
    assert media.frame_score(black) == -1.0
    assert media.frame_score(sharp) > media.frame_score(blurred) > 0
    assert media.frame_score(sharp) > media.frame_score(dark)


@pytest.mark.skipif(not media.has_ffmpeg(), reason="needs ffmpeg")
def test_video_thumbnail_skips_a_black_frame(tmp_path):
    import subprocess

    src = tmp_path / "v.mp4"
    # 2 seconds black, then 4 seconds of a test pattern
    subprocess.run(["ffmpeg", "-v", "error", "-f", "lavfi", "-i", "color=black:s=160x120:d=2", "-f", "lavfi",
                    "-i", "testsrc=s=160x120:d=4", "-filter_complex", "[0][1]concat=n=2:v=1", "-pix_fmt", "yuv420p",
                    str(src)], check=True)
    dst = tmp_path / "t.jpg"
    assert media.make_video_thumb(src, dst)
    with Image.open(dst) as img:
        assert np.asarray(img.convert("L")).mean() > 30


def test_face_focus_in_grid_items(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "p.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        aid = ids_by_name(c)["p.jpg"]
        assert "focus" not in c.get("/api/assets").json()["items"][0]
        conn = db.connect(cfg.db_path)
        conn.execute("INSERT INTO faces (asset_id, x, y, w, h, score, embedding, created_at)"
                     " VALUES (?, 0.7, 0.1, 0.2, 0.2, 0.99, x'00', '2024-01-01')", (aid,))
        conn.commit()
        assert c.get("/api/assets").json()["items"][0]["focus"] == [0.8, 0.2]


def test_holidays_and_seasons_are_also_words():
    text, firm = dates.for_content("christmas tree", TODAY)
    assert text == "christmas tree" and firm == []
    text, firm = dates.for_content("christmas tree 2023", TODAY)
    assert text == "christmas tree" and firm == [{"ranges": [("2023-01-01T00:00:00", "2023-12-31T23:59:59")]}]
    text, firm = dates.for_content("fall leaves last summer", TODAY)
    assert text == "fall leaves" and len(firm) == 1
    text, firm = dates.for_content("dog 2019", TODAY)
    assert text == "dog" and firm[0]["ranges"][0][0] == "2019-01-01T00:00:00"


def test_soft_dates_match_words_too(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "tree.jpg", "2023:12:15 10:00:00")
    make_jpeg(root / "day.jpg", "2023:12:25 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/assets/details", json={"ids": [ids["tree.jpg"]], "description": "Our Christmas tree"})
        assert set(ids_by_name(c, q="christmas")) == {"tree.jpg", "day.jpg"}
        assert set(ids_by_name(c, q="christmas 2023")) == {"tree.jpg", "day.jpg"}
        assert set(ids_by_name(c, q="christmas 2022")) == set()
