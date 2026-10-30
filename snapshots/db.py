"""SQLite storage. One short-lived connection per request; the background worker keeps its own."""

import calendar
import json
import re
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

# Each entry upgrades the database by one version. Never edit an entry once released:
# add a new one instead. PRAGMA user_version records how many have been applied.
MIGRATIONS = [
    # 1 - version 0.1: the photo index
    """
    CREATE TABLE IF NOT EXISTS assets (
        id        INTEGER PRIMARY KEY,
        path      TEXT NOT NULL UNIQUE,      -- relative to the library, forward slashes
        filename  TEXT NOT NULL,
        kind      TEXT NOT NULL CHECK (kind IN ('photo', 'video')),
        size      INTEGER NOT NULL,
        mtime_ns  INTEGER NOT NULL,
        sha1      TEXT NOT NULL,
        taken_at  TEXT NOT NULL,             -- local time, YYYY-MM-DDTHH:MM:SS
        width     INTEGER,
        height    INTEGER,
        camera    TEXT,
        has_thumb INTEGER NOT NULL DEFAULT 0,
        added_at  TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_assets_taken ON assets (taken_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_assets_kind  ON assets (kind, taken_at DESC, id DESC);
    CREATE INDEX IF NOT EXISTS idx_assets_sha1  ON assets (sha1);
    """,
    # 2 - version 0.2: accounts, favourites, albums, trash, places, faces
    """
    ALTER TABLE assets ADD COLUMN trashed_at TEXT;
    ALTER TABLE assets ADD COLUMN lat REAL;
    ALTER TABLE assets ADD COLUMN lon REAL;
    ALTER TABLE assets ADD COLUMN city TEXT;
    ALTER TABLE assets ADD COLUMN state TEXT;
    ALTER TABLE assets ADD COLUMN country TEXT;
    ALTER TABLE assets ADD COLUMN geo_done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN faces_done INTEGER NOT NULL DEFAULT 0;
    CREATE INDEX idx_assets_trashed ON assets (trashed_at);
    CREATE INDEX idx_assets_geo     ON assets (geo_done) WHERE geo_done = 0;
    CREATE INDEX idx_assets_faces   ON assets (faces_done) WHERE faces_done = 0;
    CREATE INDEX idx_assets_place   ON assets (country, state, city);

    CREATE TABLE users (
        id         INTEGER PRIMARY KEY,
        username   TEXT NOT NULL UNIQUE COLLATE NOCASE,
        pw_hash    TEXT NOT NULL,
        is_admin   INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE sessions (
        token_hash TEXT PRIMARY KEY,
        user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        expires_at TEXT NOT NULL,
        user_agent TEXT
    );
    CREATE INDEX idx_sessions_user ON sessions (user_id);

    CREATE TABLE favorites (
        user_id    INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        asset_id   INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        created_at TEXT NOT NULL,
        PRIMARY KEY (user_id, asset_id)
    ) WITHOUT ROWID;
    CREATE INDEX idx_favorites_asset ON favorites (asset_id);

    CREATE TABLE albums (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL,
        created_by INTEGER REFERENCES users (id) ON DELETE SET NULL,
        created_at TEXT NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE album_assets (
        album_id INTEGER NOT NULL REFERENCES albums (id) ON DELETE CASCADE,
        asset_id INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        added_at TEXT NOT NULL,
        PRIMARY KEY (album_id, asset_id)
    ) WITHOUT ROWID;
    CREATE INDEX idx_album_assets_asset ON album_assets (asset_id);

    CREATE TABLE people (
        id         INTEGER PRIMARY KEY,
        name       TEXT,
        hidden     INTEGER NOT NULL DEFAULT 0,
        created_at TEXT NOT NULL
    );
    CREATE TABLE faces (
        id         INTEGER PRIMARY KEY,
        asset_id   INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        person_id  INTEGER REFERENCES people (id) ON DELETE SET NULL,
        x REAL NOT NULL, y REAL NOT NULL, w REAL NOT NULL, h REAL NOT NULL,  -- fractions of the picture
        score      REAL NOT NULL,
        embedding  BLOB NOT NULL,            -- 128 float32 values, unit length
        created_at TEXT NOT NULL
    );
    CREATE INDEX idx_faces_asset  ON faces (asset_id);
    CREATE INDEX idx_faces_person ON faces (person_id);
    """,
    # 3 - version 0.4: photo checks (copies, bursts, blur, dark), search by content, text in photos
    """
    ALTER TABLE assets ADD COLUMN dhash INTEGER;           -- 64-bit picture fingerprint, NULL if flat or a video
    ALTER TABLE assets ADD COLUMN sharpness REAL;
    ALTER TABLE assets ADD COLUMN brightness REAL;
    ALTER TABLE assets ADD COLUMN highlights REAL;          -- brightness of the brightest 1%
    ALTER TABLE assets ADD COLUMN quality_done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN keep_quality INTEGER NOT NULL DEFAULT 0;  -- "this one is fine" for blurry/dark
    ALTER TABLE assets ADD COLUMN clip_done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN ocr_done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN ocr_text TEXT;
    UPDATE assets SET ocr_done = 1 WHERE kind = 'video';
    CREATE INDEX idx_assets_quality ON assets (quality_done) WHERE quality_done = 0;
    CREATE INDEX idx_assets_clip    ON assets (clip_done) WHERE clip_done = 0;
    CREATE INDEX idx_assets_ocr     ON assets (ocr_done) WHERE ocr_done = 0;
    CREATE INDEX idx_assets_dhash   ON assets (dhash) WHERE dhash IS NOT NULL;

    CREATE TABLE clip_vectors (
        asset_id INTEGER PRIMARY KEY REFERENCES assets (id) ON DELETE CASCADE,
        vec      BLOB NOT NULL                 -- 512 float16 values, unit length
    );
    CREATE TABLE not_duplicates (              -- pairs someone said are not copies of each other
        a INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        b INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        PRIMARY KEY (a, b)
    ) WITHOUT ROWID;
    CREATE INDEX idx_not_duplicates_b ON not_duplicates (b);
    """,
    # 4 - version 0.5: archive, tags, stacks, Live Photos and motion photos
    """
    ALTER TABLE assets ADD COLUMN archived_at TEXT;
    ALTER TABLE assets ADD COLUMN stack_id INTEGER REFERENCES stacks (id) ON DELETE SET NULL;
    ALTER TABLE assets ADD COLUMN no_auto_stack INTEGER NOT NULL DEFAULT 0;  -- someone took it out of a stack
    ALTER TABLE assets ADD COLUMN live_video INTEGER REFERENCES assets (id) ON DELETE SET NULL;  -- a Live Photo's clip
    ALTER TABLE assets ADD COLUMN motion_offset INTEGER;   -- a motion photo: where its video starts in the file
    ALTER TABLE assets ADD COLUMN motion_length INTEGER;
    ALTER TABLE assets ADD COLUMN motion_done INTEGER NOT NULL DEFAULT 0;
    UPDATE assets SET motion_done = 1 WHERE kind = 'video'
        OR lower(path) NOT GLOB '*.jp*g' AND lower(path) NOT GLOB '*.hei[cf]';
    CREATE INDEX idx_assets_archived ON assets (archived_at);
    CREATE INDEX idx_assets_stack    ON assets (stack_id) WHERE stack_id IS NOT NULL;
    CREATE INDEX idx_assets_live     ON assets (live_video) WHERE live_video IS NOT NULL;
    CREATE INDEX idx_assets_motion   ON assets (motion_done) WHERE motion_done = 0;
    CREATE INDEX idx_assets_path     ON assets (path COLLATE NOCASE);

    CREATE TABLE stacks (
        id         INTEGER PRIMARY KEY,
        cover_id   INTEGER REFERENCES assets (id) ON DELETE SET NULL,  -- the photo shown for the stack
        auto       INTEGER NOT NULL DEFAULT 0,                         -- made by Snapshots (RAW + JPEG)
        created_at TEXT NOT NULL
    );
    CREATE TABLE tags (
        id         INTEGER PRIMARY KEY,
        name       TEXT NOT NULL UNIQUE COLLATE NOCASE,
        created_at TEXT NOT NULL
    );
    CREATE TABLE asset_tags (
        asset_id INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        tag_id   INTEGER NOT NULL REFERENCES tags (id) ON DELETE CASCADE,
        PRIMARY KEY (asset_id, tag_id)
    ) WITHOUT ROWID;
    CREATE INDEX idx_asset_tags_tag ON asset_tags (tag_id);
    """,
]
SCHEMA_VERSION = len(MIGRATIONS)


def connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init(db_path: Path) -> None:
    """Create the database, or bring an older one up to date."""
    conn = connect(db_path)
    try:
        current = conn.execute("PRAGMA user_version").fetchone()[0]
        if current > SCHEMA_VERSION:
            raise RuntimeError(
                f"This database was made by a newer version of Snapshots "
                f"(schema {current}, this program knows {SCHEMA_VERSION}). Please update Snapshots."
            )
        for version in range(current + 1, SCHEMA_VERSION + 1):
            # executescript runs outside Python's transaction handling, so the
            # whole step (including the version bump) is wrapped in one transaction.
            conn.executescript(
                f"BEGIN;\n{MIGRATIONS[version - 1]}\nPRAGMA user_version = {version};\nCOMMIT;"
            )
    finally:
        conn.close()


@contextmanager
def session(db_path: Path):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# ---------------------------------------------------------------- filtering


@dataclass
class Filter:
    """What a grid of photos should show. Every view in the app is one of these."""

    q: str = ""
    kind: str = ""                 # "", "photo" or "video"
    favorite: bool = False
    trashed: bool = False          # the Trash view; everywhere else hides trashed items
    album: int | None = None
    person: int | None = None
    country: str | None = None     # a place: country, optionally narrowed by state and city
    state: str | None = None
    city: str | None = None
    start: str | None = None       # taken between these two times (inclusive): an event
    end: str | None = None
    low_quality: str | None = None  # SQL condition for blurry/dark photos (built from the settings)
    ids: list[int] | None = None   # only these photos (search by content)
    archived: str = ""             # "hide": leave out archived items (the timelines), "only": the Archive
    tag: int | None = None
    folder: str | None = None      # only items directly in this folder ("" = the library folder itself)
    stacks: bool = False           # show each stack as one item (its cover)
    bbox: tuple[float, float, float, float] | None = None  # south, west, north, east: an area of the map


