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


# ------------------------------------------------------------------ RAW files and motion photos

def _tiff(entries_by_ifd: list[list[tuple]], data_blobs: dict) -> bytes:
    """A little-endian TIFF built by hand. Each IFD is a list of (tag, type, values); values may be
    bytes (an ASCII or UNDEFINED value), a list of ints, or ('blob', name) / ('ifd', index) to point
    at a data blob or another IFD. Every IFD after the first is reached only through pointers."""
    import struct

    fmt = {1: "B", 3: "H", 4: "I", 13: "I"}
    out = bytearray(b"II*\x00\x00\x00\x00\x00")
    # lay out: IFDs first (fixed size), then their overflow values, then blobs
    ifd_pos, pos = [], 8
    for entries in entries_by_ifd:
        ifd_pos.append(pos)
        pos += 2 + 12 * len(entries) + 4
    extra = bytearray()
    extra_start = pos
    blob_pos = {}
    patches = []  # (offset in out, kind, key)
    body = bytearray()
    for n, entries in enumerate(entries_by_ifd):
        ifd = bytearray(struct.pack("<H", len(entries)))
        for tag, typ, values in sorted(entries, key=lambda e: e[0]):
            if isinstance(values, tuple) and values[0] in ("blob", "ifd"):
                count = len(data_blobs[values[1]]) if values[0] == "blob" and typ in (1, 7) else 1
                ifd += struct.pack("<HHI", tag, typ, count)
                patches.append((ifd_pos[n] + len(ifd), values))
                ifd += b"\0\0\0\0"
                continue
            if isinstance(values, bytes):
                raw, count = values, len(values)
            elif typ in (5, 10):
                raw = b"".join(struct.pack("<ii" if typ == 10 else "<II", a, b) for a, b in values)
                count = len(values)
            else:
                raw = struct.pack("<" + fmt[typ] * len(values), *values)
                count = len(values)
            ifd += struct.pack("<HHI", tag, typ, count)
            if len(raw) <= 4:
                ifd += raw.ljust(4, b"\0")
            else:
                ifd += struct.pack("<I", extra_start + len(extra))
                extra += raw + (b"\0" if len(raw) % 2 else b"")
        ifd += b"\0\0\0\0"
        body += ifd
    out += body + extra
    for name, blob in data_blobs.items():
        blob_pos[name] = len(out)
        out += blob
    for at, (kind, key) in patches:
        value = blob_pos[key] if kind == "blob" else ifd_pos[key]
        out[at:at + 4] = struct.pack("<I", value)
    out[4:8] = struct.pack("<I", ifd_pos[0])
    return bytes(out)


def make_dng(path: Path, taken: str | None = "2024:06:01 12:00:00", size=(96, 64), color=(200, 80, 60),
             preview: bool = True, orientation: int = 1, camera=("Testcam", "Testcam R1")):
    """A small but real DNG: a Bayer mosaic of one colour, with an optional JPEG preview (as cameras
    write them) and EXIF. LibRaw reads it like any camera's RAW file."""
    import numpy as np

    w, h = size
    r, g, b = (int(c) * 64 for c in color)  # 8-bit colour to 14-bit sensor values
    mosaic = np.zeros((h, w), np.uint16)
    mosaic[0::2, 0::2], mosaic[0::2, 1::2], mosaic[1::2, 0::2], mosaic[1::2, 1::2] = r, g, g, b
    blobs = {"raw": mosaic.astype("<u2").tobytes()}
    make, model = camera
    exif_ifd = [(36867, 2, (taken + "\0").encode())] if taken else [(37500, 7, b"none")]
    raw_ifd = [
        (254, 4, [0]), (256, 4, [w]), (257, 4, [h]), (258, 3, [16]), (259, 3, [1]), (262, 3, [32803]),
        (273, 4, ("blob", "raw")), (277, 3, [1]), (278, 4, [h]), (279, 4, [len(blobs["raw"])]),
        (284, 3, [1]), (33421, 3, [2, 2]), (33422, 1, [0, 1, 1, 2]), (50717, 4, [16383]),
    ]
    ifd0 = [
        (271, 2, (make + "\0").encode()), (272, 2, (model + "\0").encode()), (274, 3, [orientation]),
        (34665, 4, ("ifd", 1)), (50706, 1, [1, 4, 0, 0]), (50708, 2, (model + "\0").encode()),
        (50721, 10, [(10000, 10000), (0, 10000), (0, 10000), (0, 10000), (10000, 10000), (0, 10000),
                     (0, 10000), (0, 10000), (10000, 10000)]),
        (50728, 5, [(1, 1), (1, 1), (1, 1)]), (50778, 3, [21]),
    ]
    if preview:
        buf = io.BytesIO()
        Image.new("RGB", (w, h), color).save(buf, "JPEG", quality=90)
        blobs["preview"] = buf.getvalue()
        ifd0 += [(254, 4, [1]), (256, 4, [w]), (257, 4, [h]), (258, 3, [8, 8, 8]), (259, 3, [7]),
                 (262, 3, [6]), (273, 4, ("blob", "preview")), (277, 3, [3]), (278, 4, [h]),
                 (279, 4, [len(blobs["preview"])]), (330, 4, ("ifd", 2))]
        ifds = [ifd0, exif_ifd, raw_ifd]
    else:
        ifd0 = [e for e in ifd0] + [e for e in raw_ifd if e[0] not in (271, 272, 274)]
        ifds = [ifd0, exif_ifd]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_tiff(ifds, blobs))


