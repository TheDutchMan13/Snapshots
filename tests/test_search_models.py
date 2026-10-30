"""Search by content (CLIP), text in photos (OCR), regrouping faces, and model downloads.

The real models are large, so tests that need them run only when given a folder of model files:
    SNAPSHOTS_TEST_MODELS=/path/to/models   with clip/ and ocr/ inside, named as in clipsearch.FILES
                                            and ocr.FILES (the face models sit directly in the folder)
"""

import hashlib
import os
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw, ImageFont

from snapshots import clipsearch, db, downloads, ocr
from snapshots.faces import regroup
from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan

MODELS = os.environ.get("SNAPSHOTS_TEST_MODELS")
needs_models = pytest.mark.skipif(not MODELS, reason="set SNAPSHOTS_TEST_MODELS to a folder with the models")


# ------------------------------------------------------------------ downloads

def test_downloads_are_checked(tmp_path, monkeypatch):
    import importlib

    real = importlib.reload(downloads)  # the conftest guard replaced ensure_files; test the real one
    src = tmp_path / "server" / "model.bin"
    src.parent.mkdir()
    src.write_bytes(b"the real model")
    good = hashlib.sha256(b"the real model").hexdigest()
    files = {"model.bin": {"sha256": good, "urls": [src.as_uri()]}}
    paths = real.ensure_files(tmp_path / "models", files)
    assert paths["model.bin"].read_bytes() == b"the real model"
    # a tampered download is refused and nothing is kept
    bad = {"other.bin": {"sha256": good, "urls": [src.with_name("x").as_uri()]}}
    src.with_name("x").write_bytes(b"something else")
    with pytest.raises(RuntimeError):
        real.ensure_files(tmp_path / "models", bad)
    assert not (tmp_path / "models" / "other.bin").exists()
    assert real.missing(tmp_path / "models", {**files, **bad}) == ["other.bin"]


def test_model_files_have_pinned_checksums():
    for spec in list(clipsearch.FILES.values()) + list(ocr.FILES.values()):
        assert len(spec["sha256"]) == 64 and spec["urls"]
        assert all(u.startswith("https://huggingface.co/") for u in spec["urls"])


# ------------------------------------------------------------------ face regrouping

def _faces_db(tmp_path, groups):
    """groups: {person_id: (name or None, hidden, [embeddings])}."""
    path = tmp_path / "f.db"
    db.init(path)
    conn = db.connect(path)
    conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                 " VALUES (1, 'a', 'a', 'photo', 1, 1, 'x', '2020-01-01T00:00:00', 'x')")
    for pid, (name, hidden, embs) in groups.items():
        conn.execute("INSERT INTO people (id, name, hidden, created_at) VALUES (?, ?, ?, 'x')", (pid, name, hidden))
        for e in embs:
            conn.execute("INSERT INTO faces (asset_id, person_id, x, y, w, h, score, embedding, created_at)"
                         " VALUES (1, ?, 0, 0, 1, 1, 1, ?, 'x')", (pid, e.astype(np.float32).tobytes()))
    conn.commit()
    return conn


def _person(rng, centre, n, noise=0.35):
    out = []
    for _ in range(n):
        v = centre + noise * rng.standard_normal(128) / np.sqrt(128)
        out.append(v / np.linalg.norm(v))
    return out


def test_regroup_merges_a_split_person_but_not_strangers(tmp_path):
    rng = np.random.default_rng(1)
    a = rng.standard_normal(128); a /= np.linalg.norm(a)
    b = rng.standard_normal(128); b /= np.linalg.norm(b)
    conn = _faces_db(tmp_path, {
        1: (None, 0, _person(rng, a, 6)),
        2: (None, 0, _person(rng, a, 4)),      # the same person, split off
        3: (None, 0, _person(rng, b, 5)),      # someone else
        4: (None, 0, _person(rng, a, 1)),      # a single face: left alone
    })
    assert regroup(conn, 0.42) == 1
    people = dict(conn.execute("SELECT person_id, COUNT(*) FROM faces GROUP BY person_id").fetchall())
    assert people == {1: 10, 3: 5, 4: 1}
    assert regroup(conn, 0) == 0  # turned off