_MONTHS = {}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i
_DATE_TOKEN = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


def _escape(text: str) -> str:
    return text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like(text: str) -> str:
    return f"%{_escape(text)}%"


# A Live Photo's clip is shown with its photo, never on its own.
LIVE_PART_HIDDEN = "NOT EXISTS (SELECT 1 FROM assets lp WHERE lp.live_video = a.id)"
# In a stack, only the first photo still in the library is shown: the cover, or else the oldest
# other member (by id). The others are reached from it.
STACK_HIDDEN = (
    "(a.stack_id IS NULL OR NOT EXISTS ("
    " SELECT 1 FROM assets sm JOIN stacks st ON st.id = sm.stack_id"
    " WHERE sm.stack_id = a.stack_id AND sm.id != a.id AND sm.trashed_at IS NULL"
    " AND (sm.id IS st.cover_id OR (a.id IS NOT st.cover_id AND sm.id < a.id))))"
)


def where_clause(f: Filter, user_id: int | None) -> tuple[str, list]:
    """SQL for a Filter, on table alias `a`. Every search word must match (AND).

    A word can be a year / year-month / full date, a month name, or text looked for in
    the file path, camera, place names, text read in the photo and the names of people in it.
    """
    where: list[str] = ["a.trashed_at IS NOT NULL" if f.trashed else "a.trashed_at IS NULL", LIVE_PART_HIDDEN]
    params: list = []
    if f.archived == "hide":
        where.append("a.archived_at IS NULL")
    elif f.archived == "only":
        where.append("a.archived_at IS NOT NULL")
    if f.tag is not None:
        where.append("a.id IN (SELECT asset_id FROM asset_tags WHERE tag_id = ?)")
        params.append(f.tag)
    if f.folder is not None:
        if f.folder:
            prefix = f.folder.strip("/") + "/"
            where.append("substr(a.path, 1, ?) = ? AND instr(substr(a.path, ?), '/') = 0")
            params += [len(prefix), prefix, len(prefix) + 1]
        else:
            where.append("instr(a.path, '/') = 0")
    if f.stacks:
        where.append(STACK_HIDDEN)
    if f.bbox is not None:
        south, west, north, east = f.bbox
        where.append("a.lat BETWEEN ? AND ?")
        params += [south, north]
        if west <= east:
            where.append("a.lon BETWEEN ? AND ?")
            params += [west, east]
        else:  # the area crosses the date line
            where.append("(a.lon >= ? OR a.lon <= ?)")
            params += [west, east]
    if f.kind in ("photo", "video"):
        where.append("a.kind = ?")
        params.append(f.kind)
    if f.favorite:
        where.append("a.id IN (SELECT asset_id FROM favorites WHERE user_id = ?)")
        params.append(user_id)
    if f.album is not None:
        where.append("a.id IN (SELECT asset_id FROM album_assets WHERE album_id = ?)")
        params.append(f.album)
    if f.person is not None:
        where.append("a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)")
        params.append(f.person)
    if f.start:
        where.append("a.taken_at >= ?")
        params.append(f.start)
    if f.end:
        where.append("a.taken_at <= ?")
        params.append(f.end)
    if f.low_quality:
        where.append(f.low_quality)
    if f.ids is not None:
        where.append("a.id IN (SELECT value FROM json_each(?))")
        params.append(json.dumps(f.ids))
    for column in ("country", "state", "city"):
        value = getattr(f, column)
        if value is not None:
            where.append(f"a.{column} IS ?")  # IS so that "no state" can be asked for with NULL
            params.append(value or None)
    for token in f.q.lower().split()[:10]:
        if _DATE_TOKEN.fullmatch(token):
            where.append("a.taken_at LIKE ?")
            params.append(token + "%")
        elif token in _MONTHS:
            where.append("substr(a.taken_at, 6, 2) = ?")
            params.append(f"{_MONTHS[token]:02d}")
        else:
            like = _like(token)
            where.append(
                "(lower(a.path) LIKE ?1 ESCAPE '\\' OR lower(coalesce(a.camera, '')) LIKE ?1 ESCAPE '\\'"
                " OR lower(coalesce(a.city, '')) LIKE ?1 ESCAPE '\\'"
                " OR lower(coalesce(a.state, '')) LIKE ?1 ESCAPE '\\'"
                " OR lower(coalesce(a.country, '')) LIKE ?1 ESCAPE '\\'"
                " OR lower(coalesce(a.ocr_text, '')) LIKE ?1 ESCAPE '\\'"
                " OR a.id IN (SELECT at.asset_id FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
                "             WHERE lower(t.name) LIKE ?1 ESCAPE '\\')"
                " OR a.id IN (SELECT f.asset_id FROM faces f JOIN people p ON p.id = f.person_id"
                "             WHERE p.hidden = 0 AND lower(p.name) LIKE ?1 ESCAPE '\\'))"
                .replace("?1", "?")
            )
            params += [like] * 8
    return " AND ".join(where), params


