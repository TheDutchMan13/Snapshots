"""Reading photo metadata and making thumbnails. Originals are only ever read."""

import json
import math
import os
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageOps

try:  # HEIC / HEIF (iPhone photos) and AVIF need this optional codec package
    import pillow_heif

    pillow_heif.register_heif_opener()
    try:
        pillow_heif.register_avif_opener()
    except Exception:
        pass
    HEIF = True
except Exception:  # pragma: no cover - depends on the install
    HEIF = False

PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
}
VIDEO_EXTS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm", ".3gp"}
# Formats every browser can show directly. Anything else gets a JPEG preview.
BROWSER_NATIVE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".avif"}

THUMB_SIZE = 480
PREVIEW_SIZE = 2000

_EXIF_DATETIME_ORIGINAL = 36867
_EXIF_DATETIME_DIGITIZED = 36868
_EXIF_DATETIME = 306
_EXIF_MAKE = 271
_EXIF_MODEL = 272
_EXIF_ORIENTATION = 274
_EXIF_SUBIFD = 0x8769
_EXIF_GPSIFD = 0x8825


def kind_of(path: str | os.PathLike) -> str | None:
    ext = os.path.splitext(str(path))[1].lower()
    if ext in PHOTO_EXTS:
        return "photo"
    if ext in VIDEO_EXTS:
        return "video"
    return None


def has_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None


# ------------------------------------------------------------- dates

_FILENAME_DATE = re.compile(r"(?<!\d)((?:19|20)\d{2})(\d{2})(\d{2})(?:[_\-. T]?(\d{2})(\d{2})(\d{2}))?(?!\d)")


def _parse_exif_date(value) -> datetime | None:
    if not isinstance(value, str):
        return None
    value = value.strip().replace("\x00", "")
    try:
        parsed = datetime.strptime(value[:19], "%Y:%m:%d %H:%M:%S")
    except ValueError:
        return None
    return parsed if parsed.year >= 1900 else None


def date_from_filename(name: str) -> datetime | None:
    """Phones often keep the date in the name (IMG_20241005_142233.jpg)."""
    m = _FILENAME_DATE.search(name)
    if not m:
        return None
    y, mo, d, hh, mm, ss = m.groups()
    try:
        return datetime(int(y), int(mo), int(d), int(hh or 0), int(mm or 0), int(ss or 0))
    except ValueError:
        return None


# ------------------------------------------------------------- photos


def _valid_position(lat, lon) -> bool:
    return (
        lat is not None and lon is not None
        and math.isfinite(lat) and math.isfinite(lon)
        and -90 <= lat <= 90 and -180 <= lon <= 180
        and not (abs(lat) < 1e-6 and abs(lon) < 1e-6)  # 0,0 means "no fix" on many cameras
    )


def _gps_degrees(value, ref) -> float | None:
    """EXIF stores degrees, minutes, seconds as three fractions."""
    try:
        d, m, s = (float(v) for v in value)
    except (TypeError, ValueError, ZeroDivisionError):
        return None
    deg = d + m / 60 + s / 3600
    if isinstance(ref, bytes):
        ref = ref.decode("ascii", "ignore")
    if str(ref).strip().upper() in ("S", "W"):
        deg = -deg
    return deg


def read_photo_info(path: Path) -> dict:
    """Return taken (datetime|None), width, height, camera, lat, lon. Never raises."""
    info = {"taken": None, "width": None, "height": None, "camera": None, "lat": None, "lon": None}
    try:
        with Image.open(path) as img:
            w, h = img.size
            exif = img.getexif()
            if exif.get(_EXIF_ORIENTATION) in (5, 6, 7, 8):
                w, h = h, w
            info["width"], info["height"] = w, h

            sub = {}
            try:
                sub = exif.get_ifd(_EXIF_SUBIFD)
            except Exception:
                pass
            for raw in (
                sub.get(_EXIF_DATETIME_ORIGINAL),
                sub.get(_EXIF_DATETIME_DIGITIZED),
                exif.get(_EXIF_DATETIME),
            ):
                parsed = _parse_exif_date(raw)
                if parsed:
                    info["taken"] = parsed
                    break

            make = str(exif.get(_EXIF_MAKE) or "").replace("\x00", "").strip()
            model = str(exif.get(_EXIF_MODEL) or "").replace("\x00", "").strip()
            if model.lower().startswith(make.lower()):
                camera = model
            else:
                camera = f"{make} {model}".strip()
            info["camera"] = camera or None

            try:
                gps = exif.get_ifd(_EXIF_GPSIFD)
            except Exception:
                gps = {}
            if gps:
                lat = _gps_degrees(gps.get(2), gps.get(1))
                lon = _gps_degrees(gps.get(4), gps.get(3))
                if _valid_position(lat, lon):
                    info["lat"], info["lon"] = round(lat, 6), round(lon, 6)
    except Exception:
        pass
    return info


