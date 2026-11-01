"""Version 0.13: guessed places, too bright and shut eyes, birthdays and earlier trips in On this day,
who was there, event titles, captions and landmarks. Synthetic data: no models are downloaded."""

from datetime import date, datetime, timedelta

import pytest

np = pytest.importorskip("numpy")

from snapshots import analysis, captions, db, eventnames, eyes, landmarks, placeguess, xmp  # noqa: E402
from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan  # noqa: E402


def _t(text):
    return datetime.fromisoformat(text)


# ------------------------------------------------------------------ guessed places

def test_a_place_is_guessed_from_photos_close_in_time_and_place():
    before = (_t("2024-05-01T10:00:00"), 52.0, 5.0)
    after = (_t("2024-05-01T10:40:00"), 52.004, 5.004)
    # between two photos close together: in between, by time
    lat, lon = placeguess.guess(_t("2024-05-01T10:10:00"), before, after)
    assert abs(lat - 52.001) < 1e-6 and abs(lon - 5.001) < 1e-6
    # one side only: within 15 minutes
    assert placeguess.guess(_t("2024-05-01T10:12:00"), before, None) == (52.0, 5.0)
    assert placeguess.guess(_t("2024-05-01T10:30:00"), before, None) is None
    # two far-apart places (a drive): only the one close in time counts
    far = (_t("2024-05-01T10:40:00"), 52.5, 5.5)
    assert placeguess.guess(_t("2024-05-01T10:05:00"), before, far) == (52.0, 5.0)
    assert placeguess.guess(_t("2024-05-01T10:20:00"), before, far) is None