def mp4_bytes(payload: bytes = b"\0" * 64) -> bytes:
    """The box structure of an MP4 (ftyp, moov, mdat): enough to be found inside a motion photo."""
    import struct

    def box(kind, body):
        return struct.pack(">I", len(body) + 8) + kind + body
    return box(b"ftyp", b"isom\0\0\0\0isommp42") + box(b"moov", box(b"mvhd", b"\0" * 100)) + box(b"mdat", payload)


def make_motion_jpeg(path: Path, taken: str = "2024:06:01 12:00:00", style: str = "google", video: bytes | None = None):
    """A JPEG with a video added at the end, as Google ("google", "micro") and Samsung ("samsung") phones save them."""
    video = video or mp4_bytes()
    make_jpeg(path, taken)
    data = path.read_bytes()
    if style in ("google", "micro"):
        if style == "google":
            xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
                   f'<rdf:Description GCamera:MotionPhoto="1" GCamera:MotionPhotoVersion="1"><Container:Directory><rdf:Seq>'
                   f'<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="image/jpeg" Item:Semantic="Primary"/></rdf:li>'
                   f'<rdf:li rdf:parseType="Resource"><Container:Item Item:Mime="video/mp4" Item:Semantic="MotionPhoto" '
                   f'Item:Length="{len(video)}" Item:Padding="0"/></rdf:li></rdf:Seq></Container:Directory>'
                   f'</rdf:Description></rdf:RDF></x:xmpmeta>')
        else:
            xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description GCamera:MicroVideo="1" '
                   f'GCamera:MicroVideoVersion="1" GCamera:MicroVideoOffset="{len(video)}"/></rdf:RDF></x:xmpmeta>')
        payload = b"http://ns.adobe.com/xap/1.0/\0" + xmp.encode()
        import struct
        app1 = b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload
        data = data[:2] + app1 + data[2:] + video
    else:  # samsung
        data = data + b"\0\0\x30\x0a\x10\0\0\0MotionPhoto_Data" + video + b"SEFH....SEFT"
    path.write_bytes(data)
    return len(data) - len(video) - (12 if style == "samsung" else 0), len(video)


def make_keywords_jpeg(path: Path, words: list[str], taken: str = "2024:06:01 12:00:00"):
    """A JPEG with keywords in its XMP, as Lightroom or Windows Photo Gallery write them."""
    import struct
    from html import escape

    make_jpeg(path, taken)
    items = "".join(f"<rdf:li>{escape(w)}</rdf:li>" for w in words)
    xmp = (f'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF><rdf:Description><dc:subject><rdf:Bag>{items}'
           f'</rdf:Bag></dc:subject></rdf:Description></rdf:RDF></x:xmpmeta>')
    payload = b"http://ns.adobe.com/xap/1.0/\0" + xmp.encode()
    data = path.read_bytes()
    path.write_bytes(data[:2] + b"\xff\xe1" + struct.pack(">H", len(payload) + 2) + payload + data[2:])