def load_oriented(path: Path, max_side: int) -> Image.Image:
    """The picture the right way up, in RGB, no bigger than max_side. Raises if unreadable."""
    with Image.open(path) as img:
        img.draft("RGB", (max_side, max_side))
        img = ImageOps.exif_transpose(img)
        img.thumbnail((max_side, max_side), Image.LANCZOS)
        return _to_rgb(img)


def _to_rgb(img: Image.Image) -> Image.Image:
    if img.mode in ("RGBA", "LA") or (img.mode == "P" and "transparency" in img.info):
        rgba = img.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.split()[-1])
        return background
    return img.convert("RGB")


def _save_jpeg(src: Path, dst: Path, size: int, quality: int) -> bool:
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with Image.open(src) as img:
            img.draft("RGB", (size * 2, size * 2))  # fast decode for big JPEGs
            img = ImageOps.exif_transpose(img)
            img.thumbnail((size, size), Image.LANCZOS)
            _to_rgb(img).save(tmp, "JPEG", quality=quality)
        os.replace(tmp, dst)
        return True
    except Exception:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def make_thumb(src: Path, dst: Path) -> bool:
    return _save_jpeg(src, dst, THUMB_SIZE, 82)


def make_preview(src: Path, dst: Path) -> bool:
    return _save_jpeg(src, dst, PREVIEW_SIZE, 88)


# ------------------------------------------------------------- videos


_ISO6709 = re.compile(r"([+-]\d+(?:\.\d+)?)([+-]\d+(?:\.\d+)?)")


def read_video_info(path: Path) -> dict:
    """Date, size and position from a video, if ffprobe is installed. Never raises."""
    info = {"taken": None, "width": None, "height": None, "camera": None, "lat": None, "lon": None}
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return info
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-print_format", "json", "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=30, stdin=subprocess.DEVNULL, check=True,
        ).stdout
        data = json.loads(out or b"{}")
    except (subprocess.SubprocessError, OSError, ValueError):
        return info

    tags = {k.lower(): v for k, v in (data.get("format", {}).get("tags") or {}).items()}
    created = tags.get("com.apple.quicktime.creationdate") or tags.get("creation_time")
    if created:
        try:
            when = datetime.fromisoformat(created.replace("Z", "+00:00"))
            if when.tzinfo is not None:
                when = when.astimezone().replace(tzinfo=None)  # shown in this computer's time zone
            if when.year >= 1990:  # 1904 and 1970 are "not set" on many devices
                info["taken"] = when
        except ValueError:
            pass

    where = tags.get("com.apple.quicktime.location.iso6709") or tags.get("location")
    m = _ISO6709.match(where or "")
    if m and _valid_position(float(m.group(1)), float(m.group(2))):
        info["lat"], info["lon"] = round(float(m.group(1)), 6), round(float(m.group(2)), 6)

    model = tags.get("com.apple.quicktime.model") or tags.get("model")
    make = tags.get("com.apple.quicktime.make") or tags.get("make")
    if model:
        info["camera"] = model if (not make or model.lower().startswith(make.lower())) else f"{make} {model}"

    try:
        for stream in data.get("streams") or []:
            if stream.get("codec_type") == "video" and stream.get("width"):
                w, h = int(stream["width"]), int(stream.get("height") or 0)
                rotation = 0
                for side in stream.get("side_data_list") or []:
                    if "rotation" in side:
                        rotation = int(float(side["rotation"]))
                rotation = rotation or int(float((stream.get("tags") or {}).get("rotate", 0) or 0))
                if abs(rotation) % 180 == 90:
                    w, h = h, w
                info["width"], info["height"] = w, h or None
                break
    except (TypeError, ValueError):
        pass
    return info


def make_video_thumb(src: Path, dst: Path) -> bool:
    """A frame from the video, if ffmpeg is installed. Otherwise no thumbnail."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        return False
    tmp = dst.with_name(dst.stem + ".tmp.jpg")
    for seek in ("1", "0"):  # very short clips have no frame at 1 second
        cmd = [
            ffmpeg, "-v", "error", "-y", "-ss", seek, "-i", str(src),
            "-frames:v", "1", "-vf", f"scale='min({THUMB_SIZE},iw)':-2",
            "-q:v", "4", str(tmp),
        ]
        try:
            subprocess.run(
                cmd, timeout=60, capture_output=True, stdin=subprocess.DEVNULL, check=True
            )
        except (subprocess.SubprocessError, OSError):
            continue
        if tmp.exists() and tmp.stat().st_size > 0:
            os.replace(tmp, dst)
            return True
    try:
        tmp.unlink()
    except OSError:
        pass
    return False
