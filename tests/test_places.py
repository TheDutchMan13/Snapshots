import shutil
import subprocess
import zipfile

import pytest

from snapshots import media, places
from helpers import ids_by_name, make_jpeg, new_client, setup_admin, wait_for, wait_scan

# A tiny copy of the GeoNames files, in their real format (19 tab-separated columns).
CITIES = [
    # id, name, lat, lon, country, admin1, feature code
    (1, "Phoenix", 33.44838, -112.07404, "US", "AZ", "PPLA"),
    (2, "Tempe", 33.41477, -111.90931, "US", "AZ", "PPL"),
    (3, "Amsterdam", 52.37403, 4.88969, "NL", "07", "PPLC"),
    (4, "Suva", -18.14161, 178.44149, "FJ", "C", "PPLC"),
    (5, "Wairiki", -16.80000, -179.97000, "FJ", "03", "PPL"),  # just across the date line
    (6, "Nowhere Town", 10.0, 10.0, "ZZ", "99", "PPL"),          # unknown country and state codes
    (7, "Encanto", 33.47900, -112.09000, "US", "AZ", "PPLX"),    # a neighbourhood: too detailed, skipped
]


def geonames_folder(folder):
    folder.mkdir(parents=True, exist_ok=True)
    lines = []
    for gid, name, lat, lon, cc, a1, code in CITIES:
        cols = [str(gid), name, name, "", f"{lat}", f"{lon}", "P", code, cc, "", a1, "", "", "", "1000", "",
                "10", "Etc/UTC", "2024-01-01"]
        lines.append("\t".join(cols))
    with zipfile.ZipFile(folder / "cities500.zip", "w") as zf:
        zf.writestr("cities500.txt", "\n".join(lines) + "\n")
    (folder / "admin1CodesASCII.txt").write_text(
        "US.AZ\tArizona\tArizona\t5551752\nNL.07\tNorth Holland\tNorth Holland\t2749879\n"
        "FJ.C\tCentral\tCentral\t1\nFJ.03\tNorthern\tNorthern\t2\n", encoding="utf-8")
    (folder / "countryInfo.txt").write_text(
        "# ISO\tISO3\tISO-Numeric\tfips\tCountry\tCapital\n"
        "US\tUSA\t840\tUS\tUnited States\tWashington\n"
        "NL\tNLD\t528\tNL\tNetherlands\tAmsterdam\n"
        "FJ\tFJI\t242\tFJ\tFiji\tSuva\n", encoding="utf-8")
    return folder.as_uri() + "/"


def test_gps_is_read_from_photos(tmp_path):
    make_jpeg(tmp_path / "p.jpg", gps=(33.4484, -112.0740))
    info = media.read_photo_info(tmp_path / "p.jpg")
    assert info["lat"] == pytest.approx(33.4484, abs=1e-4) and info["lon"] == pytest.approx(-112.0740, abs=1e-4)
    make_jpeg(tmp_path / "zero.jpg", gps=(0.0, 0.0))  # "no fix" on many phones
    assert media.read_photo_info(tmp_path / "zero.jpg")["lat"] is None


def test_geocoder_lookups(tmp_path):
    url = geonames_folder(tmp_path / "src")
    cache = places.build_cache(tmp_path / "places", base_url=url)
    assert not (tmp_path / "places" / "cities500.zip").exists()  # only the small file is kept
    g = places.Geocoder(cache)
    assert g.lookup(33.45, -112.07) == ("Phoenix", "Arizona", "United States")
    assert g.lookup(33.479, -112.09) == ("Phoenix", "Arizona", "United States")  # not the neighbourhood
    assert g.lookup(33.42, -111.92) == ("Tempe", "Arizona", "United States")
    assert g.lookup(52.37, 4.90) == ("Amsterdam", "North Holland", "Netherlands")
    assert g.lookup(-16.8, 179.99) == ("Wairiki", "Northern", "Fiji")  # nearest is across the date line
    assert g.lookup(30.0, -140.0) is None  # the middle of the ocean: no town within 150 km
    assert g.lookup(10.01, 10.01) == ("Nowhere Town", "", "ZZ")  # unknown codes fall back gracefully


@pytest.fixture
def lib(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "phoenix.jpg", "2024:01:01 10:00:00", gps=(33.45, -112.07))
    make_jpeg(root / "tempe.jpg", "2024:01:02 10:00:00", gps=(33.42, -111.92))
    make_jpeg(root / "ams.jpg", "2024:01:03 10:00:00", gps=(52.37, 4.90))
    make_jpeg(root / "sea.jpg", "2024:01:04 10:00:00", gps=(30.0, -140.0))
    make_jpeg(root / "nogps.jpg", "2024:01:05 10:00:00")
    return root


def test_places_end_to_end(tmp_path, lib):
    url = geonames_folder(tmp_path / "geonames")
    c, cfg = new_client(tmp_path, lib, places=True)
    c.app.state.ctx.worker.places.base_url = url
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: c.get("/api/status").json()["places"]["pending"] == 0
                 and c.get("/api/places").json()["places"])
        data = c.get("/api/places").json()
        got = {(p["country"], p["state"], p["city"]): p["count"] for p in data["places"]}
        assert got == {("Netherlands", "North Holland", "Amsterdam"): 1,
                       ("United States", "Arizona", "Phoenix"): 1,
                       ("United States", "Arizona", "Tempe"): 1}
        assert data["located"] == 4 and all(p["cover_v"] for p in data["places"])

        assert set(ids_by_name(c, q="arizona")) == {"phoenix.jpg", "tempe.jpg"}
        assert set(ids_by_name(c, q="amsterdam")) == {"ams.jpg"}
        assert set(ids_by_name(c, country="United States", state="Arizona")) == {"phoenix.jpg", "tempe.jpg"}
        assert set(ids_by_name(c, country="United States", state="Arizona", city="Tempe")) == {"tempe.jpg"}
        ams = ids_by_name(c)["ams.jpg"]
        assert c.get(f"/api/assets/{ams}").json()["place"] == {
            "city": "Amsterdam", "state": "North Holland", "country": "Netherlands"}
        sea = ids_by_name(c)["sea.jpg"]
        detail = c.get(f"/api/assets/{sea}").json()
        assert detail["place"] is None and detail["has_position"] is True


def test_failed_download_is_retried_later(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib, places=True)
    job = c.app.state.ctx.worker.places
    job.base_url = (tmp_path / "does-not-exist").as_uri() + "/"
    with c:
        setup_admin(c)
        wait_scan(c)
        wait_for(lambda: job.retry_at > 0)
        status = c.get("/api/status").json()["places"]
        assert "try again" in status["message"] and status["pending"] == 4


@pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")), reason="needs ffmpeg")
def test_video_date_and_position(tmp_path):
    video = tmp_path / "clip.mp4"
    subprocess.run(
        ["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i", "testsrc=duration=1:size=320x240:rate=10",
         "-pix_fmt", "yuv420p", "-metadata", "location=+33.4484-112.0740/",
         "-metadata", "creation_time=2021-06-15T18:30:00Z", str(video)],
        check=True,
    )
    info = media.read_video_info(video)
    assert (info["lat"], info["lon"]) == (33.4484, -112.074)
    assert (info["width"], info["height"]) == (320, 240)
    assert info["taken"] is not None and info["taken"].year == 2021
