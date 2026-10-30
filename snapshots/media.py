"""Reading photo metadata and making thumbnails. Originals are only ever read."""

import io
import json
import math
import os
import re
import shutil
import struct
import subprocess
from contextlib import contextmanager
from datetime import datetime
from html import unescape
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

try:  # camera RAW files (LibRaw)
    import rawpy

    RAW = True
except Exception:  # pragma: no cover - depends on the install
    rawpy = None
    RAW = False

RAW_EXTS = {
    ".dng", ".cr2", ".cr3", ".crw", ".nef", ".nrw", ".arw", ".srf", ".sr2", ".orf", ".rw2", ".raf",
    ".pef", ".srw", ".3fr", ".erf", ".kdc", ".dcr", ".mrw", ".mos", ".iiq", ".rwl", ".x3f",
}
PHOTO_EXTS = {
    ".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp",
    ".tif", ".tiff", ".heic", ".heif", ".avif",
} | RAW_EXTS
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


def is_raw(path: str | os.PathLike) -> bool:
    return os.path.splitext(str(path))[1].lower() in RAW_EXTS


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


def _info_from_exif(exif, info: dict) -> None:
    """Fill in date, camera and position from an Image.Exif."""
    sub = {}
    try:
        sub = exif.get_ifd(_EXIF_SUBIFD)
    except Exception:
        pass
    if info["taken"] is None:
        for raw in (
            sub.get(_EXIF_DATETIME_ORIGINAL),
            sub.get(_EXIF_DATETIME_DIGITIZED),
            exif.get(_EXIF_DATETIME_ORIGINAL),   # CR3 keeps these at the top level of a separate block
            exif.get(_EXIF_DATETIME),
        ):
            parsed = _parse_exif_date(raw)
            if parsed:
                info["taken"] = parsed
                break

    if info["camera"] is None:
        make = str(exif.get(_EXIF_MAKE) or "").replace("\x00", "").strip()
        model = str(exif.get(_EXIF_MODEL) or "").replace("\x00", "").strip()
        if model.lower().startswith(make.lower()):
            camera = model
        else:
            camera = f"{make} {model}".strip()
        info["camera"] = camera or None

    if info["lat"] is None:
        try:
            gps = exif.get_ifd(_EXIF_GPSIFD)
        except Exception:
            gps = {}
        if gps:
            lat = _gps_degrees(gps.get(2), gps.get(1))
            lon = _gps_degrees(gps.get(4), gps.get(3))
            if _valid_position(lat, lon):
                info["lat"], info["lon"] = round(lat, 6), round(lon, 6)


def _empty_info() -> dict:
    return {"taken": None, "width": None, "height": None, "camera": None, "lat": None, "lon": None,
            "keywords": []}


def read_photo_info(path: Path) -> dict:
    """Return taken (datetime|None), width, height, camera, lat, lon and keywords. Never raises."""
    if is_raw(path):
        return _read_raw_info(path)
    info = _empty_info()
    try:
        with Image.open(path) as img:
            w, h = img.size
            exif = img.getexif()
            if exif.get(_EXIF_ORIENTATION) in (5, 6, 7, 8):
                w, h = h, w
            info["width"], info["height"] = w, h
            _info_from_exif(exif, info)
    except Exception:
        pass
    info["keywords"] = read_keywords(path)
    return info


# ------------------------------------------------------------- camera RAW files
#
# Most RAW files are TIFF files underneath, so their EXIF can be read without decoding the picture.
# Every RAW file also carries a JPEG preview made by the camera: that is what thumbnails and the
# viewer use, as it is quick and looks like what the camera showed. Only when there is no usable
# preview is the RAW data itself developed (at half size).

_TIFF_MAGIC = (b"II*\x00", b"MM\x00*")
_RAW_HEAD = 4 * 1024 * 1024        # EXIF of RAW files sits near the start; read at most this much


