"""XMP sidecars: what people add in Snapshots, written to a small .xmp file beside each photo or video.

Lightroom, digiKam, darktable, Immich and many other programs read these files, so descriptions, tags,
people's names, favourites, and changed dates and places are not locked inside Snapshots' database.

* The sidecar is "photo.jpg.xmp" (the name digiKam, darktable and Immich use). For a RAW file that already
  has a Lightroom-style "photo.xmp", that one is used instead.
* The photos themselves are never changed.
* A sidecar is only written for an item Snapshots knows something extra about (a description, tags,
  named people, a favourite, or a date or place changed by hand), or that already has a sidecar.
* An existing sidecar from another program is updated, not replaced: only the fields below are changed,
  everything else in it (darktable's edits, Lightroom's develop settings) stays as it was. Before
  Snapshots first writes to such a file, it takes in the description and tags already in it.
* Items in someone's locked folder never get a sidecar.

Fields: dc:description, dc:subject (tags), Iptc4xmpExt:PersonInImage and lr:hierarchicalSubject
("People|Name") for named people, xmp:Rating 5 for favourites, exif:DateTimeOriginal and
photoshop:DateCreated, exif:GPSLatitude and exif:GPSLongitude.

When a new file arrives with a sidecar already beside it, its description, tags, date and position (when
the file itself has none) and people's names are read in.
"""

import hashlib
import json
import logging
import os
import re
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

from . import libraries, media

log = logging.getLogger("snapshots.xmp")

NS = {
    "x": "adobe:ns:meta/",
    "rdf": "http://www.w3.org/1999/02/22-rdf-syntax-ns#",
    "dc": "http://purl.org/dc/elements/1.1/",
    "xmp": "http://ns.adobe.com/xap/1.0/",
    "exif": "http://ns.adobe.com/exif/1.0/",
    "photoshop": "http://ns.adobe.com/photoshop/1.0/",
    "Iptc4xmpExt": "http://iptc.org/std/Iptc4xmpExt/2008-02-29/",
    "lr": "http://ns.adobe.com/lightroom/1.0/",
    "snapshots": "https://github.com/TheDutchMan13/Snapshots/ns/1.0/",
}
for _prefix, _uri in NS.items():
    ET.register_namespace(_prefix, _uri)

MAX_SIDECAR = 4 * 1024 * 1024
CHECK_SECONDS = 10              # how often to look for changed items


def q(name: str) -> str:
    prefix, local = name.split(":")
    return f"{{{NS[prefix]}}}{local}"


OWNED = ["dc:description", "dc:subject", "Iptc4xmpExt:PersonInImage", "lr:hierarchicalSubject", "xmp:Rating",
         "exif:DateTimeOriginal", "photoshop:DateCreated", "exif:GPSLatitude", "exif:GPSLongitude",
         "snapshots:Favorite", "snapshots:Written"]


# ---------------------------------------------------------------- where

def sidecar_for(full: Path) -> Path:
    """The sidecar to use for a file: an existing Lightroom-style one for RAW files, else name.ext.xmp."""
    if media.is_raw(full):
        short = full.with_suffix(".xmp")
        if short.is_file():
            return short
    return Path(str(full) + ".xmp")


def existing_sidecar(full: Path) -> Path | None:
    for p in ([Path(str(full) + ".xmp")] + ([full.with_suffix(".xmp")] if media.is_raw(full) else [])):
        if p.is_file():
            return p
    return None


# ---------------------------------------------------------------- reading

def _parse(path: Path):
    try:
        if path.stat().st_size > MAX_SIDECAR:
            return None
        data = path.read_bytes()
    except OSError:
        return None
    if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
        return None       # XMP never needs these; refusing them keeps entity tricks out
    try:
        return ET.fromstring(data)
    except ET.ParseError:
        return None


def _descriptions(root):
    return list(root.iter(q("rdf:Description")))


def _values(desc, name: str) -> list[str]:
    """Text values of a property: an attribute, a simple element, or an rdf Bag/Seq/Alt."""
    out = []
    if q(name) in desc.attrib:
        out.append(desc.attrib[q(name)])
    for el in desc.findall(q(name)):
        items = el.findall(f"{q('rdf:Bag')}/{q('rdf:li')}") + el.findall(f"{q('rdf:Seq')}/{q('rdf:li')}") \
            + el.findall(f"{q('rdf:Alt')}/{q('rdf:li')}")
        if items:
            out.extend((li.text or "").strip() for li in items)
        elif el.text and el.text.strip():
            out.append(el.text.strip())
    return [v for v in out if v]


