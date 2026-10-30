"""Exporting albums and selections: a zip of the files, or a PDF photo book.

Zip: every photo and video, named by date ("2024-07-04 18.22.10 IMG_1234.jpg") so they sort in the order
they were taken anywhere you unpack them. Edited photos are saved edited (as JPEG), unless you ask for the
originals. The zip is made while it downloads, so it needs no room on the disk first.

PDF photo book: a title page, then 1, 2, 4 or 6 photos a page with their date, place and description,
on Letter or A4 paper, at 200 dots per inch (sharp on screen and good for printing at home). Photos only:
videos are left out.
"""

import io
import logging
import os
import zipfile
from datetime import datetime
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

from . import libraries, media

log = logging.getLogger("snapshots.exports")

DPI = 200
PAPER = {"letter": (8.5, 11.0), "a4": (8.27, 11.69)}
LAYOUTS = {1: (1, 1), 2: (1, 2), 4: (2, 2), 6: (2, 3)}   # columns, rows
TIME_FMT = "%Y-%m-%dT%H:%M:%S"


# ------------------------------------------------------------------ zip


def zip_name(row, edited: bool) -> str:
    stem, ext = os.path.splitext(row["filename"])
    try:
        when = datetime.strptime(row["taken_at"], TIME_FMT).strftime("%Y-%m-%d %H.%M.%S") + " "
    except (TypeError, ValueError):
        when = ""
    if edited:
        return f"{when}{stem} (edited).jpg"
    return f"{when}{stem}{ext}"


class _Sink:
    """A write-only file that collects what zipfile writes, so it can be sent bit by bit."""

    def __init__(self):
        self.parts: list[bytes] = []

    def write(self, data) -> int:
        self.parts.append(bytes(data))
        return len(data)

    def flush(self) -> None:
        pass

    def take(self) -> bytes:
        out = b"".join(self.parts)
        self.parts.clear()
        return out


def zip_stream(cfg, rows, edited: bool = True, videos: bool = True):
    """The zip, as a stream of bytes. Files that can't be read are listed in "Not included.txt"."""
    sink = _Sink()
    used: set[str] = set()
    missing: list[str] = []
    with zipfile.ZipFile(sink, "w", zipfile.ZIP_STORED, allowZip64=True) as zf:
        for row in rows:
            if row["kind"] == "video" and not videos:
                continue
            full = libraries.full_path(cfg, row["path"])
            if full is None or not full.is_file():
                missing.append(row["filename"])
                continue
            edits = media.parse_edits(row["edits"]) if edited and row["edits"] and row["kind"] == "photo" else None
            name = zip_name(row, bool(edits))
            stem, ext = os.path.splitext(name)
            n = 2
            while name.lower() in used:
                name, n = f"{stem} ({n}){ext}", n + 1
            used.add(name.lower())
            try:
                when = datetime.strptime(row["taken_at"], TIME_FMT)
                stamp = (max(1980, when.year), when.month, when.day, when.hour, when.minute, when.second)
            except (TypeError, ValueError):
                stamp = (1980, 1, 1, 0, 0, 0)
            info = zipfile.ZipInfo(name, stamp)
            info.compress_type = zipfile.ZIP_STORED
            try:
                if edits:
                    buf = io.BytesIO()
                    media.render_edited(full, edits, 0).save(buf, "JPEG", quality=93)
                    with zf.open(info, "w", force_zip64=True) as out:
                        out.write(buf.getvalue())
                    yield sink.take()
                else:
                    with open(full, "rb") as src, zf.open(info, "w", force_zip64=True) as out:
                        while chunk := src.read(1 << 20):
                            out.write(chunk)
                            yield sink.take()
            except Exception as exc:
                log.warning("left %s out of an export: %s", full, exc)
                missing.append(row["filename"])
            yield sink.take()
        if missing:
            zf.writestr("Not included.txt", "These could not be read, so they are not in this zip:\r\n"
                        + "\r\n".join(missing) + "\r\n")
    yield sink.take()


