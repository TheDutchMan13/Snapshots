"""Turning GPS positions into place names, offline.

The first time it is needed, the list of towns and cities from GeoNames
(https://www.geonames.org, CC BY 4.0) is downloaded once (about 14 MB) and boiled down to a
small file in the data folder. After that, every lookup happens on this computer:
photo positions are never sent anywhere.
"""

import csv
import io
import logging
import math
import os
import sys
import urllib.request
import zipfile
from pathlib import Path

log = logging.getLogger("snapshots.places")

GEONAMES_URL = "https://download.geonames.org/export/dump/"
_SOURCE_FILES = ("cities500.zip", "admin1CodesASCII.txt", "countryInfo.txt")
_CACHE_NAME = "places-v2.tsv"
# Neighbourhoods (PPLX) are too fine-grained for photo albums; abandoned, historical and destroyed
# places are not where photos are taken today.
_SKIP_FEATURES = {"PPLX", "PPLQ", "PPLH", "PPLW", "PPLCH"}
MAX_DISTANCE_KM = 150  # further than this from any town (at sea, say), no place is given
_EARTH_KM = 6371.0


def _download(url: str, dest: Path) -> None:
    tmp = dest.with_name(dest.name + ".part")
    req = urllib.request.Request(url, headers={"User-Agent": "Snapshots photo library"})
    with urllib.request.urlopen(req, timeout=120) as resp, open(tmp, "wb") as out:
        while chunk := resp.read(1 << 16):
            out.write(chunk)
    os.replace(tmp, dest)


def build_cache(folder: Path, base_url: str = GEONAMES_URL) -> Path:
    """Download the GeoNames files and write the compact place list. Returns its path."""
    folder.mkdir(parents=True, exist_ok=True)
    for name in _SOURCE_FILES:
        if not (folder / name).exists():
            log.info("downloading %s", name)
            _download(base_url + name, folder / name)

    countries = {}
    with open(folder / "countryInfo.txt", encoding="utf-8") as fh:
        for line in fh:
            if line.startswith("#") or not line.strip():
                continue
            cols = line.rstrip("\n").split("\t")
            if len(cols) > 4:
                countries[cols[0]] = cols[4]

    states = {}
    with open(folder / "admin1CodesASCII.txt", encoding="utf-8") as fh:
        for line in fh:
            cols = line.rstrip("\n").split("\t")
            if len(cols) > 1:
                states[cols[0]] = cols[1]

    cache = folder / _CACHE_NAME
    tmp = cache.with_name(cache.name + ".part")
    rows = 0
    with zipfile.ZipFile(folder / "cities500.zip") as zf, open(tmp, "w", encoding="utf-8", newline="") as out:
        with zf.open("cities500.txt") as raw:
            for line in io.TextIOWrapper(raw, encoding="utf-8"):
                cols = line.rstrip("\n").split("\t")
                if len(cols) < 11 or cols[7] in _SKIP_FEATURES:
                    continue
                try:
                    lat, lon = float(cols[4]), float(cols[5])
                except ValueError:
                    continue
                cc = cols[8]
                city = cols[1].replace("\t", " ")
                state = states.get(f"{cc}.{cols[10]}", "")
                country = countries.get(cc, cc)
                out.write(f"{lat:.5f}\t{lon:.5f}\t{city}\t{state}\t{country}\n")
                rows += 1
    if rows < 1000 and base_url == GEONAMES_URL:
        tmp.unlink(missing_ok=True)
        raise RuntimeError("The downloaded place list looks incomplete.")
    os.replace(tmp, cache)
    for name in _SOURCE_FILES:  # the compact file is all that is needed from now on
        (folder / name).unlink(missing_ok=True)
    return cache