def _date(value: str) -> datetime | None:
    value = value.strip().replace("Z", "")
    m = re.match(r"(\d{4})[-:](\d{2})[-:](\d{2})(?:[T ](\d{2}):(\d{2})(?::(\d{2}))?)?", value)
    if not m:
        return None
    try:
        y, mo, d = int(m[1]), int(m[2]), int(m[3])
        return datetime(y, mo, d, int(m[4] or 0), int(m[5] or 0), int(m[6] or 0))
    except ValueError:
        return None


def _gps(value: str) -> float | None:
    """XMP GPS: "DDD,MM.mmmmR" or "DDD,MM,SSR" (R is N, S, E or W), or a plain decimal number."""
    v = value.strip().upper()
    try:
        return float(v)
    except ValueError:
        pass
    m = re.match(r"^(\d+(?:\.\d+)?),(\d+(?:\.\d+)?)(?:,(\d+(?:\.\d+)?))?([NSEW])$", v)
    if not m:
        return None
    deg = float(m[1]) + float(m[2]) / 60 + (float(m[3]) / 3600 if m[3] else 0)
    return -deg if m[4] in "SW" else deg


def read(path: Path) -> dict:
    """What a sidecar says: description, tags, people, rating, date, lat, lon, and whether Snapshots wrote it."""
    out = {"description": None, "tags": [], "people": [], "rating": None, "taken": None, "lat": None, "lon": None,
           "ours": False}
    root = _parse(path)
    if root is None:
        return out
    people_tags = set()
    for desc in _descriptions(root):
        d = _values(desc, "dc:description")
        if d and not out["description"]:
            out["description"] = d[0][:5000]
        for word in _values(desc, "dc:subject"):
            if word not in out["tags"] and len(word) <= 100:
                out["tags"].append(word)
        for name in _values(desc, "Iptc4xmpExt:PersonInImage"):
            if name not in out["people"] and len(name) <= 200:
                out["people"].append(name)
        for h in _values(desc, "lr:hierarchicalSubject"):
            parts = h.split("|")
            if len(parts) == 2 and parts[0] in ("People", "Persons", "Personen") and parts[1].strip():
                people_tags.add(parts[1].strip())
                if parts[1].strip() not in out["people"]:
                    out["people"].append(parts[1].strip())
        r = _values(desc, "xmp:Rating")
        if r and out["rating"] is None:
            try:
                out["rating"] = int(float(r[0]))
            except ValueError:
                pass
        for key in ("exif:DateTimeOriginal", "photoshop:DateCreated", "xmp:CreateDate"):
            v = _values(desc, key)
            if v and out["taken"] is None:
                out["taken"] = _date(v[0])
        lat, lon = _values(desc, "exif:GPSLatitude"), _values(desc, "exif:GPSLongitude")
        if lat and lon and out["lat"] is None:
            la, lo = _gps(lat[0]), _gps(lon[0])
            if la is not None and lo is not None and -90 <= la <= 90 and -180 <= lo <= 180 and (la or lo):
                out["lat"], out["lon"] = la, lo
        if _values(desc, "snapshots:Written"):
            out["ours"] = True
    # A person's name written as a keyword too is not a tag.
    out["tags"] = [t for t in out["tags"] if t not in people_tags][:50]
    return out


def read_for(full: Path) -> dict | None:
    p = existing_sidecar(full)
    return read(p) if p else None


# ---------------------------------------------------------------- writing

def _gps_text(value: float, pos: str, neg: str) -> str:
    ref = pos if value >= 0 else neg
    value = abs(value)
    deg = int(value)
    minutes = (value - deg) * 60
    return f"{deg},{minutes:.6f}{ref}"


def _bag(parent, name: str, values: list[str], kind: str = "rdf:Bag"):
    el = ET.SubElement(parent, q(name))
    bag = ET.SubElement(el, q(kind))
    for v in values:
        ET.SubElement(bag, q("rdf:li")).text = v


