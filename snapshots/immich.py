"""Importing from Immich, through its web API (an API key made in Immich: Account settings, API keys).

Immich gives every photo's SHA-1, so each one is found in Snapshots by content even when the two apps
keep the files in different folders. If Snapshots reads Immich's own library folder (as the library or
an extra folder), nothing needs to be downloaded. Otherwise "copy what's missing" downloads the originals
into "From Immich" in the library.

Brought over: favourites, archived and trashed photos, albums (also ones shared with you), tags, dates
and locations changed in Immich, descriptions, and the names of people with where their faces are.
"""

import base64
import json
import logging
import shutil
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

from . import db, imports, linking, media

log = logging.getLogger("snapshots.immich")

PAGE = 1000


class ImmichError(Exception):
    pass


class Client:
    def __init__(self, url: str, key: str, transport=None):
        url = (url or "").strip().rstrip("/")
        if not url.lower().startswith(("http://", "https://")):
            raise ImmichError("Immich's address should start with http:// or https://, like http://localhost:2283")
        if not url.endswith("/api"):
            url += "/api"
        self.base = url
        self.key = (key or "").strip()
        if not self.key:
            raise ImmichError("Paste an API key from Immich (Account settings → API keys).")
        self.transport = transport or self._urllib

    def _urllib(self, method: str, path: str, body=None, out=None):
        data = json.dumps(body).encode() if body is not None else None
        headers = {"x-api-key": self.key, "Accept": "application/json"}
        if data is not None:
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=120) as resp:
                if out is not None:
                    shutil.copyfileobj(resp, out, 1 << 20)
                    return resp.status, None
                raw = resp.read()
                return resp.status, json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read() or b"{}").get("message")
            except Exception:
                detail = None
            return exc.code, {"message": detail or exc.reason}
        except urllib.error.URLError as exc:
            raise ImmichError(f"Snapshots can't reach Immich at {self.base}: {exc.reason}") from None

    def call(self, method: str, path: str, body=None, ok_missing: bool = False):
        status, data = self.transport(method, path, body)
        if status in (401, 403):
            raise ImmichError("Immich refused the API key. Make a new one in Immich (Account settings → API keys)"
                              " with all permissions.")
        if ok_missing and status in (400, 404):
            return None
        if not 200 <= status < 300:
            msg = (data or {}).get("message") if isinstance(data, dict) else None
            raise ImmichError(f"Immich answered {status} to {path}: {msg or 'error'}")
        return data

    def download(self, asset_id: str, fh) -> None:
        status, data = self.transport("GET", f"/assets/{asset_id}/original", None, fh)
        if not 200 <= status < 300:
            raise ImmichError(f"Immich answered {status} when downloading a photo.")

    def version(self) -> str:
        data = self.call("GET", "/server/version", ok_missing=True) \
            or self.call("GET", "/server-info/version", ok_missing=True) or {}
        return ".".join(str(data.get(k, "?")) for k in ("major", "minor", "patch"))

    def me(self) -> dict:
        return self.call("GET", "/users/me") or {}

    def search(self, body: dict, check=None, on_page=None) -> list[dict]:
        """Every asset a metadata search finds, page by page. Returns [] if this Immich doesn't know a filter."""
        out, page = [], 1
        while page:
            if check:
                check()
            data = self.call("POST", "/search/metadata", {**body, "page": page, "size": PAGE}, ok_missing=True)
            if data is None:
                return out
            assets = data.get("assets") or {}
            items = assets.get("items") or []
            out += items
            if on_page:
                on_page(len(out))
            nxt = assets.get("nextPage")
            page = int(nxt) if nxt and items else 0
        return out

    def albums(self) -> list[dict]:
        found = {}
        for query in ("", "?shared=true"):
            for a in self.call("GET", "/albums" + query, ok_missing=True) or []:
                found[a["id"]] = a
        return list(found.values())

    def album_asset_ids(self, album: dict, check=None) -> set[str]:
        data = self.call("GET", f"/albums/{album['id']}", ok_missing=True) or {}
        listed = data.get("assets")
        if listed and len(listed) >= int(data.get("assetCount") or 0):
            return {a["id"] for a in listed}
        ids = {a["id"] for a in self.search({"albumIds": [album["id"]]}, check)}
        ids |= {a["id"] for a in self.search({"albumIds": [album["id"]], "visibility": "archive"}, check)}
        return ids

    def tags(self) -> list[dict]:
        return self.call("GET", "/tags", ok_missing=True) or []

    def tag_asset_ids(self, tag: dict, check=None) -> set[str]:
        ids = {a["id"] for a in self.search({"tagIds": [tag["id"]]}, check)}
        ids |= {a["id"] for a in self.search({"tagIds": [tag["id"]], "visibility": "archive"}, check)}
        return ids

    def asset(self, asset_id: str) -> dict | None:
        return self.call("GET", f"/assets/{asset_id}", ok_missing=True)


def sha1_hex(checksum: str | None) -> str | None:
    if not checksum:
        return None
    try:
        raw = base64.b64decode(checksum)
    except ValueError:
        return None
    return raw.hex() if len(raw) == 20 else None


def _people(asset: dict) -> list[dict]:
    out = []
    for person in asset.get("people") or []:
        name = (person.get("name") or "").strip()
        if not name or person.get("isHidden"):
            continue
        boxes = []
        for face in person.get("faces") or []:
            try:
                w, h = float(face["imageWidth"]), float(face["imageHeight"])
                x1, y1 = float(face["boundingBoxX1"]), float(face["boundingBoxY1"])
                x2, y2 = float(face["boundingBoxX2"]), float(face["boundingBoxY2"])
                if w > 0 and h > 0 and x2 > x1 and y2 > y1:
                    boxes.append((x1 / w, y1 / h, (x2 - x1) / w, (y2 - y1) / h))
            except (KeyError, TypeError, ValueError):
                continue
        if boxes:
            out += [{"name": name, "box": b} for b in boxes]
        else:
            out.append({"name": name, "box": None})
    return out


