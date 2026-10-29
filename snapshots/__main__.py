"""Command line entry point.

    python -m snapshots --library "D:\\Photos"

The same code is the Snapshots.exe: double-click it and it asks for your photo folder the
first time, remembers it, and opens your browser.
"""

import argparse
import socket
import sys
import threading
import time
import webbrowser
from pathlib import Path

import uvicorn

from . import __version__
from .app import create_app
from .config import load_config, save_library_path

FROZEN = bool(getattr(sys, "frozen", False))  # True inside the PyInstaller exe


def pick_folder() -> Path | None:
    """Ask the user for their photo folder with the normal Windows folder dialog."""
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
    except Exception:  # no tkinter, or no display
        return None
    try:
        root.withdraw()
        root.attributes("-topmost", True)
        chosen = filedialog.askdirectory(
            parent=root, title="Choose the folder that holds your photos and videos", mustexist=True
        )
    finally:
        root.destroy()
    return Path(chosen) if chosen else None


def pause_if_frozen() -> None:
    """A double-clicked exe closes its window when it exits. Wait so the message can be read."""
    if FROZEN:
        try:
            input("\nPress Enter to close this window...")
        except (EOFError, KeyboardInterrupt, OSError):
            pass


def fail(message: str, code: int = 2) -> int:
    print(message, file=sys.stderr)
    pause_if_frozen()
    return code


def open_browser_when_ready(url: str, host: str, port: int, timeout: float = 30) -> None:
    """Open the browser once the server accepts connections (runs on a helper thread)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.25)
    else:
        return
    webbrowser.open(url)


def reset_password(cfg, username: str, read_password=None) -> int:
    """For when the only administrator has forgotten their password."""
    from datetime import datetime
    from getpass import getpass

    from . import auth, db

    read_password = read_password or getpass
    problem = auth.validate_username(username)
    if problem:
        return fail(problem)
    first = read_password(f"New password for {username}: ")
    problem = auth.validate_new_password(first)
    if problem:
        return fail(problem)
    if read_password("Type it again: ") != first:
        return fail("The two passwords were different. Nothing was changed.")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    db.init(cfg.db_path)
    with db.session(cfg.db_path) as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username.strip(),)).fetchone()
        if row:
            conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (auth.hash_password(first), row["id"]))
            auth.end_all_sessions(conn, row["id"])
            print(f"Password changed for {username}. Other devices have been logged out.")
        else:
            conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, 1, ?)",
                (username.strip(), auth.hash_password(first), datetime.now().isoformat(timespec="seconds")),
            )
            print(f"Created administrator account {username}.")
    pause_if_frozen()
    return 0


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):  # odd characters in folder names must not crash printing
        try:
            stream.reconfigure(errors="replace")
        except Exception:
            pass

    p = argparse.ArgumentParser(prog="snapshots", description="Self-hosted photo and video library.")
    p.add_argument("--library", help="Folder that holds your photos and videos")
    p.add_argument("--change-library", action="store_true", help="Choose the photo folder again")
    p.add_argument("--data", help="Where to keep the database and thumbnails")
    p.add_argument("--config", help="Path to a config.toml (default: ./config.toml if present)")
    p.add_argument("--host", help="Address to listen on (default 127.0.0.1)")
    p.add_argument("--port", type=int, help="Port to listen on (default 8080)")
    p.add_argument("--no-index", action="store_true", help="Do not scan the library on start")
    p.add_argument("--reset-password", metavar="USER",
                   help="Set a new password for an account (creates it as an administrator if missing), then exit")
    p.add_argument("--open", action="store_true", help="Open the browser on start (always on in the exe)")
    p.add_argument("--no-browser", action="store_true", help="Do not open the browser on start")
    p.add_argument("--version", action="version", version=f"snapshots {__version__}")
    args = p.parse_args(argv)

    try:
        cfg = load_config(
            args.config,
            library_path=args.library,
            data_dir=args.data,
            host=args.host,
            port=args.port,
            index_on_start=False if args.no_index else None,
        )
    except (OSError, ValueError) as exc:
        return fail(f"Config error: {exc}")

    if args.reset_password:
        return reset_password(cfg, args.reset_password)

    if args.library is None and (args.change_library or cfg.library_path is None):
        chosen = pick_folder()
        if chosen is None:
            return fail(
                "No photo folder chosen.\n"
                'Run again and pick a folder, or start with:  --library "D:\\Photos"'
            )
        cfg.library_path = chosen
        saved = save_library_path(chosen, cfg.config_file)
        print(f"Remembered your choice in {saved}")

    if not cfg.library_path.is_dir():
        return fail(
            f"Library folder not found: {cfg.library_path}\n"
            "Is the drive connected? To pick a different folder, run with --change-library."
        )

    local = "localhost" if cfg.host in ("127.0.0.1", "0.0.0.0", "localhost") else cfg.host
    url = f"http://{local}:{cfg.port}"
    print(f"Snapshots {__version__}")
    print(f"  Library : {cfg.library_path}")
    print(f"  Data    : {cfg.data_dir}")
    print(f"  Open    : {url}")
    print("  Close this window to stop Snapshots.\n" if FROZEN else "  Press Ctrl+C to stop.\n")

    if (FROZEN or args.open) and not args.no_browser:
        check_host = "127.0.0.1" if cfg.host in ("0.0.0.0", "localhost") else cfg.host
        threading.Thread(
            target=open_browser_when_ready, args=(url, check_host, cfg.port), daemon=True
        ).start()

    try:
        uvicorn.run(create_app(cfg), host=cfg.host, port=cfg.port, log_level="info")
    except KeyboardInterrupt:
        pass
    except SystemExit as exc:  # uvicorn exits with 1 when it cannot start, e.g. port already taken
        if exc.code not in (0, None):
            return fail(
                f"Snapshots could not start on port {cfg.port}.\n"
                "Is it already running? Close the other window, or use --port 8081.",
                code=1,
            )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