def test_regroup_respects_names_and_hidden_people(tmp_path):
    rng = np.random.default_rng(2)
    a = rng.standard_normal(128); a /= np.linalg.norm(a)
    conn = _faces_db(tmp_path, {
        1: ("Grandma", 0, _person(rng, a, 3)),
        2: ("Nana", 0, _person(rng, a, 3)),     # two names: the person decided they are different
        3: (None, 0, _person(rng, a, 5)),       # unnamed: may join a named group
        4: (None, 1, _person(rng, a, 5)),       # hidden: never touched
    })
    assert regroup(conn, 0.42) == 1
    owner = conn.execute("SELECT DISTINCT person_id FROM faces WHERE person_id NOT IN (2, 4)").fetchall()
    assert len(owner) == 1 and owner[0][0] in (1,)  # the unnamed group joined Grandma, keeping her name
    assert conn.execute("SELECT name FROM people WHERE id = 1").fetchone()[0] == "Grandma"
    assert conn.execute("SELECT COUNT(*) FROM faces WHERE person_id = 4").fetchone()[0] == 5


# ------------------------------------------------------------------ search by content, without the model

def test_vector_index_ranks_and_cuts_off(tmp_path):
    conn = _faces_db(tmp_path, {})
    rng = np.random.default_rng(3)
    query = rng.standard_normal(512); query /= np.linalg.norm(query)
    rows = []
    for i in range(1, 201):
        if i > 1:  # photo 1 is already there
            conn.execute("INSERT INTO assets (id, path, filename, kind, size, mtime_ns, sha1, taken_at, added_at)"
                         " VALUES (?, ?, ?, 'photo', 1, 1, 'x', '2020-01-01T00:00:00', 'x')", (i, f"p{i}", f"p{i}"))
        v = rng.standard_normal(512)
        v = v / np.linalg.norm(v)
        if i <= 5:  # five photos that clearly show it
            v = 0.3 * query + v * 0.1
            v /= np.linalg.norm(v)
        rows.append((i, v.astype(np.float16).tobytes()))
    conn.executemany("INSERT INTO clip_vectors (asset_id, vec) VALUES (?, ?)", rows)
    conn.commit()
    found = clipsearch.VectorIndex().search(conn, query)
    assert [i for i, _ in found] == sorted([i for i, _ in found], key=lambda i: -dict(found)[i])
    assert {i for i, _ in found} == {1, 2, 3, 4, 5}


