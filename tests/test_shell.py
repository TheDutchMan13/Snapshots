"""The app window and the no-console behaviour of the exe.

The window itself needs Windows, so these tests cover everything around it: choosing how to show the
app, running the server on a thread, what happens when the port is taken or the window cannot open,
and printing when there is no console. The real window is checked by the Windows build on GitHub.
"""

import socket
import sys
import threading
import time
from argparse import Namespace

import pytest

from snapshots import __main__ as cli
from snapshots import shell


@pytest.fixture(autouse=True)
def reset_console():
    shell.console = "own"
    yield
    shell.console = "own"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def args(**kw):
    base = dict(no_browser=False, open=False, window=False)
    base.update(kw)
    return Namespace(**base)


# ------------------------------------------------------------------ how to show the app

@pytest.mark.parametrize("flags, frozen, expected", [
    ({}, False, "none"),                      # python -m snapshots: server only
    ({}, True, "window"),                     # double-clicked exe: its own window
    ({"window": True}, False, "window"),
    ({"open": True}, False, "browser"),
    ({"open": True}, True, "browser"),        # the exe can still be told to use the browser
    ({"no_browser": True}, True, "none"),     # no_browser wins over everything
    ({"no_browser": True, "open": True}, False, "none"),
])
def test_choose_ui(flags, frozen, expected):
    assert cli.choose_ui(args(**flags), frozen) == expected


# ------------------------------------------------------------------ no console

def test_printing_never_crashes_without_a_console(monkeypatch):
    monkeypatch.setattr(sys, "stdout", None)
    monkeypatch.setattr(sys, "stderr", None)
    assert shell.prepare_streams([]) == "none"
    print("this must not raise")
    print("nor this", file=sys.stderr)


def test_prepare_streams_leaves_a_real_console_alone():
    assert shell.prepare_streams([]) == "own"


