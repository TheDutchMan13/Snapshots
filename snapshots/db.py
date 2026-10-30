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
    # 5 - version 0.6: edits and details, 360° photos, locked folder, comments, API keys, video copies
    """
    ALTER TABLE assets ADD COLUMN description TEXT;
    ALTER TABLE assets ADD COLUMN taken_at_file TEXT;      -- the file's own date, kept when someone changed taken_at
    ALTER TABLE assets ADD COLUMN position_edited INTEGER NOT NULL DEFAULT 0;  -- lat/lon set (or removed) by hand
    ALTER TABLE assets ADD COLUMN edits TEXT;              -- JSON: rotate, flip, straighten, crop (the file is untouched)
    ALTER TABLE assets ADD COLUMN edit_v TEXT;             -- short hash of edits, so browsers fetch new thumbnails
    ALTER TABLE assets ADD COLUMN projection TEXT;         -- 'sphere' (360°) or 'pano' (a partial panorama)
    ALTER TABLE assets ADD COLUMN pano_done INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE assets ADD COLUMN locked_by INTEGER REFERENCES users (id) ON DELETE SET NULL;
    ALTER TABLE assets ADD COLUMN vcodec TEXT;             -- a video's codec and container, once looked at
    UPDATE assets SET pano_done = 1 WHERE kind = 'video'
        OR lower(path) NOT GLOB '*.jp*g' AND lower(path) NOT GLOB '*.hei[cf]' AND lower(path) NOT GLOB '*.tif*';
    CREATE INDEX idx_assets_pano   ON assets (pano_done) WHERE pano_done = 0;
    CREATE INDEX idx_assets_locked ON assets (locked_by) WHERE locked_by IS NOT NULL;

    ALTER TABLE users ADD COLUMN pin_hash TEXT;            -- opens the locked folder
    ALTER TABLE users ADD COLUMN activity_seen_at TEXT;
    ALTER TABLE sessions ADD COLUMN unlocked_until TEXT;   -- the locked folder is open until then
    ALTER TABLE album_assets ADD COLUMN added_by INTEGER REFERENCES users (id) ON DELETE SET NULL;

    CREATE TABLE comments (
        id         INTEGER PRIMARY KEY,
        asset_id   INTEGER REFERENCES assets (id) ON DELETE CASCADE,
        album_id   INTEGER REFERENCES albums (id) ON DELETE CASCADE,
        user_id    INTEGER REFERENCES users (id) ON DELETE SET NULL,
        text       TEXT NOT NULL,
        created_at TEXT NOT NULL,
        CHECK (asset_id IS NOT NULL OR album_id IS NOT NULL)
    );
    CREATE INDEX idx_comments_asset ON comments (asset_id) WHERE asset_id IS NOT NULL;
    CREATE INDEX idx_comments_album ON comments (album_id) WHERE album_id IS NOT NULL;
    CREATE INDEX idx_comments_time  ON comments (created_at);
    CREATE INDEX idx_album_assets_added ON album_assets (added_at);

    CREATE TABLE api_keys (
        id           INTEGER PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        name         TEXT NOT NULL,
        key_hash     TEXT NOT NULL UNIQUE,
        hint         TEXT NOT NULL,                        -- the first characters, so people can tell keys apart
        created_at   TEXT NOT NULL,
        last_used_at TEXT
    );
    """,
    # 6 - version 0.7: smart albums, imports from Immich and Google Photos, webhooks
    """
    ALTER TABLE albums ADD COLUMN rule TEXT;               -- JSON: a smart album's search (its items are found, not added)

    CREATE TABLE face_hints (                  -- names from an import, waiting for faces to be found
        id       INTEGER PRIMARY KEY,
        asset_id INTEGER NOT NULL REFERENCES assets (id) ON DELETE CASCADE,
        name     TEXT NOT NULL,
        x REAL, y REAL, w REAL, h REAL,        -- where the face is (fractions), NULL if not known
        source   TEXT NOT NULL
    );
    CREATE INDEX idx_face_hints_asset ON face_hints (asset_id);

    CREATE TABLE imports (
        id          INTEGER PRIMARY KEY,
        kind        TEXT NOT NULL,             -- 'immich' or 'takeout'
        started_by  INTEGER REFERENCES users (id) ON DELETE SET NULL,
        started_at  TEXT NOT NULL,
        finished_at TEXT,
        summary     TEXT                       -- JSON: what was found and changed
    );

    CREATE TABLE webhooks (
        id          INTEGER PRIMARY KEY,
        url         TEXT NOT NULL,
        events      TEXT NOT NULL,             -- JSON list of event names
        secret      TEXT NOT NULL,             -- signs each delivery (HMAC-SHA256)
        enabled     INTEGER NOT NULL DEFAULT 1,
        created_at  TEXT NOT NULL,
        last_at     TEXT,
        last_status TEXT                       -- "200", or what went wrong
    );
    CREATE TABLE webhook_deliveries (
        id           INTEGER PRIMARY KEY,
        webhook_id   INTEGER NOT NULL REFERENCES webhooks (id) ON DELETE CASCADE,
        event        TEXT NOT NULL,
        payload      TEXT NOT NULL,
        created_at   TEXT NOT NULL,
        attempts     INTEGER NOT NULL DEFAULT 0,
        next_try_at  REAL NOT NULL DEFAULT 0,  -- seconds since 1970
        delivered_at TEXT,
        failed       INTEGER NOT NULL DEFAULT 0,
        error        TEXT
    );
    CREATE INDEX idx_webhook_due ON webhook_deliveries (next_try_at) WHERE delivered_at IS NULL AND failed = 0;
    """,
    # 7 - version 0.8: face suggestions, two-step login, known devices, XMP sidecars
    """
    CREATE TABLE people_not_same (             -- pairs someone said are different people
        a INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
        b INTEGER NOT NULL REFERENCES people (id) ON DELETE CASCADE,
        PRIMARY KEY (a, b),
        CHECK (a < b)
    ) WITHOUT ROWID;
    CREATE INDEX idx_people_not_same_b ON people_not_same (b);

    ALTER TABLE users ADD COLUMN totp_secret TEXT;         -- two-step login: base32 secret, NULL = off
    ALTER TABLE users ADD COLUMN totp_last INTEGER;        -- the last time step used, so a code works once
    CREATE TABLE recovery_codes (
        user_id   INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        code_hash TEXT NOT NULL,
        used_at   TEXT,
        PRIMARY KEY (user_id, code_hash)
    ) WITHOUT ROWID;
    CREATE TABLE known_devices (               -- browsers someone has logged in from: kept out of lockouts
        token_hash   TEXT PRIMARY KEY,
        user_id      INTEGER NOT NULL REFERENCES users (id) ON DELETE CASCADE,
        created_at   TEXT NOT NULL,
        last_used_at TEXT NOT NULL,
        user_agent   TEXT
    );
    CREATE INDEX idx_known_devices_user ON known_devices (user_id);

    ALTER TABLE assets ADD COLUMN xmp_dirty INTEGER NOT NULL DEFAULT 0;  -- the sidecar may need writing
    ALTER TABLE assets ADD COLUMN xmp_hash TEXT;                        -- what Snapshots last wrote there
    CREATE INDEX idx_assets_xmp ON assets (xmp_dirty) WHERE xmp_dirty = 1;
    CREATE TRIGGER xmp_assets AFTER UPDATE OF taken_at, lat, lon, description ON assets
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = NEW.id; END;
    CREATE TRIGGER xmp_tag_add AFTER INSERT ON asset_tags
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = NEW.asset_id; END;
    CREATE TRIGGER xmp_tag_del AFTER DELETE ON asset_tags
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = OLD.asset_id; END;
    CREATE TRIGGER xmp_tag_name AFTER UPDATE OF name ON tags
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id IN (SELECT asset_id FROM asset_tags WHERE tag_id = NEW.id); END;
    CREATE TRIGGER xmp_fav_add AFTER INSERT ON favorites
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = NEW.asset_id; END;
    CREATE TRIGGER xmp_fav_del AFTER DELETE ON favorites
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = OLD.asset_id; END;
    CREATE TRIGGER xmp_face_add AFTER INSERT ON faces WHEN NEW.person_id IS NOT NULL
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = NEW.asset_id; END;
    CREATE TRIGGER xmp_face_move AFTER UPDATE OF person_id ON faces
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = NEW.asset_id; END;
    CREATE TRIGGER xmp_face_del AFTER DELETE ON faces
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id = OLD.asset_id; END;
    CREATE TRIGGER xmp_person_name AFTER UPDATE OF name ON people
        BEGIN UPDATE assets SET xmp_dirty = 1 WHERE id IN (SELECT asset_id FROM faces WHERE person_id = NEW.id); END;
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
    locked: bool = False           # the Locked folder: only the asking person's locked items
    people: list[int] | None = None  # all of these people are in it (a smart album)
    words: str = ""                # more words that must match, like q (a smart album's own words)


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
    " WHERE sm.stack_id = a.stack_id AND sm.id != a.id AND sm.trashed_at IS NULL AND sm.locked_by IS NULL"
    " AND (sm.id IS st.cover_id OR (a.id IS NOT st.cover_id AND sm.id < a.id))))"
)


def is_external(path: str) -> bool:
    """Paths in a second (third, ...) library folder are stored whole; the main library's are relative."""
    return path.startswith("/") or bool(_DRIVE.match(path))