def test_search_tabs_without_the_model(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "beach-2024.jpg", "2024:07:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        s = c.get("/api/search", params={"q": "beach"}).json()
        assert (s["words"], s["content"], s["content_enabled"]) == (1, 0, False)
        assert c.get("/api/assets", params={"q": "beach", "look": True}).json() == {"items": [], "next": None}


class FakeEngine:
    """Stands in for CLIP: pictures and words are compared by their main colour."""

    COLOURS = {"red": (1, 0, 0), "green": (0, 1, 0), "blue": (0, 0, 1)}

    def _unit(self, v):
        v = np.asarray(v, np.float32)
        return v / np.linalg.norm(v)

    def embed_image(self, img):
        r, g, b = np.asarray(img.convert("RGB").resize((8, 8)), np.float32).reshape(-1, 3).mean(0) / 255
        return self._unit(np.concatenate([[r, g, b], np.full(509, 0.02)]))

    def embed_text(self, text):
        c = self.COLOURS.get(text.split()[-1], (0.3, 0.3, 0.3))
        return self._unit(np.concatenate([c, np.full(509, 0.02)]))


def test_search_by_content_through_the_app(tmp_path, monkeypatch):
    root = tmp_path / "Photos"
    for k, colour in enumerate([(220, 20, 20)] * 3 + [(20, 200, 20)] * 3 + [(20, 20, 220)] * 30):
        make_jpeg(root / f"p{k:02d}.jpg", f"2024:05:{k % 28 + 1:02d} 10:00:00", color=colour)
    c, cfg = new_client(tmp_path, root, smart_search=True)
    monkeypatch.setattr(c.app.state.ctx.worker.clip, "load_engine", lambda: FakeEngine())
    monkeypatch.setattr(clipsearch.ClipJob, "ready", lambda self: True)
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/status").json()["content"]["pending"] == 0
                 and db.connect(cfg.db_path).execute("SELECT COUNT(*) FROM clip_vectors").fetchone()[0] == 36)
        s = c.get("/api/search", params={"q": "something red"}).json()
        assert s["words"] == 0 and s["content"] >= 3
        items = c.get("/api/assets", params={"q": "something red", "look": True}).json()["items"]
        assert {i["filename"] for i in items[:3]} == {"p00.jpg", "p01.jpg", "p02.jpg"}
        assert items == sorted(items, key=lambda i: -i["score"])
        # the rest of the filter still applies: only favourites, and one red photo is a favourite
        fav = ids_by_name(c)["p01.jpg"]
        c.post("/api/favorites", json={"ids": [fav]})
        favs = c.get("/api/assets", params={"q": "something red", "look": True, "favorite": True}).json()["items"]
        assert [i["id"] for i in favs] == [fav]


# ------------------------------------------------------------------ text in photos, without the model

def test_text_cleanup_helpers():
    assert ocr._fix_common_mistakes("EXIT 278 FIagstaff") == "EXIT 278 Flagstaff"
    assert ocr._fix_common_mistakes("McIntosh MIAMI Il Forno") == "McIntosh MIAMI Il Forno"
    assert ocr._worth_keeping("Route 66") and not ocr._worth_keeping("—") and not ocr._worth_keeping("a")


def test_text_search_finds_words_read_in_photos(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "IMG_0001.jpg", "2024:05:01 10:00:00")
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        conn = db.connect(cfg.db_path)
        conn.execute("UPDATE assets SET ocr_text = 'WELCOME TO SEDONA', ocr_done = 1")
        conn.commit()
        assert list(ids_by_name(c, q="sedona")) == ["IMG_0001.jpg"]
        assert c.get(f"/api/assets/{ids_by_name(c)['IMG_0001.jpg']}").json()["text"] == "WELCOME TO SEDONA"


# ------------------------------------------------------------------ with the real models

def _engine_paths(sub, files):
    folder = Path(MODELS) / sub
    return {name: folder / name for name in files}


@needs_models
def test_real_text_reading():
    engine = ocr.OcrEngine(_engine_paths("ocr", ocr.FILES))
    img = Image.new("RGB", (1200, 800), (90, 120, 80))
    d = ImageDraw.Draw(img)
    font = ImageFont.truetype(_font(), 64)
    d.rectangle((100, 300, 1100, 420), fill=(250, 250, 240))
    d.text((130, 320), "Petrified Forest Route 66", font=font, fill=(10, 10, 10))
    assert "Petrified Forest Route 66" in engine.text_in(img)


@needs_models
def test_real_content_search():
    engine = clipsearch.ClipEngine(_engine_paths("clip", clipsearch.FILES))
    red = engine.embed_image(Image.new("RGB", (300, 300), (230, 20, 20)))
    blue = engine.embed_image(Image.new("RGB", (300, 300), (20, 20, 230)))
    t = engine.embed_text("a red square")
    assert float(red @ t) > float(blue @ t)
    assert engine.tokenizer.encode("dog on the beach") == [49406, 1929, 525, 518, 2117, 49407]


def _font():
    for f in ("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", r"C:\Windows\Fonts\arialbd.ttf"):
        if Path(f).exists():
            return f
    pytest.skip("no font for drawing text")
