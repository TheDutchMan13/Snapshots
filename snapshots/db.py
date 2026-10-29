"""SQLite storage. One short-lived connection per request; the background worker keeps its own."""

import calendar
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


_MONTHS = {}
for _i in range(1, 13):
    _MONTHS[calendar.month_name[_i].lower()] = _i
    _MONTHS[calendar.month_abbr[_i].lower()] = _i
_DATE_TOKEN = re.compile(r"\d{4}(-\d{2}(-\d{2})?)?")


def _like(text: str) -> str:
    esc = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{esc}%"


def where_clause(f: Filter, user_id: int | None) -> tuple[str, list]:
    """SQL for a Filter, on table alias `a`. Every search word must match (AND).

    A word can be a year / year-month / full date, a month name, or text looked for in
    the file path, camera, place names and the names of people in the photo.
    """
    where: list[str] = ["a.trashed_at IS NOT NULL" if f.trashed else "a.trashed_at IS NULL"]
    params: list = []
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
                " OR a.id IN (SELECT f.asset_id FROM faces f JOIN people p ON p.id = f.person_id"
                "             WHERE p.hidden = 0 AND lower(p.name) LIKE ?1 ESCAPE '\\'))"
                .replace("?1", "?")
            )
            params += [like] * 6
    return " AND ".join(where), params


ASSET_COLUMNS = (
    "a.id, a.path, a.filename, a.kind, a.size, a.sha1, a.taken_at, a.width, a.height, a.camera,"
    " a.has_thumb, a.trashed_at, a.lat, a.lon, a.city, a.state, a.country"
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
