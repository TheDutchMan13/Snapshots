"""Webhooks: Snapshots tells other programs (Home Assistant, a script, a chat bot) when something happens.

An administrator adds a web address and picks events. Each event is sent there as a JSON POST:

    {"event": "assets.added", "at": "2026-10-30T09:12:00", "data": {...}}

with the headers X-Snapshots-Event (the event name) and X-Snapshots-Signature ("sha256=" and the
HMAC-SHA256 of the body, made with the webhook's secret), so the receiver can check it came from
Snapshots. Deliveries are stored in the database first, so none are lost when Snapshots closes, and a
delivery that fails is tried again later (after 30 seconds, then 2 and 10 minutes, 1 and 6 hours).

Items in someone's locked folder never appear in an event.
"""

import hashlib
import hmac
import json
import logging
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime

from . import __version__, db

log = logging.getLogger("snapshots.webhooks")

EVENTS = {
    "assets.added": "New photos and videos",
    "album.added": "Photos added to an album",
    "comment.added": "New comments",
    "person.named": "A person is named",
    "import.finished": "An import finished",
    "offsite.finished": "The offsite copy finished",
    "backup.failed": "A database backup failed",
}
RETRY_AFTER = [30, 120, 600, 3600, 6 * 3600]   # seconds; after the last one a delivery is given up
MAX_ITEMS = 200                                # items listed in one event (more are sent as more events)
KEEP_DAYS = 7                                  # finished deliveries are forgotten after this long

_sender = None


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def new_secret() -> str:
    return "whsec_" + secrets.token_urlsafe(24)


def sign(secret: str, body: bytes) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def subscribed(conn, event: str) -> list[int]:
    out = []
    for row in conn.execute("SELECT id, events FROM webhooks WHERE enabled = 1"):
        try:
            events = json.loads(row["events"])
        except ValueError:
            continue
        if event in events or "*" in events:
            out.append(row["id"])
    return out


def emit(conn, event: str, data: dict, only: int | None = None) -> int:
    """Queue an event for every webhook that wants it (within the caller's transaction). Returns how many."""
    targets = [only] if only is not None else subscribed(conn, event)
    if not targets:
        return 0
    payload = json.dumps({"event": event, "at": _now(), "data": data}, ensure_ascii=False)
    for webhook_id in targets:
        conn.execute(
            "INSERT INTO webhook_deliveries (webhook_id, event, payload, created_at, next_try_at) VALUES (?, ?, ?, ?, ?)",
            (webhook_id, event, payload, _now(), time.time()))
    if _sender is not None:
        _sender.wake()
    return len(targets)


def item_json(row) -> dict:
    return {"id": row["id"], "filename": row["filename"], "kind": row["kind"], "taken_at": row["taken_at"],
            "path": row["path"]}


def emit_added(conn, ids, how: str) -> None:
    """assets.added for new items (in groups of MAX_ITEMS). Locked items are left out."""
    ids = list(ids)
    if not ids or not subscribed(conn, "assets.added"):
        return
    for start in range(0, len(ids), MAX_ITEMS):
        chunk = ids[start:start + MAX_ITEMS]
        rows = conn.execute(
            "SELECT id, filename, kind, taken_at, path FROM assets"
            f" WHERE locked_by IS NULL AND id IN ({','.join('?' * len(chunk))}) ORDER BY taken_at", chunk).fetchall()
        if rows:
            emit(conn, "assets.added", {"how": how, "count": len(rows), "items": [item_json(r) for r in rows]})


def emit_safely(cfg, event: str, data: dict) -> None:
    """emit() from code that has no database connection open (background jobs)."""
    try:
        with db.session(cfg.db_path) as conn:
            emit(conn, event, data)
    except Exception:
        log.exception("could not queue the %s webhook", event)


class Sender:
    """Delivers queued events, one at a time, in a thread of its own."""

    def __init__(self, cfg, opener=None):
        self.cfg = cfg
        self.opener = opener or self._post
        self._wake = threading.Event()
        self._stop = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        global _sender
        _sender = self
        if self._thread and self._thread.is_alive():
            return
        self._stop = False
        self._thread = threading.Thread(target=self._loop, name="snapshots-webhooks", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        global _sender
        self._stop = True
        self._wake.set()
        if _sender is self:
            _sender = None

    def wake(self) -> None:
        self._wake.set()

    @staticmethod
    def _post(url: str, body: bytes, headers: dict) -> int:
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def _loop(self) -> None:
        while not self._stop:
            try:
                busy = self.deliver_due()
            except Exception:
                log.exception("webhook delivery error")
                busy = False
            if busy:
                continue
            self._wake.wait(timeout=30)
            if self._wake.is_set():
                self._wake.clear()
                time.sleep(0.5)   # let the change that queued the event finish saving

    def deliver_due(self, limit: int = 20) -> bool:
        """Send what is due. Returns True if there may be more to send now."""
        with db.session(self.cfg.db_path) as conn:
            rows = conn.execute(
                "SELECT d.id, d.event, d.payload, d.attempts, w.id AS webhook_id, w.url, w.secret"
                " FROM webhook_deliveries d JOIN webhooks w ON w.id = d.webhook_id"
                " WHERE d.delivered_at IS NULL AND d.failed = 0 AND d.next_try_at <= ? AND w.enabled = 1"
                " ORDER BY d.id LIMIT ?", (time.time(), limit)).fetchall()
            if not rows:
                cutoff = datetime.fromtimestamp(time.time() - KEEP_DAYS * 86400).isoformat(timespec="seconds")
                conn.execute("DELETE FROM webhook_deliveries WHERE (delivered_at IS NOT NULL OR failed = 1)"
                             " AND created_at < ?", (cutoff,))
                return False
        for row in rows:
            if self._stop:
                return False
            body = row["payload"].encode()
            headers = {"Content-Type": "application/json", "User-Agent": f"Snapshots/{__version__}",
                       "X-Snapshots-Event": row["event"], "X-Snapshots-Signature": sign(row["secret"], body),
                       "X-Snapshots-Delivery": str(row["id"])}
            try:
                status = self.opener(row["url"], body, headers)
                ok, result = 200 <= int(status) < 300, str(status)
            except Exception as exc:
                ok, result = False, (str(getattr(exc, "reason", "")) or str(exc))[:200]
            with db.session(self.cfg.db_path) as conn:
                conn.execute("UPDATE webhooks SET last_at = ?, last_status = ? WHERE id = ?",
                             (_now(), result, row["webhook_id"]))
                if ok:
                    conn.execute("UPDATE webhook_deliveries SET delivered_at = ?, attempts = attempts + 1, error = NULL"
                                 " WHERE id = ?", (_now(), row["id"]))
                else:
                    attempts = row["attempts"] + 1
                    give_up = attempts > len(RETRY_AFTER)
                    wait = RETRY_AFTER[min(attempts, len(RETRY_AFTER)) - 1]
                    conn.execute("UPDATE webhook_deliveries SET attempts = ?, failed = ?, error = ?, next_try_at = ?"
                                 " WHERE id = ?", (attempts, int(give_up), result, time.time() + wait, row["id"]))
        return len(rows) == limit
