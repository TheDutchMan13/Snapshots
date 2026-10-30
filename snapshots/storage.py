"""Storage templates: which folder an uploaded photo or video is filed in.

A template is a folder pattern inside the main library, such as "Uploads/{year}/{month}" (the default)
or "Photos/{year}/{year}-{month}-{day}". The date is when the photo was taken (from the file), or the
day it arrived if the file doesn't say. Only new uploads are filed this way: files already in the
library are never moved.
"""

import re
from datetime import datetime

MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September",
          "October", "November", "December"]

VARIABLES = {
    "year": "2026",
    "month": "10",
    "monthname": "October",
    "day": "30",
    "date": "2026-10-30",
    "camera": "Pixel 9",
    "type": "Photos (or Videos)",
    "user": "who uploaded it",
}

_VAR = re.compile(r"\{([^{}]*)\}")
_BAD_CHARS = re.compile(r'[<>:"|?*\x00-\x1f\\]')
_RESERVED = {"con", "prn", "aux", "nul", *(f"com{i}" for i in range(1, 10)), *(f"lpt{i}" for i in range(1, 10))}
MAX_LEN = 200


def template_problem(template: str) -> str | None:
    """None if the template can be used, otherwise what is wrong with it."""
    t = (template or "").strip()
    if not t:
        return "Choose a folder pattern, such as Uploads/{year}/{month}."
    if len(t) > MAX_LEN:
        return "That folder pattern is too long."
    if t.startswith(("/", "\\")) or re.match(r"^[A-Za-z]:", t):
        return "The folder pattern must be a folder inside the library, not a drive or full path."
    for name in _VAR.findall(t):
        if name not in VARIABLES:
            return f"Unknown name {{{name}}}. You can use: " + ", ".join("{" + v + "}" for v in VARIABLES) + "."
    if "{" in _VAR.sub("", t) or "}" in _VAR.sub("", t):
        return "A { or } is missing its partner."
    for part in re.split(r"[/\\]", t):
        plain = _VAR.sub("x", part).strip()
        if not plain:
            return "The folder pattern has an empty folder name (two slashes in a row?)."
        if plain in (".", "..") or plain.startswith("."):
            return "Folder names in the pattern can't start with a dot."
        if _BAD_CHARS.search(_VAR.sub("", part)):
            return 'Folder names can\'t contain < > : " | ? * or backslashes.'
    return None


def _clean(part: str) -> str:
    part = _BAD_CHARS.sub("_", part).replace("/", "_").strip().rstrip(". ").lstrip(".")
    if part.split(".")[0].lower() in _RESERVED:
        part = "_" + part
    return part[:80] or "_"


def folder_for(template: str, taken: datetime, camera: str | None = None, kind: str = "photo",
               user: str | None = None) -> str:
    """The folder (relative to the library, forward slashes) the template gives for one file."""
    values = {
        "year": f"{taken:%Y}", "month": f"{taken:%m}", "monthname": MONTHS[taken.month - 1],
        "day": f"{taken:%d}", "date": f"{taken:%Y-%m-%d}",
        "camera": (camera or "").strip() or "Unknown camera",
        "type": "Videos" if kind == "video" else "Photos",
        "user": (user or "").strip() or "Unknown",
    }
    parts = []
    for part in re.split(r"[/\\]", template.strip()):
        filled = _VAR.sub(lambda m: values.get(m.group(1), ""), part)
        parts.append(_clean(filled))
    return "/".join(parts)


def examples(template: str) -> list[str]:
    """What the template makes of two sample files, for the settings page."""
    return [folder_for(template, datetime(2026, 10, 30, 14, 5), "Pixel 9", "photo", "hans"),
            folder_for(template, datetime(2019, 7, 4, 9, 30), None, "video", "hans")]
