"""Dates in search words: "2019", "june 2021", "last summer", "christmas 2023", "3 years ago"...

`parse` takes what someone typed and splits it into date conditions and the rest of the words. Every
condition must hold (they are ANDed, like search words). A condition is a list of time ranges, any of
which may match, or a set of months (any year), or a set of days of the year (any year):

    {"ranges": [("2023-12-24T00:00:00", "2023-12-26T23:59:59")]}
    {"months": [6, 7, 8]}
    {"days": [(10, 31)]}

Seasons are the northern ones (summer is June to August). A bare season ("summer") means that season
in any year; "last summer" and "summer 2022" mean one of them.
"""

import calendar
import re
from datetime import date, datetime, timedelta

_MONTHS: dict[str, int] = {}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i
_MONTHS["sept"] = 9

SEASONS = {"spring": (3, 4, 5), "summer": (6, 7, 8), "fall": (9, 10, 11), "autumn": (9, 10, 11), "winter": (12, 1, 2)}
_NUMBERS = {w: i for i, w in enumerate("zero one two three four five six seven eight nine ten eleven twelve".split())}
_NUMBERS.update({"a": 1, "an": 1})

# Words that carry nothing when looking for text in names, places and descriptions.
STOPWORDS = {"a", "an", "the", "of", "in", "on", "at", "from", "during", "with", "and", "my", "our", "to", "for",
             "photos", "photo", "pictures", "picture", "pics", "videos", "video", "taken", "around"}

_YEAR = r"(?:19|20)\d{2}"
FIRST_YEAR = 1970


def _range(start: date, end: date) -> tuple[str, str]:
    return (f"{start.isoformat()}T00:00:00", f"{end.isoformat()}T23:59:59")


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def _season(name: str, year: int) -> tuple[str, str]:
    months = SEASONS[name]
    if name == "winter":                       # December of `year` to February of the next
        return _range(date(year, 12, 1), _month_end(year + 1, 2))
    return _range(date(year, months[0], 1), _month_end(year, months[-1]))


def _easter(year: int) -> date:
    """Western Easter Sunday (the Anonymous Gregorian algorithm)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7  # noqa: E741
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = (h + l - 7 * m + 114) % 31 + 1
    return date(year, month, day)


def _thanksgiving(year: int) -> date:
    """The fourth Thursday of November (in the United States)."""
    first = date(year, 11, 1)
    return first + timedelta(days=(3 - first.weekday()) % 7 + 21)


def _memorial(year: int) -> date:
    last = date(year, 5, 31)
    return last - timedelta(days=last.weekday())


def _labor(year: int) -> date:
    first = date(year, 9, 1)
    return first + timedelta(days=(0 - first.weekday()) % 7)


# Holidays: fixed days (month, day, days before, days after) or a function of the year.
_FIXED = {
    "christmas": (12, 24, 0, 2), "xmas": (12, 24, 0, 2),
    "christmas eve": (12, 24, 0, 0), "new year's eve": (12, 31, 0, 0), "new years eve": (12, 31, 0, 0),
    "halloween": (10, 31, 0, 0), "valentine's day": (2, 14, 0, 0), "valentines day": (2, 14, 0, 0),
    "valentine's": (2, 14, 0, 0), "valentines": (2, 14, 0, 0),
    "fourth of july": (7, 4, 0, 0), "4th of july": (7, 4, 0, 0), "independence day": (7, 4, 0, 0),
    "st patrick's day": (3, 17, 0, 0), "st patricks day": (3, 17, 0, 0), "cinco de mayo": (5, 5, 0, 0),
}
_MOVING = {"easter": (_easter, 1, 1), "thanksgiving": (_thanksgiving, 0, 3),
           "memorial day": (_memorial, 2, 0), "labor day": (_labor, 2, 0)}


def _fixed_range(spec, year: int) -> tuple[str, str]:
    m, d, before, after = spec
    day = date(year, m, d)
    return _range(day - timedelta(days=before), day + timedelta(days=after))


def _moving_range(spec, year: int) -> tuple[str, str]:
    fn, before, after = spec
    day = fn(year)
    return _range(day - timedelta(days=before), day + timedelta(days=after))


def _new_year(year: int) -> tuple[str, str]:
    """New Year's: the evening of 31 December before `year` through 1 January."""
    return (f"{year - 1:04d}-12-31T18:00:00", f"{year:04d}-01-01T23:59:59")