_DRIVE = re.compile(r"^[A-Za-z]:/")


def folder_prefix(folder: str) -> str:
    """The text every path directly or indirectly in `folder` starts with ("" = the main library)."""
    if is_external(folder):
        return folder.rstrip("/") + "/"
    folder = folder.strip("/")
    return folder + "/" if folder else ""


def version(row) -> str:
    """Changes whenever the picture shown for an item changes (a new file, or an edit)."""
    edit = row["edit_v"] if "edit_v" in row.keys() else None
    return row["sha1"][:8] + (f"-{edit}" if edit else "")


def version_sql(alias: str = "a") -> str:
    return f"substr({alias}.sha1, 1, 8) || coalesce('-' || {alias}.edit_v, '')"


def versions(conn, ids) -> dict:
    """version() for many items at once."""
    ids = [i for i in dict.fromkeys(ids) if i]
    out = {}
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        out.update({r[0]: r[1] for r in conn.execute(
            f"SELECT a.id, {version_sql()} FROM assets a WHERE a.id IN ({','.join('?' * len(chunk))})", chunk)})
    return out


# Items in someone's locked folder are left out everywhere except that folder.
NOT_LOCKED = "a.locked_by IS NULL"


def open_ids(conn, ids) -> list[int]:
    """The ids that exist and are not in anyone's locked folder (others are ignored by changes)."""
    ids = list(dict.fromkeys(i for i in ids if isinstance(i, int)))
    out = set()
    for start in range(0, len(ids), 900):
        chunk = ids[start:start + 900]
        out.update(r[0] for r in conn.execute(
            f"SELECT id FROM assets WHERE locked_by IS NULL AND id IN ({','.join('?' * len(chunk))})", chunk))
    return [i for i in ids if i in out]