ASSET_COLUMNS = (
    "a.id, a.path, a.filename, a.kind, a.size, a.sha1, a.taken_at, a.width, a.height, a.camera,"
    " a.has_thumb, a.trashed_at, a.lat, a.lon, a.city, a.state, a.country, a.sharpness, a.brightness,"
    " a.highlights,"
    " a.keep_quality, a.ocr_text, a.archived_at, a.stack_id, a.live_video, a.motion_offset,"
    " (SELECT COUNT(*) FROM assets sc WHERE sc.stack_id = a.stack_id AND sc.trashed_at IS NULL) AS stack_count"
)


def list_assets(conn, f: Filter, user_id: int | None, cursor: str, limit: int):
    """Newest first. Returns (rows, next_cursor). The cursor is 'taken_at|id'."""
    where, params = where_clause(f, user_id)
    if cursor:
        try:
            taken, _, ident = cursor.rpartition("|")
            ident = int(ident)
            if not taken:
                raise ValueError
        except ValueError:
            raise ValueError("bad cursor") from None
        where += " AND (a.taken_at < ? OR (a.taken_at = ? AND a.id < ?))"
        params += [taken, taken, ident]
    sql = (
        f"SELECT {ASSET_COLUMNS}, (fav.asset_id IS NOT NULL) AS favorite FROM assets a"
        " LEFT JOIN favorites fav ON fav.asset_id = a.id AND fav.user_id = ?"
        f" WHERE {where} ORDER BY a.taken_at DESC, a.id DESC LIMIT ?"
    )
    rows = conn.execute(sql, [user_id] + params + [limit + 1]).fetchall()
    next_cursor = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        next_cursor = f"{last['taken_at']}|{last['id']}"
    return rows, next_cursor


def timeline(conn, f: Filter, user_id: int | None) -> list[dict]:
    where, params = where_clause(f, user_id)
    sql = (
        "SELECT substr(a.taken_at, 1, 7) AS month, COUNT(*) AS count FROM assets a"
        f" WHERE {where} GROUP BY month ORDER BY month DESC"
    )
    return [dict(r) for r in conn.execute(sql, params)]


def get_asset(conn, asset_id: int, user_id: int | None):
    return conn.execute(
        f"SELECT {ASSET_COLUMNS}, (fav.asset_id IS NOT NULL) AS favorite FROM assets a"
        " LEFT JOIN favorites fav ON fav.asset_id = a.id AND fav.user_id = ?"
        " WHERE a.id = ?",
        (user_id, asset_id),
    ).fetchone()