_HOLIDAY_NAMES = sorted(list(_FIXED) + list(_MOVING) + ["new year's", "new years", "new year"], key=len, reverse=True)
_HOLIDAY = "|".join(re.escape(h) for h in _HOLIDAY_NAMES)
_UNIT = r"(day|week|month|year)s?"
_NUM = r"(\d{1,2}|" + "|".join(_NUMBERS) + r")"
_MONTH = "|".join(sorted(_MONTHS, key=len, reverse=True))
_SEASON = "|".join(SEASONS)

_PATTERNS = [
    ("holiday", re.compile(rf"\b(?:(this|last)\s+)?({_HOLIDAY})(?:\s+({_YEAR}))?\b")),
    ("ago", re.compile(rf"\b{_NUM}\s+{_UNIT}\s+ago\b")),
    ("relative", re.compile(rf"\b(this|last|past)\s+(weekend|week|month|year)\b")),
    ("lastmonth", re.compile(rf"\b(this|last)\s+({_MONTH})\b")),
    ("season", re.compile(rf"\b(?:(this|last)\s+)?({_SEASON})(?:\s+(?:of\s+)?({_YEAR}))?\b")),
    ("day", re.compile(r"\b(today|yesterday)\b")),
    ("between", re.compile(rf"\b(?:between|from)\s+({_YEAR})\s+(?:and|to|-)\s+({_YEAR})\b|\b({_YEAR})\s*-\s*({_YEAR})\b")),
    ("isodate", re.compile(r"(?<![\w-])((?:19|20)\d{2})(?:-(\d{2})(?:-(\d{2}))?)?(?![\w-])")),
    ("month", re.compile(rf"\b({_MONTH})\b")),
]


def _number(word: str) -> int:
    return int(word) if word.isdigit() else _NUMBERS[word]


