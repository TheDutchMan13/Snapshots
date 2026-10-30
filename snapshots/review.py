"""Year in review: the best photos of a year, as a slideshow, an album or a video.

Choosing the photos (pick):
* Candidates are the year's photos that everyone can see (not archived, trashed or locked), one per stack,
  leaving out rejects and blurry or dark photos.
* Each gets a score: favourites, stars and picks count most, then named people, then being in a place
  with a name, then sharpness.
* Near-copies and bursts are left out: a photo that looks like one already chosen (or was taken within half a
  minute of one) is skipped.
* The year is spread out: each month gets a share by how many photos it has (the square root, so a month
  with a big trip doesn't swallow the rest).

The video (render_video) is made on this computer with Pillow and ffmpeg: a title, each photo slowly zooming,
with a crossfade to the next, and an end title. Photos are shown edited (turned and cropped) as in the app.
"""

import math
import os
import shutil
import subprocess
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

from . import analysis, db, libraries, media

SIZES = {"720p": (1280, 720), "1080p": (1920, 1080)}
FPS = 25
FADE = 0.7          # seconds of crossfade between photos
TITLE_SECONDS = 3.0


def years(conn) -> list[dict]:
    rows = conn.execute(
        f"SELECT substr(a.taken_at, 1, 4) AS year, COUNT(*) AS photos FROM assets a"
        f" WHERE a.kind = 'photo' AND a.trashed_at IS NULL AND a.archived_at IS NULL AND {db.NOT_LOCKED}"
        f" AND {db.LIVE_PART_HIDDEN} GROUP BY year ORDER BY year DESC").fetchall()
    return [{"year": int(r["year"]), "photos": r["photos"]} for r in rows if r["year"] and r["year"].isdigit()]


def _candidates(conn, cfg, year: int, user_id: int) -> list[dict]:
    rows = conn.execute(
        f"SELECT a.id, a.taken_at, a.dhash, a.sharpness, a.rating, a.flag, a.label, a.city, a.state, a.country,"
        f" (SELECT COUNT(*) FROM favorites fv WHERE fv.asset_id = a.id) AS favs,"
        f" (SELECT COUNT(*) FROM favorites fv WHERE fv.asset_id = a.id AND fv.user_id = ?) AS mine,"
        f" (SELECT COUNT(DISTINCT f.person_id) FROM faces f JOIN people p ON p.id = f.person_id"
        f"   WHERE f.asset_id = a.id AND p.name IS NOT NULL AND p.hidden = 0) AS named,"
        f" (SELECT COUNT(*) FROM faces f WHERE f.asset_id = a.id) AS faces"
        f" FROM assets a WHERE a.kind = 'photo' AND a.taken_at >= ? AND a.taken_at < ?"
        f" AND a.trashed_at IS NULL AND a.archived_at IS NULL AND {db.NOT_LOCKED} AND {db.LIVE_PART_HIDDEN}"
        f" AND {db.STACK_HIDDEN} AND a.flag != -1 AND NOT COALESCE({analysis.low_quality_sql(cfg)}, 0)",
        (user_id, f"{year:04d}-01-01", f"{year + 1:04d}-01-01")).fetchall()
    out = [dict(r) for r in rows]
    for r in out:
        r["t"] = datetime.fromisoformat(r["taken_at"]).timestamp()
    sharp = sorted(r["sharpness"] for r in out if r["sharpness"] is not None)
    top = sharp[int(len(sharp) * 0.9)] if sharp else 1.0
    for r in out:
        s = min(1.0, (r["sharpness"] or 0) / top) if top else 0
        r["score"] = (3.0 * bool(r["mine"]) + 1.5 * bool(r["favs"]) + 1.0 * r["rating"] + 1.5 * (r["flag"] == 1)
                      + 0.3 * bool(r["label"]) + 0.8 * min(r["named"], 3) + 0.25 * min(r["faces"], 4)
                      + 0.4 * bool(r["city"] or r["country"]) + 0.6 * s
                      + (r["id"] * 2654435761 % 1000) / 1e5)   # a steady tie-break
    return out


