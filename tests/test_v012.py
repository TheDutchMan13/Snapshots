"""Version 0.12: trips, forgiving search, more like this, faces across the years, colours, varied
highlights, sideways photos, pets and the photo-quality model. Synthetic data: no models needed."""

from datetime import datetime, timedelta

import pytest

np = pytest.importorskip("numpy")

from snapshots import analysis


# ------------------------------------------------------------------ trips: home by days, per year

def _row(i, when, city, country="NL", state=None):
    return {"id": i, "taken_at": when.isoformat(timespec="seconds"), "lat": None, "lon": None, "city": city,
            "state": state, "country": country, "sharpness": None, "beauty": None, "category": None}


def test_home_is_where_most_days_are_not_most_photos():
    rows, i = [], 0
    start = datetime(2023, 1, 1, 12)
    for d in range(30):                      # a photo a day at home
        i += 1
        rows.append(_row(i, start + timedelta(days=d * 7), "Utrecht"))
    for k in range(200):                     # one big holiday: many photos on four days
        i += 1
        rows.append(_row(i, datetime(2023, 7, 1, 9) + timedelta(hours=k // 50 * 24, minutes=k), "Rome", "IT"))
    rows.sort(key=lambda r: r["taken_at"])
    homes = analysis.homes_by_year(rows)
    assert homes[None] == ("Utrecht", "NL") and homes["2023"] == ("Utrecht", "NL")
    kinds = [(e["kind"], e["place"]) for e in analysis.find_events(rows, min_items=8)]
    assert ("trip", "Rome") in kinds


def test_moving_house_moves_home():
    rows, i = [], 0
    for year, town in ((2020, "Utrecht"), (2021, "Utrecht"), (2022, "Phoenix"), (2023, "Phoenix"), (2024, "Phoenix")):
        for d in range(20):
            i += 1
            rows.append(_row(i, datetime(year, 1, 1, 12) + timedelta(days=d * 10), town, "NL" if town == "Utrecht" else "US"))
    homes = analysis.homes_by_year(rows)
    assert homes[None] == ("Phoenix", "US")
    assert homes["2020"] == ("Utrecht", "NL") and homes["2023"] == ("Phoenix", "US")


def test_a_long_holiday_does_not_move_home():
    rows, i = [], 0
    for d in range(12):
        i += 1
        rows.append(_row(i, datetime(2023, 1, 1, 12) + timedelta(days=d * 20), "Utrecht"))
    for d in range(14):                      # two weeks away, a photo every day
        i += 1
        rows.append(_row(i, datetime(2023, 8, 1, 12) + timedelta(days=d), "Nice", "FR"))
    for d in range(40):                      # other years at home
        i += 1
        rows.append(_row(i, datetime(2022, 1, 1, 12) + timedelta(days=d * 9), "Utrecht"))
    assert analysis.homes_by_year(rows)["2023"] == ("Utrecht", "NL")


# ------------------------------------------------------------------ forgiving search

from snapshots import db, spelling  # noqa: E402
from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_scan  # noqa: E402


def test_distance_counts_a_swap_as_one():
    assert spelling.distance("birthday", "birthdy") == 1
    assert spelling.distance("birthday", "brithday") == 1       # two letters swapped
    assert spelling.distance("beach", "bench") == 1
    assert spelling.distance("sunset", "unrelated", 2) == 3


def test_word_table_finds_near_words():
    t = spelling.WordTable({"birthday": 5, "phoenix": 3, "sunset": 1})
    assert (1, "birthday") in t.candidates("birthdy", 1)
    assert (2, "phoenix") in t.candidates("pheonx", 2)
    assert t.candidates("zzzz", 1) == []


class _Cfg:
    def __init__(self, tmp_path):
        self.model_dir = tmp_path / "models"
        self.db_path = tmp_path / "x.db"


def _speller(tmp_path, library, english=()):
    sp = spelling.Speller(_Cfg(tmp_path))
    sp.library = spelling.WordTable(library)
    sp.english = spelling.WordTable({w: 1 for w in english}) if english else None
    sp.total = 100
    spelling._correct.cache_clear()
    return sp


def test_misspelled_words_are_corrected_ordinary_ones_are_not(tmp_path):
    sp = _speller(tmp_path, {"birthday": 12, "phoenix": 40, "grandma": 8},
                  english=["sunset", "beach", "dog", "birthday"])
    assert sp.fix("birthdy party") == "birthday party"
    assert sp.fix("Pheonix") == "phoenix"
    assert sp.fix("sunest") == "sunset"                 # an English word, for search by content
    assert sp.fix("beach sunset") == "beach sunset"     # ordinary words stay as typed
    assert sp.fix("may 2021") == "may 2021"             # dates are left alone
    assert sp.fix("xyzzyq") == "xyzzyq"                 # nothing near: as typed
    assert sp.fix("grandm") == "grandm"                 # part of a word in the library: it already finds photos
    assert sp.rarity("grandma") > sp.rarity("phoenix")


def test_related_words_find_each_other_and_typos_are_fixed_in_search(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00")
    make_jpeg(root / "c.jpg", "2024:01:03 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        r = c.post("/api/assets/details", json={"ids": [ids["a.jpg"]], "description": "our new puppy"})
        assert r.status_code == 200, r.text
        c.post("/api/assets/details", json={"ids": [ids["b.jpg"]], "description": "Birthday cake"})
        found = {i["id"] for i in c.get("/api/assets", params={"q": "dog"}).json()["items"]}
        assert found == {ids["a.jpg"]}                  # "dog" finds the puppy
        with db.session(cfg.db_path) as conn:            # gather the words now (normally in the background)
            c.app.state.ctx.speller.refresh(conn)
        s = c.get("/api/search", params={"q": "birthdy"}).json()
        assert s["q"] == "birthday" and s["typed"] == "birthdy" and s["words"] == 1
        found = {i["id"] for i in c.get("/api/assets", params={"q": "birthdy"}).json()["items"]}
        assert found == {ids["b.jpg"]}
        assert c.get("/api/search", params={"q": "cake"}).json()["typed"] is None


# ------------------------------------------------------------------ more like this

def _unit(v):
    v = np.asarray(v, np.float32)
    return v / np.linalg.norm(v)


def _vectors(cfg, vecs: dict):
    with db.session(cfg.db_path) as conn:
        for aid, v in vecs.items():
            conn.execute("INSERT OR REPLACE INTO clip_vectors (asset_id, vec) VALUES (?, ?)",
                         (aid, _unit(v).astype(np.float16).tobytes()))
            conn.execute("UPDATE assets SET clip_done = 1 WHERE id = ?", (aid,))


def test_more_like_this_lists_the_most_alike_first(tmp_path):
    root = tmp_path / "Photos"
    for n in "abcde":
        make_jpeg(root / f"{n}.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        rng = np.random.default_rng(1)
        base = _unit(rng.normal(size=512))
        other = _unit(rng.normal(size=512))
        _vectors(cfg, {ids["a.jpg"]: base, ids["b.jpg"]: base + 0.2 * other, ids["c.jpg"]: base + 0.5 * other,
                       ids["d.jpg"]: other, ids["e.jpg"]: -base})
        got = [i["id"] for i in c.get("/api/assets", params={"like": ids["a.jpg"]}).json()["items"]]
        assert got == [ids["b.jpg"], ids["c.jpg"]]          # the photo itself and unlike ones are left out
        assert c.get("/api/assets", params={"like": ids["d.jpg"]}).json()["items"] == []
        assert c.get("/api/assets", params={"like": 999999}).status_code == 404


# ------------------------------------------------------------------ faces across the years

from snapshots import faces  # noqa: E402


def test_groups_one_after_the_other_in_time_count_as_more_alike():
    young = ("2015-01-01T00:00:00", "2017-12-31T00:00:00")
    older = ("2018-06-01T00:00:00", "2021-01-01T00:00:00")
    assert faces.years_apart_bonus(young, older) == faces.YEARS_APART_BONUS
    assert faces.years_apart_bonus(older, young) == faces.YEARS_APART_BONUS
    assert faces.years_apart_bonus(young, ("2016-01-01T00:00:00", "2019-01-01T00:00:00")) == 0   # overlap
    assert faces.years_apart_bonus(young, ("2025-01-01T00:00:00", "2026-01-01T00:00:00")) == 0   # far apart


def _db_with_people(tmp_path, groups):
    """groups: [(person id, year, [embeddings])]; each face in a photo of its own, taken that year."""
    db.init(tmp_path / "f.db")
    conn = db.connect(tmp_path / "f.db")
    n = 0
    for pid, year, embs in groups:
        conn.execute("INSERT INTO people (id, created_at) VALUES (?, 't')", (pid,))
        for k, e in enumerate(embs):
            n += 1
            conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                         " VALUES (?, ?, ?, 'photo', 1, 1, ?, ?, 't')",
                         (n, f"p{n}", f"p{n}", f"s{n}", f"{year}-0{1 + k % 9}-01T10:00:00"))
            conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                         " VALUES (?, ?, 0, 0, 1, 1, 1, ?, 't')", (n, pid, e.astype(np.float32).tobytes()))
    return conn


def _near(base, cos, seed):
    rng = np.random.default_rng(seed)
    u = rng.normal(size=128)
    u -= (u @ base) * base
    u /= np.linalg.norm(u)
    v = cos * base + np.sqrt(1 - cos * cos) * u
    return v / np.linalg.norm(v)


def test_a_child_at_two_ages_is_suggested_and_people_seen_together_are_not(tmp_path):
    rng = np.random.default_rng(3)
    kid = rng.normal(size=128)
    kid /= np.linalg.norm(kid)
    young = _near(kid, 0.6, 1)
    older = _near(kid, 0.6, 2)          # the two ages are only about 0.3 alike on their own
    g1 = [_near(young, 0.95, 10 + k) for k in range(4)]
    g2 = [_near(older, 0.95, 20 + k) for k in range(4)]
    conn = _db_with_people(tmp_path, [(1, 2016, g1), (2, 2019, g2)])
    raw = float(np.mean([a @ b for a in g1 for b in g2]))   # how alike the groups are on average
    threshold = raw + 0.02              # just too far apart on faces alone
    got = faces.suggestions(conn, 3, threshold)
    assert [(s["a"], s["b"]) for s in got] in ([(1, 2)], [(2, 1)])
    # the same two groups at the same time: no question
    conn.execute("UPDATE assets SET taken_at = '2016-05-01T10:00:00'")
    assert faces.suggestions(conn, 3, threshold) == []
    # and seen side by side in one photo: never asked, however alike
    conn.execute("UPDATE faces SET asset_id = 1 WHERE id IN (1, 5)")
    assert faces.suggestions(conn, 3, 0.0) == []
    conn.close()


# ------------------------------------------------------------------ colours

from snapshots import colors  # noqa: E402
from helpers import wait_for  # noqa: E402


def test_main_colours_are_named():
    from PIL import Image

    assert colors.main_colors(Image.new("RGB", (80, 80), (220, 20, 20))) == ["red"]
    assert colors.main_colors(Image.new("RGB", (80, 80), (120, 70, 30))) == ["brown"]
    sky = Image.new("RGB", (80, 80), (20, 60, 200))
    sky.paste((245, 245, 245), (0, 0, 80, 25))
    assert colors.main_colors(sky) == ["blue", "white"]
    assert colors.color_words("Red car in the GREY rain") == {"red", "gray"}


def test_colour_words_find_photos_by_their_colours(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "r.jpg", "2024:01:01 10:00:00", color=(220, 20, 20))
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00", color=(20, 20, 220))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)

        def done():
            with db.session(cfg.db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM assets WHERE colors_done = 0").fetchone()[0] == 0
        wait_for(done, timeout=30)
        assert {i["id"] for i in c.get("/api/assets", params={"q": "red"}).json()["items"]} == {ids["r.jpg"]}
        assert {i["id"] for i in c.get("/api/assets", params={"q": "grey"}).json()["items"]} == set()
        assert {i["id"] for i in c.get("/api/assets", params={"q": "blue"}).json()["items"]} == {ids["b.jpg"]}


# ------------------------------------------------------------------ varied highlights

from snapshots import sorting  # noqa: E402


def test_highlights_keep_one_photo_per_moment_and_a_few_per_day(tmp_path):
    db.init(tmp_path / "h.db")
    conn = db.connect(tmp_path / "h.db")
    rng = np.random.default_rng(4)
    scene = _unit(rng.normal(size=512))
    n = 0

    def add(when, beauty, vec):
        nonlocal n
        n += 1
        conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at, beauty)"
                     " VALUES (?, ?, ?, 'photo', 1, 1, ?, ?, 't', ?)", (n, f"p{n}", f"p{n}", f"s{n}", when, beauty))
        conn.execute("INSERT INTO clip_vectors (asset_id, vec) VALUES (?, ?)",
                     (n, _unit(vec).astype(np.float16).tobytes()))
        return n

    burst = [add(f"2024-05-01T10:0{k}:00", 0.9 + k / 100, scene + 0.01 * rng.normal(size=512)) for k in range(5)]
    other = add("2024-05-01T10:06:00", 0.8, rng.normal(size=512))              # same time, other scene
    later = [add(f"2024-05-0{2 + k}T12:00:00", 0.7, rng.normal(size=512)) for k in range(3)]
    busy = [add(f"2024-06-01T{8 + k:02d}:00:00", 0.6 + k / 100, rng.normal(size=512)) for k in range(8)]
    for k in range(400):                                                        # the rest of the library
        add(f"2023-01-01T00:{k % 60:02d}:00", -1.0, rng.normal(size=512))
    sorting.pick_highlights(conn)
    picked = {r[0] for r in conn.execute("SELECT asset_id FROM highlight_picks")}
    assert burst[-1] in picked and not set(burst[:-1]) & picked                   # the best of the burst only
    assert other in picked and set(later) <= picked
    assert len(set(busy) & picked) == sorting.PER_DAY                            # a few from a busy day
    conn.close()


# ------------------------------------------------------------------ sideways photos

from snapshots import sideways  # noqa: E402


def test_a_turn_is_suggested_only_when_it_fits_clearly_better():
    fit = lambda v: float(v[0])                                   # noqa: E731 - how upright, made up
    vec = lambda x: np.array([x], np.float32)                     # noqa: E731
    assert sideways.best_turn(fit, {0: vec(0.20), 90: vec(0.28), 180: vec(0.10), 270: vec(0.21)}) == 90
    assert sideways.best_turn(fit, {0: vec(0.20), 90: vec(0.22), 180: vec(0.10), 270: vec(0.21)}) is None
    assert sideways.best_turn(fit, {0: vec(0.30), 90: vec(0.10), 180: vec(0.10), 270: vec(0.10)}) is None


def test_sideways_photos_are_listed_turned_and_dismissed(tmp_path):
    root = tmp_path / "Photos"
    for n in "abc":
        make_jpeg(root / f"{n}.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET turn = 90, turn_done = 1 WHERE id IN (?, ?)", (ids["a.jpg"], ids["b.jpg"]))
        listed = c.get("/api/assets", params={"turned": "true"}).json()["items"]
        assert {i["id"] for i in listed} == {ids["a.jpg"], ids["b.jpg"]} and all(i["turn"] == 90 for i in listed)
        assert c.get("/api/sorting").json()["sideways"] == 2
        assert c.post("/api/sideways/dismiss", json={"ids": [ids["b.jpg"]]}).json()["updated"] == 1
        # turning it (as the page does) is an edit, and it is no longer listed
        r = c.post(f"/api/assets/{ids['a.jpg']}/edits", json={"edits": {"rotate": 90}})
        assert r.status_code == 200 and r.json()["turn"] is None
        assert c.get("/api/assets", params={"turned": "true"}).json()["items"] == []


# ------------------------------------------------------------------ pets

from snapshots import clipsearch, pets  # noqa: E402


class AnimalEngine:
    """Stands in for CLIP: red pictures are dogs, blue ones cats, green ones anything else."""

    AXES = {"dog": 0, "cat": 1}

    def _unit(self, v):
        v = np.asarray(v, np.float32)
        return v / np.linalg.norm(v)

    def embed_image(self, img):
        r, g, b = np.asarray(img.convert("RGB").resize((8, 8)), np.float32).reshape(-1, 3).mean(0) / 255
        return self._unit(np.concatenate([[r, b, g], np.full(509, 0.01)]))

    def embed_text(self, text):
        axis = next((k for w, k in self.AXES.items() if w in text), 2)
        v = np.full(512, 0.01)
        v[axis] = 1.0
        return self._unit(v)


def test_dogs_are_found_and_grouped_into_a_pet(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    for k in range(4):
        make_jpeg(root / f"dog{k}.jpg", f"2024:01:0{k + 1} 10:00:00", color=(230, 30, 30))
    for k in range(2):
        make_jpeg(root / f"field{k}.jpg", f"2024:02:0{k + 1} 10:00:00", color=(30, 200, 30))
    c, cfg = new_client(tmp_path, root, smart_search=True)
    monkeypatch.setattr(c.app.state.ctx.worker.clip, "load_engine", lambda: AnimalEngine())
    monkeypatch.setattr(clipsearch.ClipJob, "ready", lambda self: True)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)

        def done():
            with db.session(cfg.db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM assets WHERE pets_done = 0 OR clip_done = 0"
                                    " OR turn_done = 0").fetchone()[0] == 0
        wait_for(done, timeout=60)
        listed = c.get("/api/pets").json()["pets"]
        assert len(listed) == 1 and listed[0]["species"] == "dog" and listed[0]["photos"] == 4
        pid = listed[0]["id"]
        dogs = {ids[f"dog{k}.jpg"] for k in range(4)}
        assert {i["id"] for i in c.get("/api/assets", params={"pet": pid}).json()["items"]} == dogs
        assert c.get(f"/api/pets/{pid}/thumb").status_code == 200
        c.patch(f"/api/pets/{pid}", json={"name": "Charlie"})
        assert {i["id"] for i in c.get("/api/assets", params={"q": "charlie"}).json()["items"]} == dogs
        r = c.post(f"/api/pets/{pid}/remove", json={"ids": [ids["dog0.jpg"]]})
        assert r.json()["photos"] == 3
        # nothing here is sideways: plain colours look the same every way up
        assert c.get("/api/sorting").json()["sideways"] == 0
        # a pet only in someone's locked folder isn't shown to anyone
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET locked_by = 1 WHERE filename LIKE 'dog%'")
        assert c.get(f"/api/pets/{pid}").status_code == 404
        assert c.get("/api/pets").json()["pets"] == []


def test_pet_groups_keep_species_apart(tmp_path):
    db.init(tmp_path / "p.db")
    conn = db.connect(tmp_path / "p.db")
    g = pets.PetGroups(conn)
    v = np.zeros(512, np.float32)
    v[0] = 1
    g.add(1, v, "dog")
    assert g.match(v, "dog") == 1 and g.match(v, "cat") is None
    conn.close()


def test_words_in_file_names_are_not_corrected_and_exact_search_is_possible(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "maria_graduation.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/assets/details", json={"ids": [ids["b.jpg"]], "description": "Marie at school"})
        with db.session(cfg.db_path) as conn:
            c.app.state.ctx.speller.refresh(conn)
        s = c.get("/api/search", params={"q": "maria"}).json()
        assert s["typed"] is None and s["words"] == 1          # found as typed: left alone
        s = c.get("/api/search", params={"q": "mariee"}).json()
        assert s["q"] == "marie"
        s = c.get("/api/search", params={"q": "mariee", "exact": True}).json()
        assert s["typed"] is None and s["words"] == 0


def test_related_and_colour_words_match_whole_words_only(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/assets/details", json={"ids": [ids["a.jpg"]], "description": "Ray at the lake"})
        c.post("/api/assets/details", json={"ids": [ids["b.jpg"]], "description": "Tomato soup"})
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET colors = 'gray white', colors_done = 1")
        assert {i["id"] for i in c.get("/api/assets", params={"q": "ray"}).json()["items"]} == {ids["a.jpg"]}
        assert {i["id"] for i in c.get("/api/assets", params={"q": "gray"}).json()["items"]} == {ids["a.jpg"], ids["b.jpg"]}
        assert c.get("/api/assets", params={"q": "grandma"}).json()["items"] == []     # not "tomato"


def test_locked_photos_never_win_a_highlight(tmp_path):
    db.init(tmp_path / "h.db")
    conn = db.connect(tmp_path / "h.db")
    rng = np.random.default_rng(6)
    scene = _unit(rng.normal(size=512))
    conn.execute("INSERT INTO users (id, username, pw_hash, created_at) VALUES (1, 'sam', 'x', 't')")
    for k in range(60):
        conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at, beauty,"
                     " locked_by) VALUES (?, ?, ?, 'photo', 1, 1, ?, ?, 't', ?, ?)",
                     (k + 1, f"p{k}", f"p{k}", f"s{k}", f"2024-05-01T10:00:{k:02d}", (9.0 if k == 0 else 5.0) - k / 100,
                      1 if k == 0 else None))
        conn.execute("INSERT INTO clip_vectors (asset_id, vec) VALUES (?, ?)",
                     (k + 1, _unit(scene + 0.01 * rng.normal(size=512)).astype(np.float16).tobytes()))
    sorting.pick_highlights(conn)
    picked = [r[0] for r in conn.execute("SELECT asset_id FROM highlight_picks")]
    assert picked == [2]                      # the moment is still there, from a photo everyone can see
    conn.close()