def _condition(kind: str, m: re.Match, today: date) -> dict | None:
    if kind == "holiday":
        which, name, year = m.group(1), m.group(2), m.group(3)
        years: list[int]
        if year:
            years = [int(year)]
        elif which == "this":
            years = [today.year]
        elif which == "last":
            # the most recent one that has already happened
            probe = _holiday_ranges(name, [today.year])[0]
            years = [today.year if probe[0][:10] <= today.isoformat() else today.year - 1]
        else:
            if name in _FIXED and _FIXED[name][2:] == (0, 0):
                spec = _FIXED[name]
                return {"days": [(spec[0], spec[1])]}
            if name in ("new year's", "new years", "new year"):
                return {"days": [(12, 31), (1, 1)]}
            years = list(range(FIRST_YEAR, today.year + 1))
        return {"ranges": _holiday_ranges(name, years)}
    if kind == "ago":
        n, unit = _number(m.group(1)), m.group(2)
        if unit == "day":
            d = today - timedelta(days=n)
            return {"ranges": [_range(d, d)]}
        if unit == "week":
            start = today - timedelta(days=today.weekday() + 7 * n)
            return {"ranges": [_range(start, start + timedelta(days=6))]}
        if unit == "month":
            y, mo = divmod(today.year * 12 + today.month - 1 - n, 12)
            return {"ranges": [_range(date(y, mo + 1, 1), _month_end(y, mo + 1))]}
        y = today.year - n
        return {"ranges": [_range(date(y, 1, 1), date(y, 12, 31))]}
    if kind == "relative":
        which, unit = m.group(1), m.group(2)
        if which == "past":   # the last 7 days, 31 days, 365 days, up to today
            days = {"weekend": 3, "week": 7, "month": 31, "year": 365}[unit]
            return {"ranges": [_range(today - timedelta(days=days), today)]}
        back = which == "last"
        if unit == "weekend":
            saturday = today - timedelta(days=(today.weekday() - 5) % 7)   # this weekend's (or the last) Saturday
            if back and today.weekday() >= 5:
                saturday -= timedelta(days=7)
            if not back and today.weekday() < 5:
                saturday += timedelta(days=7)
            return {"ranges": [_range(saturday, saturday + timedelta(days=1))]}
        if unit == "week":
            start = today - timedelta(days=today.weekday() + (7 if back else 0))
            return {"ranges": [_range(start, start + timedelta(days=6))]}
        if unit == "month":
            y, mo = (today.year, today.month) if not back else divmod(today.year * 12 + today.month - 2, 12)
            if back:
                mo += 1
            return {"ranges": [_range(date(y, mo, 1), _month_end(y, mo))]}
        y = today.year - (1 if back else 0)
        return {"ranges": [_range(date(y, 1, 1), date(y, 12, 31))]}
    if kind == "lastmonth":
        which, mo = m.group(1), _MONTHS[m.group(2)]
        y = today.year
        if which == "last" and mo >= today.month:
            y -= 1
        return {"ranges": [_range(date(y, mo, 1), _month_end(y, mo))]}
    if kind == "season":
        which, name, year = m.group(1), m.group(2), m.group(3)
        if year:
            return {"ranges": [_season(name, int(year))]}
        if which:
            return {"ranges": [_season(name, _last_or_this_season(name, today, which))]}
        return {"months": list(SEASONS[name])}
    if kind == "day":
        d = today if m.group(1) == "today" else today - timedelta(days=1)
        return {"ranges": [_range(d, d)]}
    if kind == "between":
        a, b = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4))
        a, b = sorted((int(a), int(b)))
        return {"ranges": [_range(date(a, 1, 1), date(b, 12, 31))]}
    if kind == "isodate":
        y, mo, d = int(m.group(1)), m.group(2), m.group(3)
        try:
            if d:
                day = date(y, int(mo), int(d))
                return {"ranges": [_range(day, day)]}
            if mo:
                return {"ranges": [_range(date(y, int(mo), 1), _month_end(y, int(mo)))]}
        except ValueError:
            return None
        return {"ranges": [_range(date(y, 1, 1), date(y, 12, 31))]}
    if kind == "month":
        return {"months": [_MONTHS[m.group(1)]]}
    return None


def _last_or_this_season(name: str, today: date, which: str) -> int:
    """The year the meant season starts in."""
    months = SEASONS[name]
    if name == "winter":
        # The winter we are in (or the next one, from March): it starts in December.
        current = today.year - 1 if today.month <= 2 else today.year
        return current if which == "this" else current - 1
    if which == "this":
        return today.year
    # last: the most recent one that is over (or the one before, if we are in it now)
    return today.year if today.month > months[-1] else today.year - 1


def _holiday_ranges(name: str, years: list[int]) -> list[tuple[str, str]]:
    out = []
    for y in years:
        if name in ("new year's", "new years", "new year"):
            out.append(_new_year(y))
        elif name in _FIXED:
            out.append(_fixed_range(_FIXED[name], y))
        else:
            out.append(_moving_range(_MOVING[name], y))
    return out


def _parse(text: str, today: date | None = None):
    today = today or date.today()
    norm = " ".join((text or "").lower().replace("’", "'").split())
    rest = " " + norm + " "
    ctext = rest                  # for search by content: only firm dates are taken out
    conditions: list[dict] = []
    used: list[str] = []
    kept: list[str] = []
    for kind, pattern in _PATTERNS:
        while True:
            m = pattern.search(rest)
            if not m:
                break
            cond = _condition(kind, m, today)
            phrase = m.group(0).strip()
            if cond is None:      # "2023-02-30": not a date after all, so it stays a word
                kept.append(phrase)
                rest = rest[:m.start()] + f" \x00{len(kept) - 1}\x00 " + rest[m.end():]
                continue
            _soften(kind, m, cond)
            conditions.append(cond)
            used.append(phrase)
            rest = rest[:m.start()] + " " + rest[m.end():]
            if not cond.get("soft") or kind == "month":    # "may" says nothing about what a photo shows
                ctext = ctext.replace(f" {phrase} ", " ", 1)
    rest = re.sub(r"\x00(\d+)\x00", lambda k: kept[int(k.group(1))], rest)
    return conditions, " ".join(rest.split()), " ".join(used), " ".join(ctext.split())