def _too_close(r: dict, chosen: list[dict]) -> bool:
    t = r["t"]
    for c in chosen:
        if abs(c["t"] - t) <= 30:
            return True
        if r["dhash"] is not None and c["dhash"] is not None and analysis.hamming(r["dhash"], c["dhash"]) <= 10:
            return True
    return False


def pick(conn, cfg, year: int, user_id: int, count: int = 60) -> dict:
    """The year's best photos, in the order they were taken, and a few facts for the titles."""
    rows = _candidates(conn, cfg, year, user_id)
    by_month: dict[str, list] = defaultdict(list)
    for r in rows:
        by_month[r["taken_at"][5:7]].append(r)
    weights = {m: math.sqrt(len(v)) for m, v in by_month.items()}
    total_w = sum(weights.values()) or 1
    chosen: list[dict] = []
    for m in sorted(by_month):
        quota = max(1, round(count * weights[m] / total_w))
        mine = []
        for r in sorted(by_month[m], key=lambda x: -x["score"]):
            if len(mine) >= quota:
                break
            if not _too_close(r, chosen + mine):
                mine.append(r)
        chosen += mine
    if len(chosen) > count:                      # rounding gave too many: drop the weakest
        chosen = sorted(chosen, key=lambda x: -x["score"])[:count]
    elif len(chosen) < count:                    # months with few photos left room: fill from the best
        taken = {c["id"] for c in chosen}
        for r in sorted(rows, key=lambda x: -x["score"]):
            if len(chosen) >= count:
                break
            if r["id"] not in taken and not _too_close(r, chosen):
                chosen.append(r)
    chosen.sort(key=lambda x: (x["taken_at"], x["id"]))
    ids = [c["id"] for c in chosen]
    places = Counter(r["city"] or r["country"] for r in rows if (r["city"] or r["country"]))
    people = Counter()
    if ids:
        for name, n in conn.execute(
            f"SELECT p.name, COUNT(DISTINCT f.asset_id) FROM faces f JOIN people p ON p.id = f.person_id"
            f" JOIN assets a ON a.id = f.asset_id WHERE p.name IS NOT NULL AND p.hidden = 0"
            f" AND a.taken_at >= ? AND a.taken_at < ? AND a.trashed_at IS NULL AND {db.NOT_LOCKED}"
            f" GROUP BY p.id", (f"{year:04d}-01-01", f"{year + 1:04d}-01-01")):
            people[name] = n
    all_photos = conn.execute(
        f"SELECT COUNT(*) FROM assets a WHERE a.taken_at >= ? AND a.taken_at < ? AND a.trashed_at IS NULL"
        f" AND {db.NOT_LOCKED} AND {db.LIVE_PART_HIDDEN}", (f"{year:04d}-01-01", f"{year + 1:04d}-01-01")).fetchone()[0]
    return {"year": year, "ids": ids, "photos": all_photos, "candidates": len(rows),
            "places": [p for p, _ in places.most_common(5)], "people": [p for p, _ in people.most_common(5)],
            "months": len(by_month)}


# ---------------------------------------------------------------- the video

def _font(size: int, bold: bool = False):
    names = (["segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf"] if bold
             else ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf"])
    for name in names:
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size=size)
    except TypeError:   # an old Pillow
        return ImageFont.load_default()


def _fit_text(draw, text: str, font_size: int, max_width: int, bold: bool):
    while font_size > 12:
        font = _font(font_size, bold)
        if draw.textlength(text, font=font) <= max_width:
            return font
        font_size = int(font_size * 0.9)
    return _font(12, bold)


