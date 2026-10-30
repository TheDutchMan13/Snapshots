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
* Two-step login (optional, per account) asks for a 6-digit code from an authenticator app (TOTP,
  RFC 6238) after the password. Each code works once. Ten one-time recovery codes are given when it is
  turned on, for a lost phone; an administrator can also turn it off for someone.
* Lockouts: wrong passwords are counted per device. A browser that has logged in before carries a
  "known device" cookie and has its own allowance, so someone guessing at your user name from elsewhere
  can't lock you out of your own browser. Unknown devices share one allowance per user name, and each
  network address has one too.
"""

import base64
import struct
import urllib.parse

import hashlib
import hmac
import secrets
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta

COOKIE = "snapshots_session"
DEVICE_COOKIE = "snapshots_device"
DEVICE_DAYS = 400
TOTP_STEP = 30
TICKET_SECONDS = 5 * 60
TICKET_TRIES = 5
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


# ---------------------------------------------------------------- known devices

def remember_device(conn, user_id: int, token: str | None, user_agent: str | None) -> str:
    """Mark this browser as one the user logs in from. Returns the (possibly new) device token."""
    stamp = datetime.now().isoformat(timespec="seconds")
    if token:
        cur = conn.execute("UPDATE known_devices SET last_used_at = ?, user_agent = ? WHERE token_hash = ? AND user_id = ?",
                           (stamp, (user_agent or "")[:200], _token_hash(token), user_id))
        if cur.rowcount:
            return token
    token = token if token and len(token) >= 32 else secrets.token_urlsafe(32)
    conn.execute("INSERT OR REPLACE INTO known_devices (token_hash, user_id, created_at, last_used_at, user_agent)"
                 " VALUES (?, ?, ?, ?, ?)", (_token_hash(token), user_id, stamp, stamp, (user_agent or "")[:200]))
    return token


def is_known_device(conn, token: str | None, username: str) -> bool:
    if not token:
        return False
    return conn.execute(
        "SELECT 1 FROM known_devices d JOIN users u ON u.id = d.user_id WHERE d.token_hash = ? AND u.username = ?",
        (_token_hash(token), username.strip())).fetchone() is not None


def throttle_keys(known: bool, username: str, device_token: str | None, address: str) -> tuple[str, ...]:
    """Which allowances a login attempt counts against (see the module notes)."""
    name = username.strip().lower()
    if known:
        return (f"device:{_token_hash(device_token or '')}:{name}",)
    return (f"user:{name}", f"addr:{address}")


# ---------------------------------------------------------------- two-step login (TOTP)

def new_totp_secret() -> str:
    return base64.b32encode(secrets.token_bytes(20)).decode("ascii")


def _b32(secret: str) -> bytes:
    s = secret.strip().replace(" ", "").upper()
    return base64.b32decode(s + "=" * (-len(s) % 8))


def totp_at(secret: str, step: int, digits: int = 6) -> str:
    mac = hmac.new(_b32(secret), struct.pack(">Q", step), hashlib.sha1).digest()
    offset = mac[-1] & 0x0F
    value = struct.unpack(">I", mac[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(value % 10 ** digits).zfill(digits)


def check_totp(secret: str, code: str, last_step: int | None, now: float | None = None) -> int | None:
    """The time step the code belongs to (allowing one step of clock drift either way), or None.
    A step at or before last_step is refused, so a code can't be used twice."""
    code = "".join(c for c in (code or "") if c.isdigit())
    if len(code) != 6:
        return None
    step = int((time.time() if now is None else now) // TOTP_STEP)
    for s in (step - 1, step, step + 1):
        if last_step is not None and s <= last_step:
            continue
        if hmac.compare_digest(totp_at(secret, s), code):
            return s
    return None


def otpauth_uri(secret: str, username: str, issuer: str = "Snapshots") -> str:
    label = urllib.parse.quote(f"{issuer}:{username}", safe=":@")
    return (f"otpauth://totp/{label}?secret={secret}&issuer={urllib.parse.quote(issuer)}"
            f"&algorithm=SHA1&digits=6&period={TOTP_STEP}")


def _recovery_hash(code: str) -> str:
    return _token_hash("".join(c for c in code.lower() if c.isalnum()))


def new_recovery_codes(conn, user_id: int, n: int = 10) -> list[str]:
    """Replace the user's recovery codes. Returns them (shown only this once)."""
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    codes = ["".join(secrets.choice(alphabet) for _ in range(5)) + "-" + "".join(secrets.choice(alphabet) for _ in range(5))
             for _ in range(n)]
    conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
    conn.executemany("INSERT INTO recovery_codes (user_id, code_hash) VALUES (?, ?)",
                     [(user_id, _recovery_hash(c)) for c in codes])
    return codes


def use_recovery_code(conn, user_id: int, code: str) -> bool:
    cur = conn.execute("UPDATE recovery_codes SET used_at = ? WHERE user_id = ? AND code_hash = ? AND used_at IS NULL",
                       (datetime.now().isoformat(timespec="seconds"), user_id, _recovery_hash(code)))
    return cur.rowcount == 1


def check_second_step(conn, user_id: int, code: str) -> str | None:
    """'totp' or 'recovery' if the code is right (and records that it was used), else None."""
    row = conn.execute("SELECT totp_secret, totp_last FROM users WHERE id = ?", (user_id,)).fetchone()
    if row is None or not row["totp_secret"]:
        return None
    step = check_totp(row["totp_secret"], code, row["totp_last"])
    if step is not None:
        # Only one request can claim a step: the condition makes a replayed code fail.
        cur = conn.execute("UPDATE users SET totp_last = ? WHERE id = ? AND (totp_last IS NULL OR totp_last < ?)",
                           (step, user_id, step))
        return "totp" if cur.rowcount == 1 else None
    if any(c.isalpha() for c in code or "") and use_recovery_code(conn, user_id, code):
        return "recovery"
    return None


class Tickets:
    """Password accepted, code still to come: a short-lived ticket links the two steps."""

    def __init__(self):
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def create(self, user_id: int, keys: tuple[str, ...]) -> str:
        ticket = secrets.token_urlsafe(24)
        now = time.time()
        with self._lock:
            for t in [t for t, v in self._items.items() if v["expires"] < now]:
                del self._items[t]
            self._items[ticket] = {"user_id": user_id, "keys": keys, "expires": now + TICKET_SECONDS, "tries": 0}
        return ticket

    def get(self, ticket: str) -> dict | None:
        with self._lock:
            item = self._items.get(ticket or "")
            if item is None or item["expires"] < time.time() or item["tries"] >= TICKET_TRIES:
                self._items.pop(ticket or "", None)
                return None
            return dict(item)

    def failed(self, ticket: str) -> None:
        with self._lock:
            if ticket in self._items:
                self._items[ticket]["tries"] += 1

    def done(self, ticket: str) -> None:
        with self._lock:
            self._items.pop(ticket, None)
