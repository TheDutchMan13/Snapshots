"""Copies of videos that the app can play, made once and kept in the data folder.

Phones save HEVC (H.265) videos, and some cameras AVI or MKV files, which the app window can't play.
Snapshots converts such a video to H.264 the first time it is opened, using the graphics card when
ffmpeg can (NVIDIA, AMD or Intel), and keeps the copy until the cache grows past video_cache_gb.
"""

import logging
import queue
import threading
import time
from pathlib import Path

from . import media

log = logging.getLogger("snapshots.videos")


class Converter:
    def __init__(self, cfg):
        self.cfg = cfg
        self.jobs: dict[int, dict] = {}     # asset id -> {"progress", "error", "started"}
        self._queue: queue.Queue = queue.Queue()
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def state(self, asset_id: int) -> dict | None:
        with self._lock:
            job = self.jobs.get(asset_id)
            return dict(job) if job else None

    def request(self, asset_id: int, src: Path, codec: str | None, duration: float | None) -> dict:
        """Start converting (if not already). Returns the job's state."""
        with self._lock:
            job = self.jobs.get(asset_id)
            if job and not job.get("error"):
                return dict(job)
            job = {"progress": 0.0, "error": None, "started": time.time()}
            self.jobs[asset_id] = job
            self._queue.put((asset_id, src, codec, duration))
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="snapshots-videos", daemon=True)
                self._thread.start()
            return dict(job)

    def _run(self) -> None:
        while True:
            try:
                asset_id, src, codec, duration = self._queue.get(timeout=60)
            except queue.Empty:
                return
            dst = self.cfg.video_path(asset_id)

            def progress(fraction, asset_id=asset_id):
                with self._lock:
                    if asset_id in self.jobs:
                        self.jobs[asset_id]["progress"] = round(fraction, 3)

            try:
                dst.parent.mkdir(parents=True, exist_ok=True)
                ok = media.convert_video(src, dst, self.cfg.video_encoder, progress, codec=codec, duration=duration)
            except Exception as exc:  # never let the thread die
                log.exception("could not convert %s", src)
                ok = False
            with self._lock:
                if ok:
                    self.jobs.pop(asset_id, None)
                else:
                    self.jobs[asset_id]["error"] = "This video could not be converted."
            if ok:
                self.trim()

    def trim(self) -> None:
        """Remove the least recently made copies once the cache is bigger than allowed."""
        limit = max(0.0, self.cfg.video_cache_gb) * 1024 ** 3
        try:
            files = [(p.stat().st_mtime, p.stat().st_size, p) for p in self.cfg.video_dir.glob("*.mp4")]
        except OSError:
            return
        total = sum(size for _, size, _ in files)
        for _, size, p in sorted(files):
            if total <= limit:
                break
            try:
                p.unlink()
                total -= size
            except OSError:
                pass
