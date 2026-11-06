"""JPEG XL photos: read through Pillow, with their EXIF, the right way up."""

import io
import struct

import numpy as np
import pytest
from PIL import Image

from helpers import ids_by_name, new_client, setup_admin, wait_scan

from snapshots import jxl, media


def _box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I", 8 + len(payload)) + kind + payload


def _exif_tiff(orientation=6) -> bytes:
    exif = Image.Exif()
    exif[306] = "2023:07:04 18:30:00"          # DateTime
    exif[271], exif[272] = "Fujifilm", "X-T5"
    exif[274] = orientation
    return exif.tobytes()[6:]                   # without the "Exif\0\0" marker


def _container(tiff: bytes) -> bytes:
    return (jxl.CONTAINER + _box(b"ftyp", b"jxl \x00\x00\x00\x00jxl ")
            + _box(b"Exif", b"\x00\x00\x00\x00" + tiff) + _box(b"jxlc", b"\xff\x0a" + b"\x00" * 16))


def test_exif_is_found_and_set_upright():
    tiff = _exif_tiff(orientation=6)
    found = jxl.exif_from_container(_container(tiff))
    exif = Image.Exif()
    exif.load(found)
    assert exif[306] == "2023:07:04 18:30:00" and exif[272] == "X-T5"
    assert exif[274] == 1                        # libjxl turned the picture already
    assert jxl.exif_from_container(b"\xff\x0a" + b"\x00" * 20) is None      # a bare picture has none
    assert jxl.exif_from_container(jxl.CONTAINER + b"\x00\x00\x00\x02junk") is None   # damaged: no crash


def test_jxl_files_open_like_any_photo(tmp_path, monkeypatch):
    monkeypatch.setattr(jxl, "AVAILABLE", True)
    picture = np.zeros((30, 40, 3), np.uint8)
    picture[:, :, 0] = 200
    monkeypatch.setattr(jxl, "_decode_pixels", lambda data: picture)
    path = tmp_path / "x.jxl"
    path.write_bytes(_container(_exif_tiff(orientation=6)))
    info = media.read_photo_info(path)
    assert (info["width"], info["height"]) == (40, 30)             # not swapped by the old orientation
    assert info["taken"].year == 2023 and "X-T5" in (info["camera"] or "")
    img = media.load_oriented(path, 480)
    assert img.size == (40, 30) and img.getpixel((0, 0))[0] == 200
    # 16-bit and grey pictures become ordinary 8-bit ones.
    monkeypatch.setattr(jxl, "_decode_pixels", lambda data: np.full((10, 12), 65535, np.uint16))
    with Image.open(path) as img:
        img.load()
        assert img.mode == "L" and img.getpixel((0, 0)) == 255


@pytest.mark.skipif(not jxl.AVAILABLE, reason="imagecodecs with JPEG XL is not installed")
def test_a_real_jxl_file_is_indexed_with_a_thumbnail(tmp_path):
    import imagecodecs

    root = tmp_path / "Photos"
    root.mkdir()
    picture = np.zeros((300, 400, 3), np.uint8)
    picture[:, :200] = (250, 40, 40)
    (root / "photo.jxl").write_bytes(imagecodecs.jpegxl_encode(picture))
    c, cfg = new_client(tmp_path, root)
    with c:
        setup_admin(c)
        wait_scan(c)
        jid = ids_by_name(c)["photo.jxl"]
        thumb = c.get(f"/api/assets/{jid}/thumb")
        assert thumb.status_code == 200
        with Image.open(io.BytesIO(thumb.content)) as img:
            assert img.size == (400, 300) and img.getpixel((10, 10))[0] > 200
        view = c.get(f"/api/assets/{jid}/view")      # the app window gets a JPEG it can show
        assert view.status_code == 200 and view.headers["content-type"] == "image/jpeg"
        assert c.get(f"/api/assets/{jid}/original").content[:2] == b"\xff\x0a"
