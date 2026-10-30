"""Accounts, passwords and sessions.

* Passwords are stored as salted scrypt hashes (Python standard library), never as text.
* A session is a random token. The browser keeps it in an HttpOnly cookie; other apps (a
  future phone app) can send it as "Authorization: Bearer <token>". Only a SHA-256 of the
  token is stored, so a copy of the database does not let anyone log in.
* Changes (POST, PATCH, DELETE) from a browser must carry the X-Snapshots-Request header,
  which other websites cannot add. That blocks cross-site request forgery.
* API keys (for scripts and other apps) start with "snap_" and are sent as a Bearer token. Like
  sessions, only a hash is stored. A key can do what its owner can, except manage keys, change the
  password or PIN, and open the locked folder.
* The locked folder opens with a PIN (stored like a password) for a few minutes at a time, and only
  in the session where it was typed.
"""

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

COOKIE = "snapshots_session"
CSRF_HEADER = "x-snapshots-request"
SESSION_DAYS = 30
MIN_PASSWORD = 8
API_KEY_PREFIX = "snap_"
UNLOCK_MINUTES = 10

_SCRYPT_N, _SCRYPT_R, _SCRYPT_P = 2**14, 8, 1   # ~16 MB and ~50 ms per check


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=32)
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt, key = stored.split("$")
        if scheme != "scrypt":
            return False
        test = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt),
                              n=int(n), r=int(r), p=int(p), dklen=len(bytes.fromhex(key)))
        return hmac.compare_digest(test.hex(), key)
    except (ValueError, TypeError):
        return False


_DUMMY_HASH = hash_password(secrets.token_hex(8))


def check_login(conn, username: str, password: str):
    """The user row if the name and password match, else None. Takes the same time either way."""
    row = conn.execute("SELECT * FROM users WHERE username = ?", (username.strip(),)).fetchone()
    ok = verify_password(password, row["pw_hash"] if row else _DUMMY_HASH)
    return row if (row and ok) else None


def validate_new_password(password: str) -> str | None:
    if len(password) < MIN_PASSWORD:
        return f"Passwords need at least {MIN_PASSWORD} characters."
    if len(password) > 1024:
        return "That password is too long."
    return None


def validate_username(name: str) -> str | None:
    name = name.strip()
    if not 1 <= len(name) <= 64:
        return "User names need 1 to 64 characters."
    if any(c in name for c in "\r\n\t\x00"):
        return "User names can't contain line breaks or tabs."
    return None


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii", "ignore")).hexdigest()


def create_session(conn, user_id: int, user_agent: str | None) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now()
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at, user_agent) VALUES (?, ?, ?, ?, ?)",
        (_token_hash(token), user_id, now.isoformat(timespec="seconds"),
         (now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), (user_agent or "")[:200]),
    )
    return token


def user_for_token(conn, token: str | None):
    """The logged-in user for a session token or API key, extending the session while it is used."""
    if not token:
        return None
    th = _token_hash(token)
    if token.startswith(API_KEY_PREFIX):
        row = conn.execute(
            "SELECT u.id, u.username, u.is_admin, k.id AS key_id, k.last_used_at FROM api_keys k"
            " JOIN users u ON u.id = k.user_id WHERE k.key_hash = ?", (th,)).fetchone()
        if row is None:
            return None
        stamp = datetime.now().isoformat(timespec="seconds")
        if not row["last_used_at"] or row["last_used_at"][:13] != stamp[:13]:  # at most once an hour
            conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (stamp, row["key_id"]))
        return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]),
                "api_key": row["key_id"], "unlocked": False}
    row = conn.execute(
        "SELECT u.id, u.username, u.is_admin, s.expires_at, s.unlocked_until FROM sessions s"
        " JOIN users u ON u.id = s.user_id WHERE s.token_hash = ?",
        (th,),
    ).fetchone()
    if row is None:
        return None
    now = datetime.now()
    expires = datetime.fromisoformat(row["expires_at"])
    if expires <= now:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (th,))
        return None
    if expires - now < timedelta(days=SESSION_DAYS - 1):  # slide the expiry, at most once a day
        conn.execute("UPDATE sessions SET expires_at = ? WHERE token_hash = ?",
                     ((now + timedelta(days=SESSION_DAYS)).isoformat(timespec="seconds"), th))
    unlocked = bool(row["unlocked_until"]) and datetime.fromisoformat(row["unlocked_until"]) > now
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]),
            "unlocked": unlocked}


def create_api_key(conn, user_id: int, name: str) -> tuple[int, str]:
    """A new key for scripts and apps. Returns (id, key); the key itself is shown only this once."""
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    key_id = conn.execute(
        "INSERT INTO api_keys (user_id, name, key_hash, hint, created_at) VALUES (?, ?, ?, ?, ?)",
        (user_id, name, _token_hash(key), key[:9], datetime.now().isoformat(timespec="seconds")),
    ).lastrowid
    return key_id, key


def validate_pin(pin: str) -> str | None:
    if not (4 <= len(pin) <= 32):
        return "The PIN needs 4 to 32 characters."
    return None


def set_unlocked(conn, token: str | None, minutes: float) -> str | None:
    """Open (minutes > 0) or close (0) the locked folder for this session. Returns until when."""
    if not token:
        return None
    until = (datetime.now() + timedelta(minutes=minutes)).isoformat(timespec="seconds") if minutes > 0 else None
    conn.execute("UPDATE sessions SET unlocked_until = ? WHERE token_hash = ?", (until, _token_hash(token)))
    return until


def end_session(conn, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))


def end_all_sessions(conn, user_id: int, keep_token: str | None = None) -> None:
    if keep_token:
        conn.execute("DELETE FROM sessions WHERE user_id = ? AND token_hash != ?", (user_id, _token_hash(keep_token)))
    else:
        conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))


class LoginThrottle:
    """Slows down password guessing: at most `limit` failures per name and per address
    in `window` seconds."""

    def __init__(self, limit: int = 10, window: float = 15 * 60):
        self.limit = limit
        self.window = window
        self._failures: dict[str, deque] = defaultdict(deque)
        self._lock = threading.Lock()

    def _trim(self, key: str, now: float) -> deque:
        q = self._failures[key]
        while q and now - q[0] > self.window:
            q.popleft()
        return q

    def retry_after(self, *keys: str) -> int:
        """Seconds to wait before trying again (0 = go ahead)."""
        now = time.time()
        with self._lock:
            waits = []
            for key in keys:
                q = self._trim(key, now)
                if len(q) >= self.limit:
                    waits.append(int(self.window - (now - q[0])) + 1)
            return max(waits, default=0)

    def failed(self, *keys: str) -> None:
        now = time.time()
        with self._lock:
            for key in keys:
                self._trim(key, now).append(now)

    def succeeded(self, *keys: str) -> None:
        with self._lock:
            for key in keys:
                self._failures.pop(key, None)