def meta_of(asset: dict) -> dict:
    exif = asset.get("exifInfo") or {}
    local = asset.get("localDateTime") or ""
    try:
        taken = datetime.fromisoformat(local[:19]).strftime(imports.DATE_FMT) if local else None
    except ValueError:
        taken = None
    lat, lon = exif.get("latitude"), exif.get("longitude")
    return {
        "favorite": bool(asset.get("isFavorite")),
        "archived": bool(asset.get("isArchived")) or asset.get("visibility") == "archive",
        "trashed": bool(asset.get("isTrashed")),
        "taken_at": taken,
        "lat": float(lat) if lat is not None else None,
        "lon": float(lon) if lon is not None else None,
        "description": exif.get("description") or None,
        "people": _people(asset),
        "albums": [],
        "tags": [],
    }


def run(manager, cfg, url: str, key: str, user_id: int, copy_missing: bool, dry_run: bool, transport=None) -> dict:
    client = Client(url, key, transport)
    check = manager.check
    manager.update(phase="Connecting to Immich")
    version = client.version()
    me = client.me()
    summary = {"immich_version": version, "immich_user": me.get("name") or me.get("email"), "assets": 0,
               "matched": 0, "missing": 0, "missing_bytes": 0, "copied": 0, "copy_failed": 0, "skipped": 0}

    manager.update(phase="Reading Immich's list of photos")
    assets: dict[str, dict] = {}
    base = {"withExif": True, "withPeople": True, "withDeleted": True}
    for extra in ({}, {"visibility": "archive"}):
        for a in client.search({**base, **extra}, check, lambda n: manager.update(done=len(assets) + n)):
            assets.setdefault(a["id"], a)
    summary["assets"] = len(assets)
    metas = {aid: meta_of(a) for aid, a in assets.items()}

    manager.update(phase="Reading albums and tags", done=0)
    albums = client.albums()
    manager.update(total=len(albums))
    for n, album in enumerate(albums):
        name = album.get("albumName") or "Untitled"
        for aid in client.album_asset_ids(album, check):
            if aid in metas:
                metas[aid]["albums"].append(name)
        manager.update(done=n + 1)
    summary["albums_found"] = len(albums)
    tags = client.tags()
    for tag in tags:
        name = tag.get("value") or tag.get("name")
        if not name:
            continue
        for aid in client.tag_asset_ids(tag, check):
            if aid in metas:
                metas[aid]["tags"].append(name)
    summary["tags_found"] = len(tags)

    manager.update(phase="Matching photos" if dry_run else "Bringing over albums, favourites and names",
                   done=0, total=len(assets))
    root = cfg.library_path
    into = root / "From Immich" if root else None
    with db.session(cfg.db_path) as conn:
        applier = imports.Applier(conn, user_id, "immich")
        tick = imports.progress_every()
        for n, (aid, asset) in enumerate(assets.items()):
            check()
            if tick():
                manager.update(done=n)
            sha1 = sha1_hex(asset.get("checksum"))
            ids = imports.find_assets(conn, sha1) if sha1 else []
            if ids is None:     # only in someone's locked folder: left alone
                summary["matched"] += 1
                continue
            if ids:
                summary["matched"] += 1
            else:
                kind_ok = asset.get("type") in ("IMAGE", "VIDEO") and media.kind_of(asset.get("originalFileName") or "")
                if not kind_ok or metas[aid]["trashed"] or not sha1:
                    summary["skipped"] += 1
                    continue
                summary["missing"] += 1
                summary["missing_bytes"] += int((asset.get("exifInfo") or {}).get("fileSizeInByte") or 0)
                if dry_run or not copy_missing or into is None:
                    continue
                new_id = _download(cfg, conn, client, asset, sha1, into)
                if new_id is None:
                    summary["copy_failed"] += 1
                    continue
                summary["copied"] += 1
                ids = [new_id]
            if dry_run:
                continue
            for i in ids:
                applier.apply(i, metas[aid])
            if n % 100 == 0:
                conn.commit()
        if not dry_run:
            linking.link_pairs(conn, cfg)
            applier.finish()
            summary.update(applier.counts)
    manager.update(done=len(assets))
    return summary


def _download(cfg, conn, client: Client, asset: dict, sha1: str, into) -> int | None:
    local = asset.get("localDateTime") or asset.get("fileCreatedAt") or ""
    folder = into / (local[:4] or "Unknown") / (local[5:7] or "")
    name = asset.get("originalFileName") or f"{asset['id']}.jpg"
    try:
        new_id = imports.add_file(cfg, conn, folder, name, lambda fh: client.download(asset["id"], fh), sha1)
    except Exception as exc:
        log.warning("could not download %s from Immich: %s", name, exc)
        return None
    video_id = asset.get("livePhotoVideoId")
    if new_id and video_id:   # a Live Photo: its clip goes beside it, so Snapshots pairs them
        try:
            clip = client.asset(video_id) or {}
            ext = "." + (clip.get("originalFileName") or "x.mov").rsplit(".", 1)[-1]
            stem = conn.execute("SELECT filename FROM assets WHERE id = ?", (new_id,)).fetchone()[0].rsplit(".", 1)[0]
            imports.add_file(cfg, conn, folder, stem + ext, lambda fh: client.download(video_id, fh),
                             sha1_hex(clip.get("checksum")))
        except Exception as exc:
            log.warning("could not download the clip of %s from Immich: %s", name, exc)
    return new_id
