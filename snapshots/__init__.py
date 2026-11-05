"""Snapshots: a self-hosted photo and video library for Windows."""

__version__ = "0.14.4"


def _no_console_windows() -> None:
    """On Windows, keep every program the app starts (ffmpeg, ffprobe, ...) from flashing a black window.

    The app has no console of its own, so Windows would open a new one for each of them.
    """
    import subprocess
    import sys

    flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    if sys.platform != "win32" or not flag or getattr(subprocess.Popen, "_snapshots_quiet", False):
        return
    original = subprocess.Popen.__init__

    def quiet_init(self, *args, **kwargs):
        kwargs["creationflags"] = kwargs.get("creationflags", 0) | flag
        original(self, *args, **kwargs)

    subprocess.Popen.__init__ = quiet_init
    subprocess.Popen._snapshots_quiet = True


_no_console_windows()
