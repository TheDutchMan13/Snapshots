"""One background thread that keeps the library up to date.

In order of priority it:
1. rescans the folder when asked, and automatically every `scan_interval_minutes`;
2. finds videos inside photos indexed by older versions (motion photos), and looks up place names for photos with a GPS position;
3. measures photos for the Clean up page (copies, bursts, blur, darkness);
4. finds faces in new photos and groups them into people;
5. prepares search by content;
6. reads text in photos.
Steps 2-6 work in small batches, so a rescan never waits long.
"""

import logging
import threading
import time
from datetime import datetime, timedelta

from . import db
from .config import Config
from .analysis import QualityJob
from .clipsearch import ClipJob
from .faces import FacesJob
from .linking import MotionJob
from .ocr import OcrJob
from .places import PlacesJob
from .scanner import Scanner

log = logging.getLogger("snapshots.worker")


class Worker:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.scanner = Scanner(cfg)
        self.places = PlacesJob(cfg)
        self.motion = MotionJob(cfg)
        self.faces = FacesJob(cfg)
        self.quality = QualityJob(cfg)
        self.clip = ClipJob(cfg)
        self.ocr = OcrJob(cfg)
        self._wake = threading.Event()
        self._scan_requested = False
        self._remove_missing = False
        self._stopping = False
        self._thread: threading.Thread | None = None
        self.next_scan_at: float | None = None
        self.scans_finished = 0

    # ------------------------------------------------------------ control

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stopping = False
        if self.cfg.library_path and self.cfg.index_on_start:
            self.request_scan()
        else:
            self._schedule_next()
        self._thread = threading.Thread(target=self._loop, name="snapshots-worker", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 10) -> None:
        self._stopping = True
        self._wake.set()
        if self._thread:
            self._thread.join(timeout)

    def request_scan(self, automatic: bool = False, remove_missing: bool = False) -> bool:
        """Ask for a rescan. Returns False if one is already queued or running.

        remove_missing: also forget missing files even if that is more than half the library.
        """
        if self.scanner.state.running:
            return False
        self._remove_missing = remove_missing
        self.scanner.mark_queued(automatic)
        self._scan_requested = True
        self._wake.set()
        return True

    def poke(self) -> None:
        """Something changed (e.g. a photo was restored): look for work now."""
        self._wake.set()

    def status(self) -> dict:
        return {
            "scan": self.scanner.snapshot(),
            "next_scan_at": (
                datetime.fromtimestamp(self.next_scan_at).isoformat(timespec="seconds")
                if self.next_scan_at else None
            ),
            "places": dict(self.places.state, enabled=self.cfg.places),
            "faces": dict(self.faces.state, enabled=self.cfg.faces),
            "checks": dict(self.quality.state, enabled=self.cfg.photo_checks),
            "content": dict(self.clip.state, enabled=self.cfg.smart_search),
            "text": dict(self.ocr.state, enabled=self.cfg.text_search),
        }

    def _jobs(self):
        """Background jobs in order of priority, with whether each is switched on."""
        return [
            (self.motion, True),
            (self.places, self.cfg.places),
            (self.quality, self.cfg.photo_checks),
            (self.faces, self.cfg.faces),
            (self.clip, self.cfg.smart_search),
            (self.ocr, self.cfg.text_search),
        ]

    # ------------------------------------------------------------ internals

    def _schedule_next(self) -> None:
        minutes = self.cfg.scan_interval_minutes
        self.next_scan_at = time.time() + minutes * 60 if minutes and minutes > 0 else None

    def _interrupted(self) -> bool:
        return self._stopping or self._scan_requested

    def _loop(self) -> None:
        conn = None
        while not self._stopping:
            try:
                if conn is None:
                    conn = db.connect(self.cfg.db_path)
                now = time.time()
                if not self._scan_requested and self.next_scan_at and now >= self.next_scan_at:
                    self.request_scan(automatic=True)
                if self._scan_requested:
                    self._scan_requested = False
                    remove_missing, self._remove_missing = self._remove_missing, False
                    self.scanner.run(automatic=self.scanner.state.automatic, remove_missing=remove_missing)
                    self.scans_finished += 1
                    self._schedule_next()
                    continue

                did_work = False
                for job, enabled in self._jobs():
                    if enabled and job.run_batch(conn, now, self._interrupted):
                        did_work = True
                        break
                if did_work:
                    continue

                # Nothing to do: sleep until the next scan, a request, or an hour (to retry
                # downloads that failed).
                wait = 3600.0
                if self.next_scan_at:
                    wait = max(0.0, min(wait, self.next_scan_at - time.time()))
                for job, enabled in self._jobs():
                    if enabled and job.retry_at > now:
                        wait = min(wait, job.retry_at - now)
                self._wake.wait(timeout=wait)
                self._wake.clear()
            except Exception:  # never let the worker die; try again shortly
                log.exception("background worker error")
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
                conn = None
                self._wake.wait(timeout=30)
                self._wake.clear()
        if conn is not None:
            conn.close()