def _raw_exifs(path: Path) -> list:
    """Image.Exif blocks found in a RAW file without decoding it (may be empty)."""
    out = []
    try:
        with open(path, "rb") as fh:
            head = fh.read(_RAW_HEAD)
    except OSError:
        return out
    blocks = []
    if head[:4] in _TIFF_MAGIC:
        blocks.append(head)
    elif head[:2] == b"II" and head[2:4] in (b"RO", b"RS", b"U\x00"):  # Olympus ORF, Panasonic RW2
        blocks.append(b"II*\x00" + head[4:])
    elif head[4:8] == b"ftyp" and head[8:12] == b"crx ":  # Canon CR3: TIFF blocks in CMT boxes
        for box in (b"CMT1", b"CMT2", b"CMT4"):
            i = head.find(box)
            if i >= 4:
                size = struct.unpack(">I", head[i - 4:i])[0]
                blocks.append(head[i + 4:i - 4 + size])
    elif head[:16] == b"FUJIFILMCCD-RAW ":  # Fujifilm RAF: the preview JPEG has the EXIF
        offset, length = struct.unpack(">II", head[84:92])
        jpeg = head[offset:offset + length]
        if len(jpeg) < length:
            try:
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    jpeg = fh.read(length)
            except OSError:
                jpeg = b""
        try:
            with Image.open(io.BytesIO(jpeg)) as img:
                out.append(img.getexif())
        except Exception:
            pass
    for block in blocks:
        try:
            exif = Image.Exif()
            exif.load(block)
            if len(exif):
                out.append(exif)
        except Exception:
            pass
    return out


def _raw_orientation(exifs) -> int:
    for exif in exifs:
        value = exif.get(_EXIF_ORIENTATION)
        if value in range(1, 9):
            return value
    return 1


# LibRaw's "flip" in EXIF orientation terms
_FLIP_TO_ORIENTATION = {0: 1, 3: 3, 5: 8, 6: 6}


@contextmanager
def _open_raw(path: Path):
    if rawpy is None:
        raise OSError("RAW files need the rawpy package")
    raw = rawpy.imread(str(path))
    try:
        yield raw
    finally:
        raw.close()


def _read_raw_info(path: Path) -> dict:
    info = _empty_info()
    exifs = _raw_exifs(path)
    for exif in exifs:
        _info_from_exif(exif, info)
    orientation = _raw_orientation(exifs)
    try:
        with _open_raw(path) as raw:
            w, h = raw.sizes.width, raw.sizes.height
            if not exifs:
                orientation = _FLIP_TO_ORIENTATION.get(raw.sizes.flip, 1)
            if orientation in (5, 6, 7, 8):
                w, h = h, w
            info["width"], info["height"] = int(w), int(h)
            if info["taken"] is None or info["camera"] is None:
                # Some cameras only put EXIF in the preview JPEG.
                try:
                    thumb = raw.extract_thumb()
                    if thumb.format == rawpy.ThumbFormat.JPEG:
                        with Image.open(io.BytesIO(thumb.data)) as img:
                            _info_from_exif(img.getexif(), info)
                except Exception:
                    pass
    except Exception:
        pass
    info["keywords"] = read_keywords(path)
    return info


_TRANSPOSE = {
    2: Image.Transpose.FLIP_LEFT_RIGHT, 3: Image.Transpose.ROTATE_180, 4: Image.Transpose.FLIP_TOP_BOTTOM,
    5: Image.Transpose.TRANSPOSE, 6: Image.Transpose.ROTATE_270, 7: Image.Transpose.TRANSVERSE,
    8: Image.Transpose.ROTATE_90,
}


def _raw_picture(path: Path, max_side: int) -> Image.Image:
    """A RAW file's picture the right way up, in RGB, no bigger than max_side (when it can be)."""
    exifs = _raw_exifs(path)
    with _open_raw(path) as raw:
        orientation = _raw_orientation(exifs) if exifs else _FLIP_TO_ORIENTATION.get(raw.sizes.flip, 1)
        picture = None
        try:
            thumb = raw.extract_thumb()
            if thumb.format == rawpy.ThumbFormat.JPEG:
                picture = Image.open(io.BytesIO(thumb.data))
                picture.draft("RGB", (max_side, max_side))
                picture = picture.convert("RGB")
            elif thumb.format == rawpy.ThumbFormat.BITMAP:
                picture = Image.fromarray(thumb.data)
        except Exception:  # no preview in this file
            picture = None
        # A tiny preview (some older cameras) is not good enough for the viewer: develop the RAW instead.
        full_side = max(raw.sizes.width, raw.sizes.height)
        if picture is not None and max(picture.size) < min(max_side, full_side) * 0.6:
            picture = None
        if picture is not None:
            if orientation in _TRANSPOSE:
                picture = picture.transpose(_TRANSPOSE[orientation])
        else:
            # LibRaw turns the developed picture the right way up itself.
            rgb = raw.postprocess(half_size=True, use_camera_wb=True, no_auto_bright=False, output_bps=8)
            picture = Image.fromarray(rgb)
    picture.thumbnail((max_side, max_side), Image.LANCZOS)
    return picture.convert("RGB")


