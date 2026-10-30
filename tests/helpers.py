"""Shared helpers for the tests."""

import io
import time
from pathlib import Path

from fastapi.testclient import TestClient
from PIL import Image

from snapshots.app import create_app
from snapshots.config import Config

ADMIN = ("admin", "correct horse battery")


def make_jpeg(path: Path, taken: str | None = None, size=(640, 480), color=(200, 80, 60), camera=None,
              gps: tuple[float, float] | None = None, image=None, quality: int = 90):
    """A JPEG with EXIF date, camera and GPS. `image` (a PIL image) is saved instead of a plain colour."""
    path.parent.mkdir(parents=True, exist_ok=True)
    img = image.convert("RGB") if image is not None else Image.new("RGB", size, color)
    exif = Image.Exif()
    if taken:
        exif.get_ifd(0x8769)[36867] = taken  # DateTimeOriginal
    if camera:
        exif[271], exif[272] = camera
    if gps:
        lat, lon = gps

        def dms(value):
            value = abs(value)
            d = int(value)
            m = int((value - d) * 60)
            s = round(((value - d) * 60 - m) * 60, 4)
            return (float(d), float(m), s)

        g = exif.get_ifd(0x8825)
        g[1], g[2] = ("N" if lat >= 0 else "S"), dms(lat)
        g[3], g[4] = ("E" if lon >= 0 else "W"), dms(lon)
    img.save(path, "JPEG", exif=exif, quality=quality)


def jpeg_bytes(color=(1, 2, 3)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (64, 64), color).save(buf, "JPEG")
    return buf.getvalue()


def new_client(tmp_path: Path, root: Path, **kw):
    """A test client for a fresh install. Faces, places, content and text search and timed scans are
    off unless asked for (they need downloads); the photo checks are on."""
    kw.setdefault("faces", False)
    kw.setdefault("places", False)
    kw.setdefault("smart_search", False)
    kw.setdefault("text_search", False)
    kw.setdefault("scan_interval_minutes", 0)
    kw.setdefault("data_dir", tmp_path / "data")
    cfg = Config(library_path=root, **kw)
    client = TestClient(create_app(cfg))
    client.headers["X-Snapshots-Request"] = "1"
    return client, cfg


def setup_admin(client, username=ADMIN[0], password=ADMIN[1]):
    r = client.post("/api/auth/setup", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def login(client, username, password):
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()


def wait_scan(client, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = client.get("/api/status").json()["scan"]
        if not s["running"] and s["finished_at"]:
            return s
        time.sleep(0.05)
    raise AssertionError("scan did not finish")


def rescan(client):
    assert client.post("/api/scan").status_code == 202
    time.sleep(0.05)
    return wait_scan(client)


def wait_for(predicate, timeout=30, interval=0.05):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError("condition not met in time")


def ids_by_name(client, **params) -> dict:
    return {i["filename"]: i["id"] for i in client.get("/api/assets", params=params).json()["items"]}
