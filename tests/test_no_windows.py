"""Programs started by the app must not open black console windows on Windows."""

import subprocess
import sys

import snapshots


def test_started_programs_get_no_console_window(monkeypatch):
    seen = {}

    class Fake:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", Fake)
    snapshots._no_console_windows()
    subprocess.Popen(["x"])
    assert seen["creationflags"] & 0x08000000
    subprocess.Popen(["x"], creationflags=1)
    assert seen["creationflags"] == 1 | 0x08000000