def _set_fields(desc, meta: dict) -> None:
    for name in OWNED:
        desc.attrib.pop(q(name), None)
        for el in desc.findall(q(name)):
            desc.remove(el)
    if meta.get("description"):
        el = ET.SubElement(desc, q("dc:description"))
        alt = ET.SubElement(el, q("rdf:Alt"))
        li = ET.SubElement(alt, q("rdf:li"))
        li.set("{http://www.w3.org/XML/1998/namespace}lang", "x-default")
        li.text = meta["description"]
    if meta.get("tags"):
        _bag(desc, "dc:subject", meta["tags"])
    if meta.get("people"):
        _bag(desc, "Iptc4xmpExt:PersonInImage", meta["people"])
        _bag(desc, "lr:hierarchicalSubject", [f"People|{p}" for p in meta["people"]])
    if meta.get("favorite"):
        ET.SubElement(desc, q("xmp:Rating")).text = "5"
        ET.SubElement(desc, q("snapshots:Favorite")).text = "True"
    elif meta.get("keep_rating") is not None:
        ET.SubElement(desc, q("xmp:Rating")).text = str(meta["keep_rating"])
    if meta.get("taken"):
        ET.SubElement(desc, q("exif:DateTimeOriginal")).text = meta["taken"]
        ET.SubElement(desc, q("photoshop:DateCreated")).text = meta["taken"]
    if meta.get("lat") is not None and meta.get("lon") is not None:
        ET.SubElement(desc, q("exif:GPSLatitude")).text = _gps_text(meta["lat"], "N", "S")
        ET.SubElement(desc, q("exif:GPSLongitude")).text = _gps_text(meta["lon"], "E", "W")
    ET.SubElement(desc, q("snapshots:Written")).text = "True"


def _new_tree():
    root = ET.Element(q("x:xmpmeta"))
    root.set(q("x:xmptk"), "Snapshots")
    rdf = ET.SubElement(root, q("rdf:RDF"))
    desc = ET.SubElement(rdf, q("rdf:Description"))
    desc.set(q("rdf:about"), "")
    return root, desc


def _register_from(path: Path) -> None:
    """Keep the prefixes another program used, so its part of the file stays readable as it was."""
    try:
        for _event, (prefix, uri) in ET.iterparse(str(path), events=("start-ns",)):
            if prefix and prefix not in NS and not re.match(r"ns\d+$", prefix):
                try:
                    ET.register_namespace(prefix, uri)
                except ValueError:
                    pass
    except (ET.ParseError, OSError):
        pass


