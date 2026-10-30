"""Smart albums: albums that fill themselves from a saved search.

A smart album keeps a rule instead of a list of photos. Every time it is opened, the photos that match
the rule right now are shown, so new photos join it on their own. A rule can ask for:

* words (as typed in the search box: names, places, text in photos, tags, dates),
* what is in the photo (search by content, like "beach"),
* people (all of them must be in the photo), a tag, photos or videos only, a date range, a place,
* favourites only (each person sees their own favourites),
* whether archived items count.
"""

import json
import re
from dataclasses import replace

from . import db

DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
MAX_TEXT = 200
MAX_PEOPLE = 10


def parse_rule(value) -> dict:
    """A clean rule from what the app sent. Raises ValueError with a message people can read."""
    if isinstance(value, str):
        value = json.loads(value)
    if not isinstance(value, dict):
        raise ValueError("A smart album needs rules.")
    rule: dict = {}

    def text(key):
        v = value.get(key)
        if v is None:
            return
        if not isinstance(v, str) or len(v) > MAX_TEXT:
            raise ValueError(f"'{key}' should be text of at most {MAX_TEXT} characters.")
        v = " ".join(v.split())
        if v:
            rule[key] = v

    text("words")
    text("look")
    people = value.get("people") or []
    if not isinstance(people, list) or len(people) > MAX_PEOPLE or not all(isinstance(p, int) for p in people):
        raise ValueError(f"Choose at most {MAX_PEOPLE} people.")
    if people:
        rule["people"] = list(dict.fromkeys(people))
    if value.get("tag") is not None:
        if not isinstance(value["tag"], int):
            raise ValueError("Bad tag.")
        rule["tag"] = value["tag"]
    if value.get("kind"):
        if value["kind"] not in ("photo", "video"):
            raise ValueError("'kind' should be photo or video.")
        rule["kind"] = value["kind"]
    for key in ("start", "end"):
        v = value.get(key)
        if v:
            if not isinstance(v, str) or not DATE.match(v):
                raise ValueError("Dates should look like 2024-07-31.")
            rule[key] = v
    if rule.get("start") and rule.get("end") and rule["start"] > rule["end"]:
        raise ValueError("The first date is after the last one.")
    for key in ("country", "state", "city"):
        v = value.get(key)
        if v:
            if not isinstance(v, str) or len(v) > MAX_TEXT:
                raise ValueError("Bad place.")
            rule[key] = v.strip()
    if value.get("favorite"):
        rule["favorite"] = True
    if value.get("hide_archived"):
        rule["hide_archived"] = True
    if not any(k in rule for k in ("words", "look", "people", "tag", "kind", "start", "end", "country", "favorite")):
        raise ValueError("Give the smart album at least one rule, like a person, words or dates.")
    return rule


def load(text: str | None) -> dict | None:
    if not text:
        return None
    try:
        return parse_rule(text)
    except (ValueError, TypeError):
        return None


def to_filter(rule: dict, f: db.Filter, content_search) -> tuple[db.Filter, bool]:
    """The grid filter for a smart album, keeping what the person added on top (a search, say).

    content_search(text) -> [asset ids] or None when search by content isn't ready yet.
    Returns (filter, waiting): waiting is True when the rule needs search by content and it isn't ready.
    """
    g = replace(f, album=None)
    g.words = " ".join(x for x in (rule.get("words", ""), f.words) if x)
    if rule.get("people"):
        g.people = list(dict.fromkeys((f.people or []) + rule["people"]))
    if rule.get("tag") is not None:
        g.tag = rule["tag"]
    if rule.get("kind"):
        g.kind = rule["kind"]
    if rule.get("start"):
        g.start = max(filter(None, (f.start, rule["start"] + "T00:00:00")))
    if rule.get("end"):
        g.end = min(filter(None, (f.end, rule["end"] + "T23:59:59")))
    if rule.get("country"):
        g.country = rule["country"]
        g.state = rule.get("state") or None    # None: any state
        g.city = rule.get("city") or None
    if rule.get("favorite"):
        g.favorite = True
    if rule.get("hide_archived") and not f.archived:
        g.archived = "hide"
    waiting = False
    if rule.get("look"):
        found = content_search(rule["look"])
        if found is None:
            found, waiting = [], True
        ids = set(found)
        g.ids = [i for i in (f.ids if f.ids is not None else found) if i in ids]
    return g, waiting


def resolve(conn, cfg, worker, f: db.Filter) -> tuple[db.Filter, bool]:
    """f itself, or, when it shows a smart album, the filter that finds that album's items."""
    if f.album is None:
        return f, False
    row = conn.execute("SELECT rule FROM albums WHERE id = ?", (f.album,)).fetchone()
    rule = load(row["rule"]) if row else None
    if not rule:
        return f, False

    def content_search(text):
        if not cfg.smart_search or not worker.clip.ready():
            return None
        return [i for i, _ in worker.clip.search(conn, text)]

    return to_filter(rule, f, content_search)