# ------------------------------------------------------------- keywords (tags written by other programs)

_XMP_START = re.compile(rb"<x:xmpmeta|<rdf:RDF")
_SUBJECT = re.compile(rb"<dc:subject>\s*<rdf:Bag>(.*?)</rdf:Bag>", re.S)
_LI = re.compile(rb"<rdf:li[^>]*>(.*?)</rdf:li>", re.S)
_HEAD_BYTES = 256 * 1024


def _read_head(path: Path, n: int = _HEAD_BYTES) -> bytes:
    try:
        with open(path, "rb") as fh:
            return fh.read(n)
    except OSError:
        return b""


def read_keywords(path: Path, head: bytes | None = None) -> list[str]:
    """Keywords (dc:subject) that Lightroom, digiKam, Windows Photo Gallery and others store in XMP."""
    head = _read_head(path) if head is None else head
    words = []
    m = _SUBJECT.search(head)
    if m:
        for li in _LI.findall(m.group(1)):
            try:
                word = unescape(li.decode("utf-8")).strip()
            except UnicodeDecodeError:
                continue
            if word and len(word) <= 100 and word not in words:
                words.append(word)
    return words[:50]


# ------------------------------------------------------------- motion photos
#
# Google, Samsung and other Android phones can save a short video inside the photo file itself:
# the photo comes first and the video (an MP4) is added at the end. The photo's XMP says so.

_MOTION_FLAG = re.compile(rb'(?:MotionPhoto|MicroVideo)\s*=\s*"1"|<GCamera:(?:MotionPhoto|MicroVideo)>1<')
_MICRO_OFFSET = re.compile(rb'MicroVideoOffset\s*=\s*"(\d+)"|<GCamera:MicroVideoOffset>(\d+)<')
_ITEM = re.compile(rb"<Container:Item\b(.*?)/?>", re.S)
_ATTR_SEMANTIC = re.compile(rb'Item:Semantic\s*=\s*"MotionPhoto"')
_ATTR_LENGTH = re.compile(rb'Item:Length\s*=\s*"(\d+)"')


def _mp4_extent(fh, start: int, file_size: int) -> int | None:
    """If an MP4 starts at `start`, how long it is (its top-level boxes, which must include moov)."""
    pos, seen_moov, seen_ftyp = start, False, False
    while pos + 8 <= file_size:
        fh.seek(pos)
        header = fh.read(16)
        size, kind = struct.unpack(">I4s", header[:8])
        if size == 1 and len(header) >= 16:
            size = struct.unpack(">Q", header[8:16])[0]
        elif size == 0:
            size = file_size - pos
        if size < 8 or pos + size > file_size or not all(32 <= c < 127 for c in kind):
            break
        if pos == start:
            if kind != b"ftyp":
                return None
            seen_ftyp = True
        seen_moov = seen_moov or kind == b"moov"
        pos += size
    return pos - start if seen_ftyp and seen_moov and pos > start else None