def write(path: Path, meta: dict) -> None:
    """Write meta into the sidecar at path, keeping whatever else is in an existing one."""
    root = _parse(path) if path.is_file() else None
    if path.is_file() and root is None:
        raise ValueError("the existing sidecar can't be read, so it is left alone")
    if root is None:
        root, desc = _new_tree()
    else:
        _register_from(path)
        if root.tag != q("x:xmpmeta"):           # a bare rdf:RDF: wrap it
            wrapper = ET.Element(q("x:xmpmeta"))
            wrapper.append(root)
            root = wrapper
        descs = _descriptions(root)
        if descs:
            desc = descs[0]
            for other in descs[1:]:               # owned fields live in the first description only
                for name in OWNED:
                    other.attrib.pop(q(name), None)
                    for el in other.findall(q(name)):
                        other.remove(el)
        else:
            rdf = root.find(q("rdf:RDF"))
            if rdf is None:
                rdf = ET.SubElement(root, q("rdf:RDF"))
            desc = ET.SubElement(rdf, q("rdf:Description"))
            desc.set(q("rdf:about"), "")
    _set_fields(desc, meta)
    ET.indent(root, space=" ")
    body = ET.tostring(root, encoding="unicode")
    text = '<?xpacket begin="﻿" id="W5M0MpCehiHzreSzNTczkc9d"?>\n' + body + '\n<?xpacket end="w"?>\n'
    tmp = path.with_name(path.name + ".snapshots-tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


# ---------------------------------------------------------------- what to write for one item

def meta_for(conn, asset_id: int) -> dict | None:
    """What Snapshots knows about an item that belongs in its sidecar."""
    a = conn.execute("SELECT id, path, kind, taken_at, taken_at_file, lat, lon, position_edited, description,"
                     " locked_by, trashed_at FROM assets WHERE id = ?", (asset_id,)).fetchone()
    if a is None:
        return None
    tags = [r[0] for r in conn.execute(
        "SELECT t.name FROM asset_tags x JOIN tags t ON t.id = x.tag_id WHERE x.asset_id = ? ORDER BY t.name",
        (asset_id,))]
    people = [r[0] for r in conn.execute(
        "SELECT DISTINCT p.name FROM faces f JOIN people p ON p.id = f.person_id"
        " WHERE f.asset_id = ? AND p.name IS NOT NULL AND p.hidden = 0 ORDER BY p.name", (asset_id,))]
    favorite = conn.execute("SELECT 1 FROM favorites WHERE asset_id = ? LIMIT 1", (asset_id,)).fetchone() is not None
    return {
        "row": a,
        "description": (a["description"] or "").strip() or None,
        "tags": tags,
        "people": people,
        "favorite": favorite,
        "taken": a["taken_at"],
        "lat": a["lat"], "lon": a["lon"],
        "extra": bool(a["description"] or tags or people or favorite or a["taken_at_file"] or a["position_edited"]),
    }


def _digest(meta: dict) -> str:
    keep = {k: meta.get(k) for k in ("description", "tags", "people", "favorite", "taken", "lat", "lon")}
    return hashlib.sha1(json.dumps(keep, sort_keys=True).encode(), usedforsecurity=False).hexdigest()[:16]


def adopt(conn, asset_id: int, side: dict) -> None:
    """Take in a sidecar another program wrote, before Snapshots first writes to it."""
    from . import linking

    if side.get("description"):
        conn.execute("UPDATE assets SET description = ? WHERE id = ? AND (description IS NULL OR description = '')",
                     (side["description"], asset_id))
    if side.get("tags"):
        linking.add_keywords(conn, asset_id, side["tags"])


def sync_one(cfg, conn, asset_id: int) -> str:
    """Bring one item's sidecar up to date. Returns what happened: written, same, skipped or failed."""
    meta = meta_for(conn, asset_id)
    if meta is None:
        return "skipped"
    row = meta["row"]
    if row["locked_by"] is not None or row["trashed_at"] is not None:
        return "skipped"
    full = libraries.full_path(cfg, row["path"])
    if full is None or not full.is_file():
        return "skipped"
    path = sidecar_for(full)
    exists = path.is_file()
    if not exists and not meta["extra"]:
        return "skipped"
    if exists:
        side = read(path)
        if not side["ours"]:
            adopt(conn, asset_id, side)
            meta = meta_for(conn, asset_id)
            if side.get("rating") is not None and not meta["favorite"]:
                meta["keep_rating"] = side["rating"]   # someone else's star rating stays
    digest = _digest(meta)
    prev = conn.execute("SELECT xmp_hash FROM assets WHERE id = ?", (asset_id,)).fetchone()[0]
    if exists and prev == digest:
        return "same"
    write(path, meta)
    conn.execute("UPDATE assets SET xmp_hash = ? WHERE id = ?", (digest, asset_id))
    return "written"


class XmpJob:
    """Writes sidecars for items whose details changed. Run in small batches by the worker."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "written": 0, "failed": 0, "message": None}
        self._seen_enabled = False

    def pending(self, conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM assets WHERE xmp_dirty = 1").fetchone()[0]

    def mark_all(self, conn) -> None:
        conn.execute("UPDATE assets SET xmp_dirty = 1 WHERE locked_by IS NULL AND trashed_at IS NULL")

    def run_batch(self, conn, now: float, should_stop, batch: int = 50) -> bool:
        if now < self.retry_at:
            return False
        rows = conn.execute("SELECT id FROM assets WHERE xmp_dirty = 1 LIMIT ?", (batch,)).fetchall()
        if not rows:
            self.state.update(running=False, pending=0)
            # Changes are made all over the app without telling the worker, so look again shortly.
            self.retry_at = now + CHECK_SECONDS
            return False
        self.state["running"] = True
        for (asset_id,) in rows:
            if should_stop():
                break
            try:
                result = sync_one(self.cfg, conn, asset_id)
                if result == "written":
                    self.state["written"] += 1
            except Exception as exc:   # a read-only drive, a sidecar another program is holding open...
                log.info("could not write the sidecar of item %s: %s", asset_id, exc)
                self.state["failed"] += 1
                self.state["message"] = f"Some sidecars could not be written ({exc})."
            # Cleared even after a failure: the next change to the item tries again.
            conn.execute("UPDATE assets SET xmp_dirty = 0 WHERE id = ?", (asset_id,))
            conn.commit()
        self.state["pending"] = self.pending(conn)
        return True


def apply_on_index(conn, asset_id: int, side: dict) -> None:
    """A new file arrived with a sidecar beside it: take in what it says."""
    adopt(conn, asset_id, side)
    for name in side.get("people") or []:
        if not conn.execute("SELECT 1 FROM face_hints WHERE asset_id = ? AND name = ?", (asset_id, name)).fetchone():
            conn.execute("INSERT INTO face_hints (asset_id, name, source) VALUES (?, ?, 'xmp')", (asset_id, name))