class Geocoder:
    """Finds the nearest town to a position. Load once, then look up many."""

    def __init__(self, cache_file: Path):
        self.names: list[tuple[str, str, str]] = []
        self.grid: dict[tuple[int, int], list[tuple[float, float, int]]] = {}
        with open(cache_file, encoding="utf-8", newline="") as fh:
            for row in csv.reader(fh, delimiter="\t", quoting=csv.QUOTE_NONE):
                if len(row) < 5:
                    continue
                lat, lon = float(row[0]), float(row[1])
                self.grid.setdefault((math.floor(lat), math.floor(lon)), []).append((lat, lon, len(self.names)))
                # States and countries repeat thousands of times: share one copy of each name.
                self.names.append((row[2], sys.intern(row[3]), sys.intern(row[4])))

    @staticmethod
    def _km(lat1, lon1, lat2, lon2) -> float:
        p1, p2 = math.radians(lat1), math.radians(lat2)
        dp, dl = p2 - p1, math.radians(lon2 - lon1)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 2 * _EARTH_KM * math.asin(min(1.0, math.sqrt(a)))

    def lookup(self, lat: float, lon: float) -> tuple[str, str, str] | None:
        """(city, state, country) of the nearest town within MAX_DISTANCE_KM, else None."""
        ilat, ilon = math.floor(lat), math.floor(lon)
        lat_cells = 2  # a degree of latitude is ~111 km
        km_per_lon_degree = 111.32 * max(math.cos(math.radians(lat)), 0.01)
        lon_cells = min(180, math.ceil(MAX_DISTANCE_KM / km_per_lon_degree) + 1)
        best, best_km = None, MAX_DISTANCE_KM
        for dy in range(-lat_cells, lat_cells + 1):
            for dx in range(-lon_cells, lon_cells + 1):
                cx = (ilon + dx + 180) % 360 - 180  # wrap around the date line
                for plat, plon, idx in self.grid.get((ilat + dy, cx), ()):
                    d = self._km(lat, lon, plat, plon)
                    if d < best_km:
                        best, best_km = idx, d
        return self.names[best] if best is not None else None


class PlacesJob:
    """Fills in city / state / country for photos that have a position. Run by the worker."""

    def __init__(self, cfg, base_url: str = GEONAMES_URL):
        self.cfg = cfg
        self.base_url = base_url
        self.geocoder: Geocoder | None = None
        self.retry_at = 0.0
        self.state = {"running": False, "pending": 0, "message": None}

    def pending(self, conn) -> int:
        return conn.execute(
            "SELECT COUNT(*) FROM assets WHERE geo_done = 0 AND lat IS NOT NULL"
        ).fetchone()[0]

    def unload(self) -> None:
        self.geocoder = None  # frees the memory until it is needed again

    def run_batch(self, conn, now: float, should_stop, batch: int = 500) -> bool:
        """Look up one batch. Returns True if anything was done (more may remain)."""
        if now < self.retry_at:
            return False
        rows = conn.execute(
            "SELECT id, lat, lon FROM assets WHERE geo_done = 0 AND lat IS NOT NULL LIMIT ?", (batch,)
        ).fetchall()
        self.state["pending"] = self.pending(conn)
        if not rows:
            self.unload()
            self.state["running"] = False
            return False
        self.state["running"] = True
        if self.geocoder is None:
            try:
                cache = self.cfg.places_dir / _CACHE_NAME
                if not cache.exists():
                    self.state["message"] = "Downloading the place-name list (about 14 MB)…"
                    build_cache(self.cfg.places_dir, self.base_url)
                self.geocoder = Geocoder(cache)
                self.state["message"] = None
            except Exception as exc:
                log.warning("place names unavailable: %s", exc)
                self.state["message"] = "Could not download place names. Will try again in an hour."
                self.state["running"] = False
                self.retry_at = now + 3600
                return False
        for row in rows:
            if should_stop():
                break
            place = self.geocoder.lookup(row["lat"], row["lon"])
            city, state, country = place if place else (None, None, None)
            conn.execute(
                "UPDATE assets SET city = ?, state = ?, country = ?, geo_done = 1 WHERE id = ?",
                (city, state or None, country, row["id"]),
            )
        conn.commit()
        self.state["pending"] = self.pending(conn)
        return True


def search_towns(cfg, text: str, limit: int = 12) -> list[dict]:
    """Towns whose name starts with `text` (then those containing it), from the offline place list.

    Used to set a photo's location by typing a town. Empty until the list has been downloaded.
    """
    cache = cfg.places_dir / _CACHE_NAME
    needle = " ".join(text.split()).casefold()
    if not needle or not cache.is_file():
        return []
    starts, contains = [], []
    with open(cache, encoding="utf-8", newline="") as fh:
        for line in fh:
            parts = line.rstrip("\n").split("\t")
            if len(parts) < 5:
                continue
            name = parts[2].casefold()
            if name.startswith(needle):
                starts.append(parts)
            elif len(contains) < limit and needle in name:
                contains.append(parts)
    # Exact names first, then shorter names (usually the better-known place).
    starts.sort(key=lambda p: (p[2].casefold() != needle, len(p[2])))
    out = []
    for p in (starts + contains)[:limit]:
        out.append({"city": p[2], "state": p[3] or None, "country": p[4], "lat": float(p[0]), "lon": float(p[1])})
    return out