def test_photos_without_gps_get_a_guessed_place_kept_out_of_sidecars(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "phone1.jpg", "2024:05:01 10:00:00", gps=(52.09, 5.12))
    make_jpeg(root / "phone2.jpg", "2024:05:01 10:30:00", gps=(52.091, 5.121))
    make_jpeg(root / "camera.jpg", "2024:05:01 10:10:00")
    make_jpeg(root / "later.jpg", "2024:05:01 18:00:00")        # nothing with a place near then
    make_jpeg(root / "mine.jpg", "2024:05:01 10:05:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        # someone removes the place of one by hand first: that is never guessed over
        c.post("/api/assets/details", json={"ids": [ids["mine.jpg"]], "clear_position": True})
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET place_checked = 0")
        c.post("/api/scan")
        wait_for(lambda: c.get(f"/api/assets/{ids['camera.jpg']}").json()["position"] is not None)
        cam = c.get(f"/api/assets/{ids['camera.jpg']}").json()
        assert cam["place_guess"] == "nearby" and abs(cam["position"][0] - 52.0903) < 0.001
        assert c.get(f"/api/assets/{ids['later.jpg']}").json()["position"] is None
        assert c.get(f"/api/assets/{ids['mine.jpg']}").json()["position"] is None
        with db.session(cfg.db_path) as conn:
            meta = xmp.meta_for(conn, ids["camera.jpg"])
            assert meta["lat"] is None and meta["lon"] is None
            assert xmp.meta_for(conn, ids["phone1.jpg"])["lat"] is not None
        # "Not here"
        c.post("/api/assets/details", json={"ids": [ids["camera.jpg"]], "clear_position": True})
        cam = c.get(f"/api/assets/{ids['camera.jpg']}").json()
        assert cam["position"] is None and cam["place_guess"] is None


# ------------------------------------------------------------------ too bright, shut eyes

def test_too_bright_photos_are_flagged_but_not_documents():
    cfg = type("C", (), {"dark_threshold": 30, "blur_threshold": 50, "bright_threshold": 235})()
    assert analysis.quality_label(cfg, 500, 245, 255) == "bright"
    assert analysis.quality_label(cfg, 500, 245, 255, category="document") is None
    assert analysis.quality_label(cfg, 500, 120, 250, eyes_closed=1) == "eyes"
    assert analysis.quality_label(cfg, 500, 120, 250) is None


def test_eye_model_gives_a_chance_and_skips_eyes_at_the_edge():
    rng = np.random.default_rng(0)
    grey = rng.integers(0, 255, size=(200, 200)).astype(np.float32)
    p = eyes.face_shut(grey, [80, 100, 120, 100, 100, 120])
    assert p is not None and 0.0 <= p <= 1.0
    assert eyes.face_shut(grey, [2, 100, 40, 100, 20, 120]) is None        # an eye cut off at the edge
    assert eyes.face_shut(grey, [100, 100, 102, 100, 101, 110]) is None    # too small to tell


def test_shut_eyes_count_only_for_main_faces_looking_at_the_camera():
    # (asset, person, width, shut, frontal, person's usual)
    rows = [
        (1, 7, 0.20, 0.97, 0.9, 0.1),     # shut, frontal, main face: counts
        (2, 7, 0.20, 0.97, 0.3, 0.1),     # turned away
        (3, 7, 0.20, 0.50, 0.9, 0.1),     # open
        (4, 8, 0.20, 0.97, 0.9, 0.8),     # this person's eyes always look like this
        (5, 7, 0.30, 0.10, 0.9, 0.1),
        (5, 9, 0.05, 0.99, 0.9, None),    # someone small in the background
        (6, None, 0.2, 0.95, 0.8, None),  # nobody we know: still counts
    ]
    assert eyes.shut_photos(rows) == {1, 6}


def test_eyes_job_marks_photos_and_clean_up_lists_them(tmp_path):
    root = tmp_path / "Photos"
    for k in range(3):
        make_jpeg(root / f"p{k}.jpg", f"2024:01:0{k + 1} 10:00:00", color=(120, 110, 100))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        def measured():
            with db.session(cfg.db_path) as conn:
                return conn.execute("SELECT COUNT(*) FROM assets WHERE quality_done = 0").fetchone()[0] == 0
        wait_for(measured, timeout=60)      # or the worker would overwrite the numbers set next
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET sharpness = 999, brightness = 120, highlights = 200")   # sharp, not dark
            conn.execute("INSERT INTO people (id, name, created_at) VALUES (1, 'Anna', 't')")
            for name, shut in (("p0.jpg", 0.98), ("p1.jpg", 0.05)):
                conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at,"
                             " frontal, shut) VALUES (?, 1, 0.3, 0.3, 0.2, 0.2, 1, x'00', 't', 0.9, ?)",
                             (ids[name], shut))
        job = eyes.EyesJob(cfg)
        with db.session(cfg.db_path) as conn:
            assert job.run_batch(conn, 0, lambda: False)
            assert not job.run_batch(conn, 0, lambda: False)      # nothing changed: nothing to do
        assert c.get(f"/api/assets/{ids['p0.jpg']}").json()["quality"] == "eyes"
        assert c.get(f"/api/assets/{ids['p1.jpg']}").json()["quality"] is None
        assert c.get("/api/cleanup").json()["low_quality"] == 1


# ------------------------------------------------------------------ captions

def test_captions_are_tidied_and_phrases_not_repeated():
    assert captions.tidy("  a dog   on a beach ") == "A dog on a beach."
    assert captions.tidy("") is None
    assert captions.tidy("Is it a cat?") == "Is it a cat?"
    assert captions.no_repeat([5, 6, 7, 5, 6]) == {7}
    assert captions.no_repeat([5, 6]) == set()


class FakeCaptions:
    def caption(self, img):
        r, g, b = img.convert("RGB").resize((1, 1)).getpixel((0, 0))
        return "A red motorcycle in a garage." if r > b else "A blue boat on a lake."