def test_log_file_collects_output(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    path = shell.log_to_file(tmp_path / "data")
    print("hello from the exe")
    sys.stdout.flush()
    assert path == tmp_path / "data" / "snapshots.log"
    assert "hello from the exe" in path.read_text(encoding="utf-8")


def test_log_file_starts_fresh_when_big(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "stdout", sys.stdout)
    monkeypatch.setattr(sys, "stderr", sys.stderr)
    log = tmp_path / "snapshots.log"
    log.write_text("x" * (shell.LOG_LIMIT + 1))
    shell.log_to_file(tmp_path)
    assert (tmp_path / "snapshots.old.log").exists() and log.stat().st_size == 0


def test_errors_appear_in_a_message_box_when_there_is_no_console(monkeypatch):
    shown = []
    monkeypatch.setattr(shell, "show_message", lambda text, title="", error=False: shown.append((text, error)))
    shell.console = "none"
    assert cli.fail("Library folder not found: X") == 2
    assert shown == [("Library folder not found: X", True)]


def test_errors_are_only_printed_when_there_is_a_console(monkeypatch, capsys):
    shown = []
    monkeypatch.setattr(shell, "show_message", lambda *a, **k: shown.append(a))
    assert cli.fail("Library folder not found: X") == 2
    assert shown == [] and "not found" in capsys.readouterr().err


def test_password_reset_without_a_console_uses_a_dialog(tmp_path, monkeypatch):
    from snapshots.config import Config

    shell.console = "none"
    shown, asked = [], []
    monkeypatch.setattr(shell, "show_message", lambda text, title="", error=False: shown.append(text))
    answers = iter(["a new password", "a new password"])
    monkeypatch.setattr(shell, "ask_password", lambda prompt: asked.append(prompt) or next(answers))
    cfg = Config(data_dir=tmp_path / "data")
    assert cli.reset_password(cfg, "admin") == 0
    assert len(asked) == 2 and any("Created administrator" in t for t in shown)


# ------------------------------------------------------------------ WebView2 check

def test_webview2_is_not_reported_on_other_systems():
    if sys.platform != "win32":
        assert shell.webview2_installed() is False
        assert shell.window_available() is False


# ------------------------------------------------------------------ the server thread

def make_cfg(tmp_path, port):
    from snapshots.config import Config

    photos = tmp_path / "Photos"
    photos.mkdir(exist_ok=True)
    return Config(library_path=photos, data_dir=tmp_path / "data", port=port,
                  index_on_start=False, scan_interval_minutes=0, faces=False, places=False)


def test_server_starts_on_a_thread_and_stops_cleanly(tmp_path):
    port = free_port()
    server, thread = cli.start_server(make_cfg(tmp_path, port))
    try:
        assert cli.wait_until_up(server, thread, timeout=30)
        assert shell.snapshots_running("127.0.0.1", port)
    finally:
        cli.stop_server(server, thread)
    assert not thread.is_alive()
    assert not shell.snapshots_running("127.0.0.1", port)


def test_a_taken_port_is_noticed_instead_of_hanging(tmp_path):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        server, thread = cli.start_server(make_cfg(tmp_path, port))
        assert cli.wait_until_up(server, thread, timeout=30) is False
        assert not shell.snapshots_running("127.0.0.1", port)  # something else owns the port


# ------------------------------------------------------------------ the whole start-up

@pytest.fixture
def start(tmp_path, monkeypatch):
    """Run main() as the exe would, with the window and browser replaced by recorders."""
    log = {"window": [], "browser": [], "messages": []}
    monkeypatch.setattr(shell, "window_available", lambda: True)
    monkeypatch.setattr(shell, "log_to_file", lambda data_dir: log.setdefault("logged", []).append(data_dir))
    monkeypatch.setattr(cli.webbrowser, "open", lambda url: log["browser"].append(url))
    monkeypatch.setattr(shell, "show_message",
                        lambda text, title="", error=False: log["messages"].append((text, error)))

    def run(*extra, port=None):
        port = port or free_port()
        photos = tmp_path / "Photos"
        photos.mkdir(exist_ok=True)
        log["port"] = port
        code = cli.main(["--library", str(photos), "--data", str(tmp_path / "data"), "--port", str(port),
                         "--no-index", *extra])
        return code

    return run, log


def test_window_mode_opens_a_window_on_a_running_server_and_stops_it_afterwards(start, monkeypatch):
    run, log = start

    def fake_window(url, storage_dir, server_thread=None, title="Snapshots"):
        log["window"].append(url)
        assert shell.snapshots_running("127.0.0.1", log["port"])  # the page can be loaded
        assert server_thread.is_alive()
        log["thread"] = server_thread
        log["storage"] = storage_dir

    monkeypatch.setattr(shell, "run_window", fake_window)
    assert run("--window") == 0
    assert log["window"] == [f"http://127.0.0.1:{log['port']}"]
    assert log["storage"].name == "webview"  # login cookie is kept in the data folder
    assert log["browser"] == []
    assert not log["thread"].is_alive()  # closing the window stopped the server
    assert not shell.snapshots_running("127.0.0.1", log["port"])


def test_a_window_that_cannot_open_falls_back_to_the_browser(start, monkeypatch):
    run, log = start

    def broken_window(*a, **k):
        raise RuntimeError("no WebView2")

    monkeypatch.setattr(shell, "run_window", broken_window)
    shell.console = "none"
    assert run("--window") == 0
    assert log["browser"] and log["browser"][0].startswith("http://localhost:")
    assert "OK to stop" in log["messages"][0][0]  # still a way to stop it
    assert not shell.snapshots_running("127.0.0.1", log["port"])


def test_browser_flag_is_the_same_as_open(start):
    run, log = start
    shell.console = "none"
    assert run("--browser") == 0
    assert log["browser"] and log["window"] == []


def test_the_log_file_is_used_when_there_is_no_console(start, monkeypatch, tmp_path):
    run, log = start
    shell.console = "none"
    monkeypatch.setattr(shell, "run_window", lambda *a, **k: None)
    assert run("--window") == 0
    assert log["logged"] == [tmp_path / "data"]


def test_without_webview2_the_browser_is_used(start, monkeypatch):
    run, log = start
    monkeypatch.setattr(shell, "window_available", lambda: False)
    shell.console = "none"
    assert run("--window") == 0
    assert log["browser"] and log["browser"][0].startswith("http://localhost:")
    assert log["window"] == []
    assert len(log["messages"]) == 1 and "Press OK to stop" in log["messages"][0][0]


def test_without_a_console_the_browser_message_is_how_you_stop(start, monkeypatch):
    run, log = start
    shell.console = "none"
    assert run("--open") == 0
    assert log["browser"] and "OK to stop" in log["messages"][0][0]


def test_starting_twice_shows_the_copy_that_is_already_running(start, monkeypatch, tmp_path):
    run, log = start
    port = free_port()
    first, thread = cli.start_server(make_cfg(tmp_path, port))
    try:
        assert cli.wait_until_up(first, thread, timeout=30)
        monkeypatch.setattr(shell, "run_window", lambda url, storage, *a, **k: log["window"].append(url))
        assert run("--window", port=port) == 0
        assert log["window"] == [f"http://127.0.0.1:{port}"]
        assert shell.snapshots_running("127.0.0.1", port)  # the first copy is untouched
    finally:
        cli.stop_server(first, thread)


def test_a_taken_port_is_reported(start, monkeypatch, capsys):
    run, log = start
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        taken.listen()
        port = taken.getsockname()[1]
        monkeypatch.setattr(shell, "run_window", lambda *a, **k: pytest.fail("no window without a server"))
        assert run("--window", port=port) == 1
    assert "could not start on port" in capsys.readouterr().err


def test_if_the_server_dies_while_the_window_is_open_you_are_told(start, monkeypatch, capsys):
    run, log = start
    real_start = cli.start_server

    def remember(cfg):
        log["server"], thread = real_start(cfg)
        return log["server"], thread

    def window_during_a_crash(url, storage, server_thread=None, title="Snapshots"):
        log["server"].should_exit = True  # the server goes away behind the window's back
        server_thread.join(20)

    monkeypatch.setattr(cli, "start_server", remember)
    monkeypatch.setattr(shell, "run_window", window_during_a_crash)
    assert run("--window") == 1
    assert "stopped unexpectedly" in capsys.readouterr().err