def title_card(size, title: str, lines: list[str], background: Image.Image | None = None) -> Image.Image:
    w, h = size
    if background is not None:
        img = _cover(background, size).filter(ImageFilter.GaussianBlur(radius=w // 60))
        img = Image.blend(img, Image.new("RGB", size, (0, 0, 0)), 0.55)
    else:
        img = Image.new("RGB", size, (16, 20, 24))
    d = ImageDraw.Draw(img)
    big = _fit_text(d, title, h // 5, int(w * 0.85), True)
    tw = d.textlength(title, font=big)
    y = int(h * 0.36)
    d.text(((w - tw) / 2, y), title, font=big, fill=(255, 255, 255))
    y += int(h // 5 * 1.25)
    for line in lines:
        f = _fit_text(d, line, h // 22, int(w * 0.85), False)
        lw = d.textlength(line, font=f)
        d.text(((w - lw) / 2, y), line, font=f, fill=(225, 230, 235))
        y += int(h / 22 * 1.6)
    return img


def _cover(img: Image.Image, size) -> Image.Image:
    """Scale and crop img to fill size exactly."""
    w, h = size
    scale = max(w / img.width, h / img.height)
    nw, nh = max(w, round(img.width * scale)), max(h, round(img.height * scale))
    big = img.resize((nw, nh), Image.BILINEAR)
    left, top = (nw - w) // 2, (nh - h) // 2
    return big.crop((left, top, left + w, top + h))


def _stage(img: Image.Image, size, margin: float = 1.12) -> Image.Image:
    """A picture a little bigger than the frame (room for the slow zoom). Photos that would lose too much
    when cropped to the frame (portrait photos, say) are shown whole on a blurred copy of themselves."""
    w, h = int(size[0] * margin), int(size[1] * margin)
    frame_ratio, ratio = w / h, img.width / img.height
    if 0.74 * frame_ratio <= ratio <= 1.35 * frame_ratio:   # 4:3 and 3:2 photos fill the frame
        return _cover(img, (w, h))
    back = _cover(img, (w, h)).filter(ImageFilter.GaussianBlur(radius=w // 40))
    back = Image.blend(back, Image.new("RGB", (w, h), (0, 0, 0)), 0.35)
    scale = min(w / img.width, h / img.height) * 0.94
    fg = img.resize((max(1, round(img.width * scale)), max(1, round(img.height * scale))), Image.LANCZOS)
    back.paste(fg, ((w - fg.width) // 2, (h - fg.height) // 2))
    return back


def _zoom(stage: Image.Image, size, t: float, direction: int) -> Image.Image:
    """One frame of the slow zoom (t from 0 to 1): in for even photos, out for odd ones, drifting a little."""
    w, h = size
    sw, sh = stage.size
    z = t if direction % 2 == 0 else 1 - t
    scale = 1.0 + (sw / w - 1.0) * (1 - z)        # how much of the stage shows: all of it .. just the frame
    cw, ch = min(sw, w * scale), min(sh, h * scale)
    dx = max(0.0, (sw - cw) * (0.5 + 0.3 * math.sin(direction * 1.7) * (t - 0.5)))
    dy = max(0.0, (sh - ch) * (0.5 + 0.3 * math.cos(direction * 1.3) * (t - 0.5)))
    dx, dy = min(dx, sw - cw), min(dy, sh - ch)
    return stage.resize(size, Image.BILINEAR, box=(dx, dy, dx + cw, dy + ch))


def _frames(stages, size, seconds: float, intro: Image.Image, outro: Image.Image):
    """Every frame of the video, as RGB bytes. stages yields one photo's stage at a time (only one is in memory)."""
    black = Image.new("RGB", size, (0, 0, 0))
    n_title = int(TITLE_SECONDS * FPS)
    fade = int(FADE * FPS)
    for i in range(n_title):                      # the title, out of black
        yield Image.blend(black, intro, min(1.0, i / fade)).tobytes()
    per = int(seconds * FPS)
    prev_last = intro
    for k, stage in enumerate(stages):
        for i in range(per):
            frame = _zoom(stage, size, i / max(1, per - 1), k)
            if i < fade:
                frame = Image.blend(prev_last, frame, i / fade)
            yield frame.tobytes()
        prev_last = _zoom(stage, size, 1.0, k)
    for i in range(n_title):                      # the end title, into black
        frame = Image.blend(prev_last, outro, i / fade) if i < fade else outro
        if i >= n_title - fade:
            frame = Image.blend(frame, black, (i - (n_title - fade)) / fade)
        yield frame.tobytes()


RENDER_LOCK = threading.BoundedSemaphore(1)   # one video at a time: each holds pictures in memory


def _load(cfg, row, size):
    full = libraries.full_path(cfg, row["path"])
    try:
        return media.render_edited(full, media.parse_edits(row["edits"]), int(size[0] * 1.3))
    except Exception:
        return None   # unreadable: left out


def render_video(cfg, rows: list, dest: Path, title: str, lines: list[str], size_name: str = "720p",
                 seconds: float = 4.0, progress=None, should_stop=None) -> dict:
    """Make the MP4. rows: asset rows (path, edits). Returns facts about it."""
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("Making a video needs ffmpeg.")
    if not RENDER_LOCK.acquire(blocking=False):
        raise RuntimeError("Another video is being made. Try again when it's done.")
    try:
        return _render(cfg, ffmpeg, rows, dest, title, lines, size_name, seconds, progress, should_stop)
    finally:
        RENDER_LOCK.release()


def _render(cfg, ffmpeg, rows, dest, title, lines, size_name, seconds, progress, should_stop) -> dict:
    size = SIZES.get(size_name, SIZES["720p"])
    seconds = max(2.0, min(10.0, float(seconds)))
    first = None
    for row in rows:                      # the first readable photo, for the title's background
        first = _load(cfg, row, size)
        if first is not None:
            break
    if first is None:
        raise RuntimeError("None of the photos could be read.")
    intro = title_card(size, title, lines, first)
    outro = title_card(size, title, ["Made with Snapshots"], None)
    del first
    per = int(seconds * FPS)
    estimate = int(TITLE_SECONDS * FPS) * 2 + per * len(rows)
    shown = []

    def stages():
        for row in rows:
            img = _load(cfg, row, size)
            if img is not None:
                shown.append(row["id"])
                yield _stage(img, size)

    encoder = media.pick_encoder(cfg.video_encoder)
    plans = [encoder] + (["libx264"] if encoder != "libx264" else [])
    tmp = dest.with_name(dest.stem + ".tmp.mp4")
    for enc in plans:
        shown.clear()
        cmd = [ffmpeg, "-v", "error", "-y", "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", f"{size[0]}x{size[1]}",
               "-r", str(FPS), "-i", "-", *media._ENCODER_ARGS[enc], "-pix_fmt", "yuv420p",
               "-movflags", "+faststart", str(tmp)]
        proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        ok = True
        frames = 0
        try:
            for i, frame in enumerate(_frames(stages(), size, seconds, intro, outro)):
                if should_stop and should_stop():
                    raise InterruptedError("stopped")
                proc.stdin.write(frame)
                frames = i + 1
                if progress and i % FPS == 0:
                    progress(min(0.99, i / estimate))
            proc.stdin.close()
            proc.wait(timeout=600)
        except (BrokenPipeError, OSError):
            ok = False
        except BaseException:
            proc.kill()
            proc.wait()
            tmp.unlink(missing_ok=True)
            raise
        if ok and proc.returncode == 0 and tmp.is_file() and tmp.stat().st_size > 0:
            os.replace(tmp, dest)
            return {"photos": len(shown), "seconds": round(frames / FPS, 1), "size": size_name, "encoder": enc}
        try:
            proc.kill()
            proc.wait()
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
    raise RuntimeError("ffmpeg could not make the video.")
