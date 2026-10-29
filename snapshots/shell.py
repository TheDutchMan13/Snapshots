"""The Windows app shell: the native window, and what a program without a console window needs.

Snapshots.exe is built without a console window, so it behaves like any other Windows program.
That means there is nowhere to print to, so output goes to a log file and problems are shown in
message boxes. Run from source (python -m snapshots) nothing here changes: you still get a terminal.
"""

import ctypes
import os
import sys
import threading
import urllib.request
from pathlib import Path

# How this process talks to the user:
#   "own"      - it has its own console (running from source, or a console build of the exe)
#   "attached" - the exe was started from a terminal, and borrows that terminal
#   "none"     - the exe was double-clicked: no console at all, so use the log file and message boxes
console = "own"

LOG_NAME = "snapshots.log"
LOG_LIMIT = 1_000_000  # start a fresh log when the old one gets bigger than this (bytes)

# Microsoft's id for the "Evergreen" WebView2 runtime, which draws the app window.
_WEBVIEW2_ID = "{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
_WEBVIEW2_MIN_MAJOR = 86  # what pywebview needs


# ---------------------------------------------------------------- console and messages

def prepare_streams(argv) -> str:
    """Make sure printing never crashes. Call this first thing. Returns the new `console` value."""
    if sys.stdout is not None and sys.stderr is not None:
        return "own"
    if os.name == "nt" and argv:
        # Started from a terminal with options (--help, --reset-password ...): use that terminal.
        try:
            if ctypes.windll.kernel32.AttachConsole(0xFFFFFFFF):  # ATTACH_PARENT_PROCESS
                out = open("CONOUT$", "w", encoding="utf-8", errors="replace", buffering=1)
                sys.stdout = sys.stderr = out
                sys.stdin = open("CONIN$", "r", encoding="utf-8", errors="replace")
                return "attached"
        except (OSError, AttributeError):
            pass
    sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")
    sys.stdin = sys.stdin or open(os.devnull, "r")
    return "none"


def log_to_file(data_dir: Path) -> Path | None:
    """Send everything that would be printed to a log file instead. Returns its path."""
    path = Path(data_dir) / LOG_NAME
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_name("snapshots.old.log"))
        stream = open(path, "a", encoding="utf-8", errors="replace", buffering=1)
    except OSError:
        return None
    sys.stdout = sys.stderr = stream
    return path


def show_message(text: str, title: str = "Snapshots", error: bool = False) -> None:
    """A message box (Windows only). Waits until the person presses OK."""
    if os.name != "nt":
        return
    try:
        icon = 0x10 if error else 0x40  # MB_ICONERROR / MB_ICONINFORMATION
        ctypes.windll.user32.MessageBoxW(None, text, title, icon | 0x40000)  # | MB_TOPMOST
    except Exception:
        pass


def ask_password(prompt: str) -> str:
    """Ask for a password in a small window (used when there is no console to type in)."""
    import tkinter as tk
    from tkinter import simpledialog

    root = tk.Tk()
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        return simpledialog.askstring("Snapshots", prompt, show="*", parent=root) or ""
    finally:
        root.destroy()


# ---------------------------------------------------------------- the app window

def webview2_installed() -> bool:
    """Is Microsoft's WebView2 runtime present? (Windows 11 and current Windows 10 ship it.)

    Without it pywebview would quietly fall back to the old Internet Explorer engine, which cannot
    run this interface, so we check first and use the browser instead.
    """
    if os.name != "nt":
        return False
    try:
        import winreg
    except ImportError:
        return False
    places = [
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
        (winreg.HKEY_LOCAL_MACHINE, rf"SOFTWARE\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
        (winreg.HKEY_CURRENT_USER, rf"Software\Microsoft\EdgeUpdate\Clients\{_WEBVIEW2_ID}"),
    ]
    for hive, key in places:
        try:
            with winreg.OpenKey(hive, key) as handle:
                version = str(winreg.QueryValueEx(handle, "pv")[0])
        except OSError:
            continue
        try:
            if int(version.split(".")[0]) >= _WEBVIEW2_MIN_MAJOR:
                return True
        except ValueError:
            continue
    return False


def window_available() -> bool:
    """Can this computer show the app window? (pywebview installed and WebView2 present.)"""
    try:
        import webview  # noqa: F401
    except Exception:
        return False
    return webview2_installed()


def run_window(url: str, storage_dir: Path, server_thread: threading.Thread | None = None,
               title: str = "Snapshots") -> None:
    """Show the app in its own window. Returns when the window is closed."""
    import webview

    # Without this, the viewer's Download button silently does nothing.
    webview.settings["ALLOW_DOWNLOADS"] = True

    Path(storage_dir).mkdir(parents=True, exist_ok=True)
    window = webview.create_window(title, url, width=1360, height=860, min_size=(520, 480))
    closed = threading.Event()
    window.events.closed += closed.set

    if server_thread is not None:
        def close_if_server_stops():
            server_thread.join()
            if not closed.is_set():
                try:
                    window.destroy()
                except Exception:
                    pass

        threading.Thread(target=close_if_server_stops, name="snapshots-watch", daemon=True).start()

    # private_mode=False keeps the login cookie between launches (the default forgets it on close).
    webview.start(private_mode=False, storage_path=str(storage_dir))


def snapshots_running(host: str, port: int) -> bool:
    """Is a Snapshots server already answering on this address?"""
    try:
        direct = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # never via a system proxy
        with direct.open(f"http://{host}:{port}/api/health", timeout=2) as response:
            body = response.read(2000).decode("utf-8", "replace")
    except Exception:
        return False
    return '"ok":true' in body.replace(" ", "") and '"version"' in body