def where_clause(f: Filter, user_id: int | None) -> tuple[str, list]:
    """SQL for a Filter, on table alias `a`. Every search word must match (AND).

    A word can be a year / year-month / full date, a month name, or text looked for in
    the file path, camera, place names, description, text read in the photo and the names of people in it.
    """
    where: list[str] = ["a.trashed_at IS NOT NULL" if f.trashed else "a.trashed_at IS NULL", LIVE_PART_HIDDEN]
    params: list = []
    if f.locked:
        where.append("a.locked_by = ?")
        params.append(user_id)
    else:
        where.append(NOT_LOCKED)
    if f.archived == "hide":
        where.append("a.archived_at IS NULL")
    elif f.archived == "only":
        where.append("a.archived_at IS NOT NULL")
    if f.tag is not None:
        where.append("a.id IN (SELECT asset_id FROM asset_tags WHERE tag_id = ?)")
        params.append(f.tag)
    if f.folder is not None:
        prefix = folder_prefix(f.folder)
        if prefix:
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
    for person in f.people or []:
        where.append("a.id IN (SELECT asset_id FROM faces WHERE person_id = ?)")
        params.append(person)
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
    for token in (f.q.lower().split()[:10] + f.words.lower().split()[:10]):
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
                " OR lower(coalesce(a.description, '')) LIKE ?1 ESCAPE '\\'"
                " OR a.id IN (SELECT at.asset_id FROM asset_tags at JOIN tags t ON t.id = at.tag_id"
                "             WHERE lower(t.name) LIKE ?1 ESCAPE '\\')"
                " OR a.id IN (SELECT f.asset_id FROM faces f JOIN people p ON p.id = f.person_id"
                "             WHERE p.hidden = 0 AND lower(p.name) LIKE ?1 ESCAPE '\\'))"
                .replace("?1", "?")
            )
            params += [like] * 9
    return " AND ".join(where), params


ASSET_COLUMNS = (
    "a.id, a.path, a.filename, a.kind, a.size, a.sha1, a.taken_at, a.width, a.height, a.camera,"
    " a.has_thumb, a.trashed_at, a.lat, a.lon, a.city, a.state, a.country, a.sharpness, a.brightness,"
    " a.highlights,"
    " a.keep_quality, a.ocr_text, a.archived_at, a.stack_id, a.live_video, a.motion_offset,"
    " a.description, a.taken_at_file, a.position_edited, a.edits, a.edit_v, a.projection, a.locked_by,"
    " (SELECT COUNT(*) FROM assets sc WHERE sc.stack_id = a.stack_id AND sc.trashed_at IS NULL"
    "  AND sc.locked_by IS NULL) AS stack_count"
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