# ------------------------------------------------------------------ PDF photo book


def _font(size: int, bold: bool = False):
    for name in (("segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf") if bold
                 else ("segoeui.ttf", "arial.ttf", "DejaVuSans.ttf")):
        try:
            return ImageFont.truetype(name, size)
        except OSError:
            continue
    return ImageFont.load_default(size=size)


def _fit_text(draw, text: str, font, width: int) -> str:
    if draw.textlength(text, font=font) <= width:
        return text
    while text and draw.textlength(text + "…", font=font) > width:
        text = text[:-1]
    return text.rstrip() + "…"


def caption_lines(row) -> list[str]:
    try:
        when = datetime.strptime(row["taken_at"], TIME_FMT)
        date = f"{when.day} {when:%B %Y}"
    except (TypeError, ValueError):
        date = ""
    place = ", ".join(x for x in (row["city"], row["state"] if not row["city"] else None, row["country"]) if x)
    first = " · ".join(x for x in (date, place) if x)
    lines = [first] if first else []
    if row["description"]:
        lines.append(" ".join(row["description"].split()))
    return lines


class PdfWriter:
    """A small PDF writer: each page is one JPEG picture covering the page. Pages go straight to the file,
    so a book of hundreds of pages needs little memory."""

    def __init__(self, path: Path, width_pt: float, height_pt: float, title: str):
        self.fh = open(path, "wb")
        self.w, self.h = width_pt, height_pt
        self.title = title
        self.offsets: dict[int, int] = {}
        self.pages: list[int] = []
        self.next_id = 4          # 1 catalog, 2 pages, 3 document info
        self.fh.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")

    def _obj(self, num: int, body: bytes, stream: bytes | None = None) -> None:
        self.offsets[num] = self.fh.tell()
        self.fh.write(f"{num} 0 obj\n".encode() + body)
        if stream is not None:
            self.fh.write(b"\nstream\n" + stream + b"\nendstream")
        self.fh.write(b"\nendobj\n")

    def add_page(self, jpeg: bytes, px_w: int, px_h: int) -> None:
        img, content, page = self.next_id, self.next_id + 1, self.next_id + 2
        self.next_id += 3
        self._obj(img, (f"<< /Type /XObject /Subtype /Image /Width {px_w} /Height {px_h} /ColorSpace /DeviceRGB"
                        f" /BitsPerComponent 8 /Filter /DCTDecode /Length {len(jpeg)} >>").encode(), jpeg)
        draw = f"q {self.w:.2f} 0 0 {self.h:.2f} 0 0 cm /Im0 Do Q".encode()
        self._obj(content, f"<< /Length {len(draw)} >>".encode(), draw)
        self._obj(page, (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {self.w:.2f} {self.h:.2f}]"
                         f" /Resources << /XObject << /Im0 {img} 0 R >> >> /Contents {content} 0 R >>").encode())
        self.pages.append(page)

    def close(self) -> None:
        kids = " ".join(f"{p} 0 R" for p in self.pages)
        self._obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(self.pages)} >>".encode())
        self._obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
        title_bytes = b"\xfe\xff" + self.title.encode("utf-16-be")
        self._obj(3, b"<< /Title <" + title_bytes.hex().encode() + b"> /Producer (Snapshots) >>")
        xref = self.fh.tell()
        count = self.next_id
        self.fh.write(f"xref\n0 {count}\n0000000000 65535 f \n".encode())
        for num in range(1, count):
            self.fh.write(f"{self.offsets.get(num, 0):010d} 00000 n \n".encode())
        self.fh.write(f"trailer\n<< /Size {count} /Root 1 0 R /Info 3 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
        self.fh.close()


def _page_jpeg(page: Image.Image) -> bytes:
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=88, optimize=True)
    return buf.getvalue()


