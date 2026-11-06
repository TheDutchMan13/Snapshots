"""Is a newer Snapshots out? Asks GitHub for the latest release now and then, and only tells an administrator."""

import json
import logging
import re
import threading
import time
import urllib.request

from . import __version__

log = logging.getLogger("snapshots.updates")

REPO = "TheDutchMan13/Snapshots"
API_URL = f"https://api.github.com/repos/{REPO}/releases/latest"
PAGE_URL = f"https://github.com/{REPO}/releases/latest"
CHECK_EVERY = 12 * 3600       # seconds between asking GitHub
RETRY_AFTER = 1800            # after a failed try (no internet, say)


def parse_version(text) -> tuple | None:
    """'v0.14.4' -> (0, 14, 4); anything that isn't a plain number version -> None."""
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", str(text or "").strip())
    return tuple(int(n) for n in match.groups()) if match else None


def is_newer(latest, current=__version__) -> bool:
    a, b = parse_version(latest), parse_version(current)
    return a is not None and b is not None and a > b


def fetch_latest(timeout: float = 10) -> dict | None:
    """The newest published release: {"version", "url"}, or None if it can't be read."""
    req = urllib.request.Request(API_URL, headers={"User-Agent": "Snapshots photo library",
                                                   "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read(200_000).decode("utf-8", "replace"))
    tag = data.get("tag_name")
    if parse_version(tag) is None:
        return None
    return {"version": ".".join(str(n) for n in parse_version(tag)), "url": PAGE_URL}


class UpdateChecker:
    """Keeps the last answer. Never makes a web request unless asked to, and never makes one on the caller's thread."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled
        self.latest: dict | None = None
        self.checked_at = 0.0
        self._wait_until = 0.0
        self._busy = False
        self._lock = threading.Lock()

    def refresh_if_due(self) -> None:
        with self._lock:
            if not self.enabled or self._busy or time.time() < self._wait_until:
                return
            self._busy = True
        threading.Thread(target=self._run, name="snapshots-updates", daemon=True).start()

    def _run(self) -> None:
        try:
            found = fetch_latest()
            ok = True
        except Exception as exc:       # no internet, GitHub busy, ...: try again later, quietly
            log.info("could not look for a new version: %s", exc)
            found, ok = None, False
        with self._lock:
            self._busy = False
            self._wait_until = time.time() + (CHECK_EVERY if ok else RETRY_AFTER)
            if ok:
                self.latest, self.checked_at = found, time.time()

    def status(self) -> dict:
        latest = self.latest if self.enabled else None
        return {"enabled": self.enabled, "current": __version__,
                "latest": latest["version"] if latest else None, "url": latest["url"] if latest else None,
                "newer": bool(latest and is_newer(latest["version"]))}