def _soften(kind: str, m: re.Match, cond: dict) -> None:
    """Holidays, bare seasons and bare month names are also ordinary words: "christmas tree" is a tree
    in December as much as one on 25 December, and "fall leaves" is about leaves. These conditions are
    "soft": a word search takes the date or the word, and search by content keeps the word (and only
    keeps a year that was given, as in "christmas 2023")."""
    if kind == "holiday":
        cond["soft"], cond["word"] = True, m.group(2)
        ranges = cond.get("ranges") or []
        years = {r[0][:4] for r in ranges}
        if len(years) == 1 and len(ranges) == 1:
            y = int(next(iter(years)))
            cond["firm"] = {"ranges": [(f"{y:04d}-01-01T00:00:00", f"{y:04d}-12-31T23:59:59")]}
            if ranges[0][0][:4] != ranges[0][1][:4]:      # New Year's runs over two years
                cond["firm"] = {"ranges": [(ranges[0][0][:4] + "-12-01T00:00:00", ranges[0][1][:4] + "-01-31T23:59:59")]}
    elif kind == "season" and not (m.group(1) or m.group(3)):
        cond["soft"], cond["word"] = True, m.group(2)
    elif kind == "month":
        cond["soft"], cond["word"] = True, m.group(1)


def parse(text: str, today: date | None = None) -> tuple[list[dict], str, str]:
    """(conditions, the rest of the words, the date words that were found)."""
    conditions, rest, used, _ = _parse(text, today)
    return conditions, rest, used


def for_content(text: str, today: date | None = None) -> tuple[str, list[dict]]:
    """What to search by content, and the date conditions that still apply to its results: the firm
    ones ("2019", "last summer"), and the year of a holiday given one."""
    conditions, _, _, ctext = _parse(text, today)
    firm = []
    for c in conditions:
        if not c.get("soft"):
            firm.append(c)
        elif c.get("firm"):
            firm.append(c["firm"])
    return ctext, firm


def word_tokens(rest: str, had_dates: bool = False) -> list[str]:
    """The words to look for in names, places and so on: small words dropped, unless nothing else is left
    (and there were no date words either: "photos from christmas" is only a date)."""
    words = rest.lower().split()
    kept = [w for w in words if w not in STOPWORDS]
    return kept if kept or had_dates else words


def sql(cond: dict, column: str = "a.taken_at") -> tuple[str, list]:
    """SQL for one condition."""
    if "ranges" in cond:
        parts = [f"({column} BETWEEN ? AND ?)" for _ in cond["ranges"]]
        params = [v for r in cond["ranges"] for v in r]
        if len(parts) > 60:     # every year since 1970: compare month and day instead
            days = sorted({(s[5:10], e[5:10]) for s, e in cond["ranges"]})
            parts = [f"(substr({column}, 6, 5) BETWEEN ? AND ?)" for _ in days]
            params = [v for d in days for v in d]
        return "(" + " OR ".join(parts) + ")", params
    if "months" in cond:
        marks = ",".join("?" * len(cond["months"]))
        return f"CAST(substr({column}, 6, 2) AS INTEGER) IN ({marks})", list(cond["months"])
    if "days" in cond:
        parts = [f"substr({column}, 6, 5) = ?" for _ in cond["days"]]
        return "(" + " OR ".join(parts) + ")", [f"{m:02d}-{d:02d}" for m, d in cond["days"]]
    return "1", []


def matches(cond: dict, taken_at: str) -> bool:
    """The same test in Python (for results found by content)."""
    if "ranges" in cond:
        return any(s <= taken_at <= e for s, e in cond["ranges"]) or (
            len(cond["ranges"]) > 60 and any(s[5:10] <= taken_at[5:10] <= e[5:10] for s, e in cond["ranges"]))
    if "months" in cond:
        return int(taken_at[5:7]) in cond["months"]
    if "days" in cond:
        return taken_at[5:10] in {f"{m:02d}-{d:02d}" for m, d in cond["days"]}
    return True


def now_local() -> date:
    return datetime.now().date()