def build_pdf(cfg, rows, dest: Path, title: str, subtitle: str, paper: str = "letter", per_page: int = 4,
              progress=None, should_stop=None) -> dict:
    """Write a photo book to dest. Returns {"pages", "photos", "missing"}."""
    inches = PAPER.get(paper, PAPER["letter"])
    W, H = round(inches[0] * DPI), round(inches[1] * DPI)
    cols, nrows = LAYOUTS.get(per_page, LAYOUTS[4])
    margin, gap = DPI // 2, DPI // 6
    caption_font, small_font = _font(DPI // 7), _font(DPI // 8)
    line_h = DPI // 5
    cap_h = line_h * 2 + DPI // 20
    photos = [r for r in rows if r["kind"] == "photo"]
    tmp = dest.with_name(dest.name + ".part")
    pdf = PdfWriter(tmp, inches[0] * 72, inches[1] * 72, title)
    missing = 0
    try:
        page = Image.new("RGB", (W, H), "white")
        d = ImageDraw.Draw(page)
        d.text((W // 2, H * 2 // 5), _fit_text(d, title, _font(DPI // 2, True), W - 2 * margin),
               font=_font(DPI // 2, True), fill=(30, 30, 30), anchor="mm")
        if subtitle:
            d.text((W // 2, H * 2 // 5 + DPI * 2 // 3), subtitle, font=_font(DPI // 5), fill=(90, 90, 90), anchor="mm")
        d.text((W // 2, H - margin), f"{len(photos)} photos", font=small_font, fill=(140, 140, 140), anchor="mm")
        pdf.add_page(_page_jpeg(page), W, H)

        cell_w = (W - 2 * margin - (cols - 1) * gap) // cols
        cell_h = (H - 2 * margin - (nrows - 1) * gap) // nrows
        per = cols * nrows
        for start in range(0, len(photos), per):
            if should_stop and should_stop():
                raise InterruptedError("stopped")
            page = Image.new("RGB", (W, H), "white")
            d = ImageDraw.Draw(page)
            for k, row in enumerate(photos[start:start + per]):
                x0 = margin + (k % cols) * (cell_w + gap)
                y0 = margin + (k // cols) * (cell_h + gap)
                box_w, box_h = cell_w, cell_h - cap_h
                full = libraries.full_path(cfg, row["path"])
                try:
                    if full is None or not full.is_file():
                        raise OSError("missing")
                    pic = media.render_edited(full, media.parse_edits(row["edits"]), max(box_w, box_h))
                    pic = media._to_rgb(pic)
                    scale = min(box_w / pic.width, box_h / pic.height)   # small photos are enlarged to fit too
                    pic = pic.resize((max(1, round(pic.width * scale)), max(1, round(pic.height * scale))), Image.LANCZOS)
                    page.paste(pic, (x0 + (box_w - pic.width) // 2, y0 + (box_h - pic.height) // 2))
                    bottom = y0 + (box_h + pic.height) // 2
                except Exception:
                    missing += 1
                    d.rectangle((x0, y0, x0 + box_w, y0 + box_h), outline=(200, 200, 200), width=2)
                    d.text((x0 + box_w // 2, y0 + box_h // 2), row["filename"], font=small_font,
                           fill=(140, 140, 140), anchor="mm")
                    bottom = y0 + box_h
                for i, line in enumerate(caption_lines(row)[:2]):
                    d.text((x0 + box_w // 2, bottom + DPI // 20 + i * line_h),
                           _fit_text(d, line, caption_font, box_w), font=caption_font,
                           fill=(60, 60, 60) if i == 0 else (110, 110, 110), anchor="ma")
            pdf.add_page(_page_jpeg(page), W, H)
            if progress:
                progress(min(1.0, (start + per) / max(1, len(photos))))
        pdf.close()
        os.replace(tmp, dest)
    except BaseException:
        try:
            pdf.fh.close()
        except Exception:
            pass
        tmp.unlink(missing_ok=True)
        raise
    return {"pages": len(pdf.pages), "photos": len(photos), "missing": missing}