def find_motion_video(path: Path) -> tuple[int, int] | None:
    """(offset, length) of the video inside a motion photo, or None for an ordinary photo."""
    ext = os.path.splitext(str(path))[1].lower()
    if ext not in (".jpg", ".jpeg", ".heic", ".heif"):
        return None
    try:
        file_size = os.path.getsize(path)
        with open(path, "rb") as fh:
            head = fh.read(_HEAD_BYTES)
            candidates = []
            if ext in (".heic", ".heif"):
                # Samsung puts the video in an "mpvd" box at the end of the HEIC file.
                pos = 0
                while pos + 8 <= file_size:
                    fh.seek(pos)
                    size, kind = struct.unpack(">I4s", fh.read(8))
                    if size == 1:
                        size = struct.unpack(">Q", fh.read(8))[0]
                        body = pos + 16
                    else:
                        body = pos + 8
                    if size == 0:
                        size = file_size - pos
                    if size < 8:
                        break
                    if kind == b"mpvd":
                        candidates.append(body)
                        break
                    pos += size
            flagged = bool(_MOTION_FLAG.search(head))
            if flagged:
                for item in _ITEM.findall(head):
                    if _ATTR_SEMANTIC.search(item):
                        m = _ATTR_LENGTH.search(item)
                        if m:
                            candidates.append(file_size - int(m.group(1)))
                m = _MICRO_OFFSET.search(head)
                if m:
                    candidates.append(file_size - int(m.group(1) or m.group(2)))
            # Older Samsung phones: the video follows a "MotionPhoto_Data" marker near the end.
            fh.seek(max(0, file_size - 4))
            if fh.read(4) == b"SEFT" or flagged:
                marker = _find_in_file(fh, b"MotionPhoto_Data", file_size)
                if marker is not None:
                    candidates.append(marker + len(b"MotionPhoto_Data"))
            for start in candidates:
                if 0 < start < file_size:
                    length = _mp4_extent(fh, start, file_size)
                    if length:
                        return start, length
    except (OSError, struct.error, ValueError):
        return None
    return None


def _find_in_file(fh, needle: bytes, file_size: int, chunk: int = 1 << 20) -> int | None:
    """Position of the last `needle` in the file, searching from the end."""
    pos = file_size
    tail = b""
    while pos > 0:
        start = max(0, pos - chunk)
        fh.seek(start)
        data = fh.read(pos - start) + tail
        i = data.rfind(needle)
        if i >= 0:
            return start + i
        tail = data[:len(needle) - 1]
        pos = start
    return None


def video_codec(path: Path) -> str | None:
    """The codec of a video's picture (h264, hevc, ...), if ffprobe is installed."""
    ffprobe = shutil.which("ffprobe")
    if not ffprobe:
        return None
    try:
        out = subprocess.run(
            [ffprobe, "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
             "-of", "default=nw=1:nk=1", str(path)],
            capture_output=True, timeout=30, stdin=subprocess.DEVNULL, check=True,
        ).stdout
        return out.decode("ascii", "ignore").strip() or None
    except (subprocess.SubprocessError, OSError):
        return None


# Windows' web view plays HEVC only with an extra codec from the Microsoft Store, so short clips
# in HEVC (iPhone Live Photos, some Samsung motion photos) are converted once to H.264.
NEEDS_CONVERTING = {"hevc", "h265"}


def make_playable(src: Path, dst: Path) -> bool:
    """Write an H.264 copy of a short clip to dst if its codec may not play. False if not needed or not possible."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg or video_codec(src) not in NEEDS_CONVERTING:
        return False
    tmp = dst.with_name(dst.stem + ".tmp.mp4")
    cmd = [ffmpeg, "-v", "error", "-y", "-i", str(src), "-map", "0:v:0", "-map", "0:a:0?",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "23", "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-movflags", "+faststart", str(tmp)]
    try:
        subprocess.run(cmd, timeout=300, capture_output=True, stdin=subprocess.DEVNULL, check=True)
        os.replace(tmp, dst)
        return True
    except (subprocess.SubprocessError, OSError):
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def extract_motion_video(src: Path, offset: int, length: int, dst: Path) -> bool:
    """Copy a motion photo's video out to its own file (kept in the data folder)."""
    tmp = dst.with_name(dst.name + ".tmp")
    try:
        with open(src, "rb") as fh, open(tmp, "wb") as out:
            fh.seek(offset)
            left = length
            while left > 0:
                data = fh.read(min(left, 1 << 20))
                if not data:
                    raise OSError("the file is shorter than expected")
                out.write(data)
                left -= len(data)
        os.replace(tmp, dst)
        return True
    except OSError:
        try:
            tmp.unlink()
        except OSError:
            pass
        return False


def load_oriented(path: Path, max_side: int) -> Image.Image:
    """The picture the right way up, in RGB, no bigger than max_side. Raises if unreadable."""
    if is_raw(path):
        return _raw_picture(path, max_side)
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
        if is_raw(src):
            _raw_picture(src, size).save(tmp, "JPEG", quality=quality)
        else:
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
    info = _empty_info()
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