def test_captions_are_written_searchable_and_redone_after_edits(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    make_jpeg(root / "red.jpg", "2024:01:01 10:00:00", color=(220, 30, 30))
    make_jpeg(root / "blue.jpg", "2024:01:02 10:00:00", color=(30, 30, 220))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        job = captions.CaptionJob(cfg)
        monkeypatch.setattr(job, "load_engine", lambda: FakeCaptions())
        with db.session(cfg.db_path) as conn:
            assert not job.run_batch(conn, 0, lambda: False)      # switched off
            cfg.captions = True
            assert job.run_batch(conn, 0, lambda: False)
        assert c.get(f"/api/assets/{ids['red.jpg']}").json()["caption"] == "A red motorcycle in a garage."
        found = {i["id"] for i in c.get("/api/assets", params={"q": "motorcycle"}).json()["items"]}
        assert found == {ids["red.jpg"]}
        # an edit changes the picture: it is described again
        c.post(f"/api/assets/{ids['red.jpg']}/edits", json={"edits": {"rotate": 90}})
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT caption_done FROM assets WHERE id = ?", (ids["red.jpg"],)).fetchone()[0] == 0


def test_captions_switch_in_settings(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root, config_file=tmp_path / "config.toml")
    with c:
        setup_admin(c)
        r = c.put("/api/settings/sorting", json={"captions": True, "guess_places": False})
        assert r.status_code == 200 and r.json()["captions"] is True and r.json()["guess_places"] is False
        assert cfg.captions and not cfg.guess_places
        assert "captions = true" in (tmp_path / "config.toml").read_text()


# ------------------------------------------------------------------ landmarks

class PlaceEngine:
    """Stands in for CLIP: a picture's colour picks an axis; each landmark name has its own axis."""

    def __init__(self, names):
        self.axes = {n: k for k, n in enumerate(names)}

    @staticmethod
    def _unit(v):
        v = np.asarray(v, np.float32)
        return v / np.linalg.norm(v)

    def embed_text(self, text):
        v = np.full(512, 0.001)
        for name, k in self.axes.items():
            if name in text:
                v[k] = 1.0
                return self._unit(v)
        v[500] = 1.0                              # every plain description
        return self._unit(v)

    def vec_for(self, name, clear=1.0):
        v = np.full(512, 0.001)
        v[self.axes[name]] = clear
        v[500] = 1.0 - clear
        return self._unit(v)


SMALL_LIST = [
    ("Eiffel Tower", "Paris", "France", 48.8584, 2.2945, 5),
    ("Arc de Triomphe", "Paris", "France", 48.8738, 2.2950, 2),
    ("Grand Canyon", "Arizona", "United States", 36.1069, -112.1129, 60),
]


def test_landmarks_near_a_position_need_less_certainty_than_without_one():
    eng = PlaceEngine([e[0] for e in SMALL_LIST])
    r = landmarks.Recogniser(eng, SMALL_LIST)
    assert r.near(48.86, 2.30) == [0, 1]
    clear = eng.vec_for("Eiffel Tower")
    assert r.recognise(clear, (48.86, 2.30))[0] == "Eiffel Tower"
    assert r.recognise(clear, None)[0] == "Eiffel Tower"
    assert r.recognise(clear, (36.1, -112.1)) is None                   # it can't be there
    # a fair match: enough next to it, not without a position (more places to choose from)
    fair = next(v for v in (eng.vec_for("Eiffel Tower", c) for c in np.linspace(0.40, 0.60, 81))
                if 0.65 <= r.shares(v, [0, 1])[0] and r.shares(v, [0, 1, 2])[0] < landmarks.FAR_SHARE)
    assert r.recognise(fair, (48.86, 2.30))[0] == "Eiffel Tower"
    assert r.recognise(fair, None) is None
    plain = eng._unit(np.eye(512)[500] + 0.001)
    assert r.recognise(plain, None) is None


def test_landmark_job_names_photos_and_places_those_without_gps(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    make_jpeg(root / "gps.jpg", "2024:06:01 10:00:00", gps=(48.858, 2.294))
    make_jpeg(root / "nogps.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "handmade.jpg", "2024:02:01 10:00:00")
    c, cfg = new_client(tmp_path, root, guess_places=False)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        c.post("/api/assets/details", json={"ids": [ids["handmade.jpg"]], "clear_position": True})
        eng = PlaceEngine([e[0] for e in SMALL_LIST])
        with db.session(cfg.db_path) as conn:
            for name in ids:
                conn.execute("INSERT INTO clip_vectors (asset_id, vec) VALUES (?, ?)",
                             (ids[name], eng.vec_for("Eiffel Tower").astype(np.float16).tobytes()))
        clip = type("Clip", (), {"ready": lambda self: True, "load_engine": lambda self: eng})()
        monkeypatch.setattr(landmarks, "LANDMARKS", SMALL_LIST)
        job = landmarks.LandmarkJob(cfg, clip)
        job.recogniser = landmarks.Recogniser(eng, SMALL_LIST)
        cfg.smart_search, cfg.guess_places = True, True
        with db.session(cfg.db_path) as conn:
            assert job.run_batch(conn, 0, lambda: False)
        got = {n: c.get(f"/api/assets/{ids[n]}").json() for n in ids}
        assert all(g["landmark"] == "Eiffel Tower" for g in got.values())
        assert got["gps.jpg"]["place_guess"] is None and abs(got["gps.jpg"]["position"][0] - 48.858) < 1e-3
        assert got["nogps.jpg"]["place_guess"] == "landmark" and abs(got["nogps.jpg"]["position"][0] - 48.8584) < 1e-4
        assert got["handmade.jpg"]["position"] is None                  # removed by hand: stays removed
        found = {i["id"] for i in c.get("/api/assets", params={"q": "eiffel"}).json()["items"]}
        assert found == set(ids.values())


# ------------------------------------------------------------------ event titles, who was there

def test_an_event_is_about_an_occasion_when_enough_photos_are():
    beach = 0
    assert eventnames.theme_of([beach] * 4 + [eventnames.NONE] * 8) == beach
    assert eventnames.theme_of([beach] * 2 + [eventnames.NONE] * 2) is None        # too few
    assert eventnames.theme_of([beach] * 3 + [eventnames.NONE] * 20) is None       # too small a share
    assert eventnames.theme_of([]) is None


def test_birthdays_near_an_event():
    assert eventnames.near_birthday("1990-05-03", "2024-05-04T10:00:00", "2024-05-04T18:00:00")
    assert eventnames.near_birthday("--05-03", "2024-05-01T10:00:00", "2024-05-01T18:00:00")
    assert not eventnames.near_birthday("--05-03", "2024-05-20T10:00:00", "2024-05-20T18:00:00")
    assert eventnames.near_birthday("--02-29", "2023-03-01T10:00:00", "2023-03-01T12:00:00")
    assert not eventnames.near_birthday(None, "2024-05-04T10:00:00", "2024-05-04T18:00:00")


def _theme(key):
    return next(i for i, t in enumerate(eventnames.THEMES) if t[0] == key)


def test_event_titles():
    trip = {"kind": "trip", "place": "Rosarito", "start": "2024-07-01T10:00:00", "end": "2024-07-04T10:00:00"}
    day = {"kind": "day", "place": "Phoenix", "start": "2024-05-04T10:00:00", "end": "2024-05-04T18:00:00"}
    assert eventnames.title(trip, _theme("beach"), []) == "Beach trip to Rosarito"
    assert eventnames.title(trip, None, []) == "Trip to Rosarito"
    assert eventnames.title(dict(trip, place=None), _theme("wedding"), []) == "Wedding"
    assert eventnames.title(trip, _theme("wedding"), []) == "Wedding in Rosarito"
    assert eventnames.title(day, _theme("beach"), []) == "Beach day in Phoenix"
    assert eventnames.title(day, None, []) is None
    anna = {"id": 1, "name": "Anna", "birthday": "--05-04"}
    assert eventnames.title(day, _theme("birthday"), [anna]) == "Anna's birthday"
    assert eventnames.title(day, _theme("birthday"), [dict(anna, birthday="--09-09")]) == "Birthday"
    assert eventnames.title(day, _theme("christmas"), []) == "Christmas"


def _add_faces(cfg, ids_names, person_id, name, birthday=None):
    with db.session(cfg.db_path) as conn:
        conn.execute("INSERT INTO people (id, name, birthday, created_at) VALUES (?, ?, ?, 't')",
                     (person_id, name, birthday))
        for aid in ids_names:
            conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                         " VALUES (?, ?, 0.3, 0.3, 0.2, 0.2, 1, x'00', 't')", (aid, person_id))


def test_events_say_who_was_there_and_people_pages_show_who_they_are_with(tmp_path):
    root = tmp_path / "Photos"
    start = datetime(2024, 5, 4, 10)
    for k in range(10):
        make_jpeg(root / "Party" / f"p{k}.jpg", (start + timedelta(minutes=10 * k)).strftime("%Y:%m:%d %H:%M:%S"),
                  color=(20 * k, 100, 50))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        party = sorted(ids.values())
        _add_faces(cfg, party[:6], 1, "Anna", "--05-04")
        _add_faces(cfg, party[:3], 2, "Ben")
        _add_faces(cfg, party[:1], 3, "Cleo")
        (event,) = c.get("/api/events").json()["events"]
        assert [p["name"] for p in event["people"]] == ["Anna", "Ben", "Cleo"]   # small event: one photo is enough
        assert "ids" not in event and all("birthday" not in p for p in event["people"])
        together = c.get("/api/people/1/together").json()
        assert [(p["name"], p["together"]) for p in together["people"]] == [("Ben", 3), ("Cleo", 1)]
        assert len(together["events"]) == 1
        assert c.get("/api/people/999/together").status_code == 404


# ------------------------------------------------------------------ birthdays and earlier trips

def test_birthdays_are_checked_and_shown_on_the_day(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2020:03:10 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        (aid,) = ids_by_name(c).values()
        _add_faces(cfg, [aid], 1, "Anna")
        assert c.patch("/api/people/1", json={"birthday": "1990-02-30"}).status_code == 400
        assert c.patch("/api/people/1", json={"birthday": "2999-01-01"}).status_code == 400
        assert c.patch("/api/people/1", json={"birthday": "05/03/1990"}).status_code == 422
        assert c.patch("/api/people/1", json={"birthday": "--02-29"}).json()["birthday"] == "--02-29"
        assert c.patch("/api/people/1", json={"birthday": "1990-06-15"}).json()["birthday"] == "1990-06-15"
        m = c.get("/api/memories", params={"today": "2025-06-15"}).json()
        assert [(b["name"], b["age"], b["cover"]) for b in m["birthdays"]] == [("Anna", 35, aid)]
        assert c.get("/api/memories", params={"today": "2025-06-16"}).json()["birthdays"] == []
        # without a year: no age
        c.patch("/api/people/1", json={"birthday": "--06-15"})
        assert c.get("/api/memories", params={"today": "2025-06-15"}).json()["birthdays"][0]["age"] is None
        # 29 February is remembered on 28 February in other years
        c.patch("/api/people/1", json={"birthday": "--02-29"})
        assert len(c.get("/api/memories", params={"today": "2025-02-28"}).json()["birthdays"]) == 1
        assert c.get("/api/memories", params={"today": "2024-02-28"}).json()["birthdays"] == []
        assert c.patch("/api/people/1", json={"birthday": ""}).json()["birthday"] is None


def test_trips_from_earlier_years_show_around_their_dates(tmp_path):
    root = tmp_path / "Photos"
    start = datetime(2022, 7, 10, 9)
    for k in range(12):
        make_jpeg(root / "Trip" / f"t{k}.jpg", (start + timedelta(hours=5 * k)).strftime("%Y:%m:%d %H:%M:%S"),
                  color=(10 * k, 60, 90))
    for k in range(30):                     # home: a photo a week through the year
        make_jpeg(root / "Home" / f"h{k}.jpg", (datetime(2022, 1, 3, 12) + timedelta(days=7 * k)).strftime(
            "%Y:%m:%d %H:%M:%S"), color=(90, 10 * (k % 20), 30))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET city = 'Phoenix', country = 'United States' WHERE path LIKE 'Home/%'")
            conn.execute("UPDATE assets SET city = 'Rosarito', country = 'Mexico' WHERE path LIKE 'Trip/%'")
        trips = c.get("/api/memories", params={"today": "2025-07-11"}).json()["trips"]
        assert [(t["title"], t["years_ago"]) for t in trips] == [("Trip to Rosarito", 3)]
        assert c.get("/api/memories", params={"today": "2025-08-20"}).json()["trips"] == []
        assert c.get("/api/memories", params={"today": "2022-07-11"}).json()["trips"] == []   # not this year's


def test_locked_photos_lend_no_place_and_a_new_date_drops_a_guess(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "secret.jpg", "2024:05:01 10:00:00", gps=(48.1, 11.5))
    make_jpeg(root / "phone.jpg", "2024:05:02 10:00:00", gps=(52.09, 5.12))
    make_jpeg(root / "camera.jpg", "2024:05:01 10:05:00")
    make_jpeg(root / "shot.jpg", "2024:05:02 10:03:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET locked_by = 1 WHERE id = ?", (ids["secret.jpg"],))
            conn.execute("UPDATE assets SET place_checked = 0, lat = NULL, lon = NULL, place_guess = NULL"
                         " WHERE id IN (?, ?)", (ids["camera.jpg"], ids["shot.jpg"]))
        c.post("/api/scan")
        wait_for(lambda: c.get(f"/api/assets/{ids['shot.jpg']}").json()["position"] is not None)
        assert c.get(f"/api/assets/{ids['camera.jpg']}").json()["position"] is None    # not from the locked one
        # a new date for a photo with a guessed place: the guess goes, and is made again for the new time
        c.post("/api/assets/details", json={"ids": [ids["shot.jpg"]], "taken_at": "2024-05-01T10:01"})
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT lat, place_guess, place_checked FROM assets WHERE id = ?",
                               (ids["shot.jpg"],)).fetchone()
        assert row["place_guess"] is None or row["lat"] is None or row["lat"] < 50
        # a screenshot never keeps a guessed place
        c.post("/api/assets/details", json={"ids": [ids["shot.jpg"]], "taken_at": "2024-05-02T10:03"})
        wait_for(lambda: c.get(f"/api/assets/{ids['shot.jpg']}").json()["place_guess"] == "nearby")
        c.post("/api/assets/category", json={"ids": [ids["shot.jpg"]], "category": "screenshot"})
        c.post("/api/scan")
        wait_for(lambda: c.get(f"/api/assets/{ids['shot.jpg']}").json()["position"] is None)


def test_a_trip_over_new_year_is_remembered(tmp_path):
    root = tmp_path / "Photos"
    start = datetime(2023, 12, 30, 9)
    for k in range(12):
        make_jpeg(root / "Trip" / f"t{k}.jpg", (start + timedelta(hours=7 * k)).strftime("%Y:%m:%d %H:%M:%S"),
                  color=(10 * k, 60, 90))
    for k in range(30):
        make_jpeg(root / "Home" / f"h{k}.jpg", (datetime(2023, 1, 3, 12) + timedelta(days=7 * k)).strftime(
            "%Y:%m:%d %H:%M:%S"), color=(90, 10 * (k % 20), 30))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE assets SET city = 'Phoenix', country = 'United States' WHERE path LIKE 'Home/%'")
            conn.execute("UPDATE assets SET city = 'Flagstaff', country = 'United States' WHERE path LIKE 'Trip/%'")
        trips = c.get("/api/memories", params={"today": "2026-01-01"}).json()["trips"]
        assert [t["title"] for t in trips] == ["Trip to Flagstaff"]
