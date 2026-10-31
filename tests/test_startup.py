"""Starting with Windows (optional): the Run-key command, the settings, the hidden mode, and stopping it."""

import socket
import sys
from argparse import Namespace
from pathlib import Path

import pytest

from snapshots import __main__ as cli
from snapshots import shell, startup
from snapshots.config import Config
from helpers import ADMIN, add_member, login, new_client, setup_admin


class FakeRegistry:
    def __init__(self):
        self.value = None
        self.off_in_windows = False

    def switched_off_in_windows(self):
        return self.off_in_windows

    def clear_switched_off(self):
        self.off_in_windows = False

    def get(self):
        return self.value

    def set(self, command):
        self.value = command

    def delete(self):
        self.value = None


@pytest.fixture
def reg(monkeypatch):
    fake = FakeRegistry()
    monkeypatch.setattr(startup, "registry", fake)
    monkeypatch.setattr(startup, "supported", lambda: True)
    return fake


@pytest.fixture(autouse=True)
def reset_console():
    shell.console = "own"
    yield
    shell.console = "own"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_the_command_for_the_exe_runs_it_hidden(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\Snapshots\Snapshots.exe")
    config = tmp_path / "config.toml"
    config.write_text("")
    cmd = startup.command(config)
    assert cmd.startswith('"C:\\Apps\\Snapshots\\Snapshots.exe" --background')
    assert f'--config "{config.resolve()}"' in cmd          # Windows starts programs in another folder
    assert startup.command() == '"C:\\Apps\\Snapshots\\Snapshots.exe" --background'


def test_the_command_from_source_names_the_script_in_full(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    cmd = startup.command()
    script = (Path(startup.__file__).resolve().parent.parent / "run_snapshots.py")
    assert f'"{script}" --background' in cmd                 # works from any starting folder


def test_without_a_config_file_the_folders_are_named(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "executable", r"C:\Apps\Snapshots.exe")
    cmd = startup.command(None, tmp_path / "Photos", tmp_path / "data")
    assert f'--library "{(tmp_path / "Photos").resolve()}"' in cmd
    assert f'--data "{(tmp_path / "data").resolve()}"' in cmd
    assert "--library" not in startup.command(tmp_path / "c.toml", tmp_path / "Photos")   # the file says it


def cfg_for(tmp_path):
    return Config(library_path=tmp_path / "Photos", data_dir=tmp_path / "data")


def test_status_enable_and_disable(reg, tmp_path):
    cfg = cfg_for(tmp_path)
    assert startup.status(cfg) == {"supported": True, "enabled": False, "current": False, "command": None}
    startup.enable(cfg)
    st = startup.status(cfg)
    assert st["enabled"] and st["current"] and st["command"] == reg.value
    reg.value = '"D:\\old\\Snapshots.exe" --background'      # another (or moved) copy
    st = startup.status(cfg)
    assert st["enabled"] and not st["current"]
    startup.disable()
    startup.disable()                                         # turning off twice is fine
    assert not startup.status(cfg)["enabled"]


def test_switched_off_in_windows_shows_as_off_and_turning_on_undoes_it(reg, tmp_path):
    cfg = cfg_for(tmp_path)
    startup.enable(cfg)
    reg.off_in_windows = True                                 # Settings > Apps > Startup, or Task Manager
    assert startup.status(cfg)["enabled"] is False
    startup.enable(cfg)
    assert startup.status(cfg)["enabled"] is True and reg.off_in_windows is False


def test_not_supported_off_windows(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "supported", lambda: False)
    assert startup.status(cfg_for(tmp_path))["supported"] is False


def test_settings_turn_it_on_and_off_and_it_is_off_by_default(reg, tmp_path):
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    (tmp_path / "Photos").mkdir()
    setup_admin(c)
    s = c.get("/api/settings").json()["startup"]
    assert s["supported"] and not s["enabled"] and not s["can_stop"]
    r = c.put("/api/settings/startup", json={"enabled": True})
    assert r.status_code == 200 and r.json()["enabled"] and r.json()["current"]
    assert reg.value and "--background" in reg.value
    assert c.put("/api/settings/startup", json={"enabled": False}).json()["enabled"] is False
    assert reg.value is None


def test_only_an_administrator_can_change_it(reg, tmp_path):
    (tmp_path / "Photos").mkdir()
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    setup_admin(c)
    add_member(c, "sam", "a long enough password")
    c.post("/api/auth/logout")
    login(c, "sam", "a long enough password")
    assert c.put("/api/settings/startup", json={"enabled": True}).status_code == 403
    assert c.post("/api/quit").status_code == 403
    assert reg.value is None


def test_a_registry_error_is_explained(reg, monkeypatch, tmp_path):
    (tmp_path / "Photos").mkdir()
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    setup_admin(c)

    def refuse(command):
        raise PermissionError("Access is denied")

    reg.set = refuse
    r = c.put("/api/settings/startup", json={"enabled": True})
    assert r.status_code == 409 and "Access is denied" in r.json()["detail"]


def test_off_windows_the_switch_is_refused(monkeypatch, tmp_path):
    monkeypatch.setattr(startup, "supported", lambda: False)
    (tmp_path / "Photos").mkdir()
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    setup_admin(c)
    assert c.put("/api/settings/startup", json={"enabled": True}).status_code == 400


def test_the_window_copy_cannot_be_stopped_from_settings(tmp_path):
    (tmp_path / "Photos").mkdir()
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    setup_admin(c)
    r = c.post("/api/quit")
    assert r.status_code == 409 and "window" in r.json()["detail"]


# ------------------------------------------------------------------ the hidden mode

def test_background_means_no_window_and_no_browser():
    assert cli.choose_ui(Namespace(no_browser=False, open=False, window=True, background=True), True) == "none"


def make_cfg(tmp_path, port):
    from snapshots.config import Config

    photos = tmp_path / "Photos"
    photos.mkdir(exist_ok=True)
    return Config(library_path=photos, data_dir=tmp_path / "data", port=port,
                  index_on_start=False, scan_interval_minutes=0, faces=False, places=False)


def test_a_background_server_can_be_stopped_by_an_administrator(tmp_path):
    port = free_port()
    cfg = make_cfg(tmp_path, port)
    server, thread = cli.start_server(cfg, allow_quit=True)
    try:
        assert cli.wait_until_up(server, thread, timeout=30)
        import httpx

        base = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base, headers={"X-Snapshots-Request": "1"}) as c:
            assert c.post("/api/auth/setup", json={"username": ADMIN[0], "password": ADMIN[1]}).status_code == 200
            assert c.get("/api/settings").json()["startup"]["can_stop"] is True
            assert c.post("/api/quit").json() == {"stopping": True}
        thread.join(20)
        assert not thread.is_alive()
    finally:
        cli.stop_server(server, thread)


def test_background_when_already_running_exits_quietly(tmp_path, capsys, monkeypatch):
    port = free_port()
    first, thread = cli.start_server(make_cfg(tmp_path, port))
    monkeypatch.setattr(shell, "show_message", lambda *a, **k: pytest.fail("no message box at sign-in"))
    try:
        assert cli.wait_until_up(first, thread, timeout=30)
        assert cli.run_background(make_cfg(tmp_path, port), "127.0.0.1") == 0
        assert "already running" in capsys.readouterr().out
    finally:
        cli.stop_server(first, thread)


def test_main_accepts_the_background_flag(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(cli, "run_background", lambda cfg, host: seen.setdefault("ran", True) and 0)
    photos = tmp_path / "Photos"
    photos.mkdir()
    code = cli.main(["--library", str(photos), "--data", str(tmp_path / "data"), "--background",
                     "--port", str(free_port())])
    assert code == 0 and seen["ran"]


def test_background_never_asks_for_a_folder_at_sign_in(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cli, "pick_folder", lambda: pytest.fail("no folder picker at sign-in"))
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config(library_path=None, data_dir=tmp_path / "data"))
    assert cli.main(["--background"]) == 2
    assert "no photo folder" in capsys.readouterr().err


def test_background_waits_for_a_slow_drive(tmp_path, monkeypatch):
    photos = tmp_path / "Photos"
    naps = []

    def nap(seconds):
        naps.append(seconds)
        photos.mkdir(exist_ok=True)                           # the drive turns up during the wait

    monkeypatch.setattr(cli.time, "sleep", nap)
    monkeypatch.setattr(cli, "run_background", lambda cfg, host: 0)
    assert cli.main(["--library", str(photos), "--data", str(tmp_path / "data"), "--background"]) == 0
    assert naps


def test_a_folder_chosen_on_first_run_is_what_windows_is_told(tmp_path, monkeypatch):
    photos = tmp_path / "Photos"
    photos.mkdir()
    seen = {}
    monkeypatch.setattr(cli, "pick_folder", lambda: photos)
    monkeypatch.setattr(cli, "load_config", lambda *a, **k: Config(library_path=None, data_dir=tmp_path / "data"))
    monkeypatch.setattr(cli, "save_library_path", lambda chosen, cf: tmp_path / "cfg" / "config.toml")
    monkeypatch.setattr(cli, "serve_with_ui", lambda cfg, *a: seen.setdefault("cfg", cfg) and 0)
    monkeypatch.setattr(shell, "window_available", lambda: True)
    assert cli.main(["--window"]) == 0
    assert seen["cfg"].config_file == tmp_path / "cfg" / "config.toml"


def test_first_time_setup_offers_it_only_on_windows_and_only_before_setup(reg, tmp_path, monkeypatch):
    (tmp_path / "Photos").mkdir()
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    assert c.get("/api/auth/state").json()["can_start_with_windows"] is True
    setup_admin(c)
    # what the setup page does when the box is ticked: the new administrator turns it on
    assert c.put("/api/settings/startup", json={"enabled": True}).json()["enabled"] is True
    assert c.get("/api/auth/state").json()["can_start_with_windows"] is False
    monkeypatch.setattr(startup, "supported", lambda: False)
    c2, _ = new_client(tmp_path / "other", tmp_path / "Photos")
    assert c2.get("/api/auth/state").json()["can_start_with_windows"] is False
