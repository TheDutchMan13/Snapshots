from pathlib import Path

import pytest

from snapshots import __main__ as cli
from snapshots.config import load_config, save_library_path, user_config_path

ROOT = Path(__file__).resolve().parent.parent


def test_example_config_is_valid_toml():
    cfg = load_config(ROOT / "config.example.toml")
    assert cfg.library_path == Path(r"D:\Photos")
    assert (cfg.host, cfg.port, cfg.index_on_start) == ("127.0.0.1", 8080, True)


def test_command_line_beats_file(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('library_path = "/from/file"\nport = 9000\n')
    cfg = load_config(f, library_path="/from/cli", port=None)
    assert cfg.library_path == Path("/from/cli")
    assert cfg.port == 9000  # None means "not given", so the file's value stays


def test_missing_explicit_config_is_an_error(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_config(tmp_path / "nope.toml")


def test_data_dir_defaults_outside_library(tmp_path):
    library = tmp_path / "Photos"
    cfg = load_config(None, library_path=library)
    assert not cfg.data_dir.is_relative_to(library)


# ------------------------------------------------------------------ remembering the folder

@pytest.mark.parametrize("folder", [
    r"D:\Photos",
    r"C:\Users\Hans\Dad's Photos",          # an apostrophe is legal in Windows folder names
    r'D:\Family "Best of"',
    "D:\\Zoë & Renée — vacances",           # accents and symbols
    "D:\\Фото",
    "/home/me/pictures",
])
def test_saved_folder_round_trips(folder):
    path = save_library_path(Path(folder))
    assert path == user_config_path()
    assert load_config(None).library_path == Path(folder)


def test_saving_keeps_other_settings_and_replaces_the_old_folder(tmp_path):
    f = tmp_path / "c.toml"
    f.write_text('# my notes\nlibrary_path = "/old"\nport = 9001\n', encoding="utf-8")
    save_library_path(Path("/new"), f)
    save_library_path(Path("/newer"), f)
    text = f.read_text(encoding="utf-8")
    assert text.count("library_path") == 1 and "# my notes" in text
    cfg = load_config(f)
    assert cfg.library_path == Path("/newer") and cfg.port == 9001


def test_user_config_is_used_when_there_is_no_local_config(tmp_path):
    save_library_path(tmp_path / "Photos")
    cfg = load_config(None)
    assert cfg.config_file == user_config_path()


def test_local_config_wins_over_user_config(tmp_path):
    save_library_path(tmp_path / "from_user")
    Path("config.toml").write_text(f'library_path = "{(tmp_path / "from_local").as_posix()}"\n')
    assert load_config(None).library_path == tmp_path / "from_local"


# ------------------------------------------------------------------ command line

@pytest.fixture
def fake_server(monkeypatch):
    calls = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda app, **kw: calls.append(kw))
    return calls


def test_cli_refuses_to_start_without_a_library(capsys, tmp_path, monkeypatch, fake_server):
    monkeypatch.setattr(cli, "pick_folder", lambda: None)  # the user cancels the dialog
    assert cli.main([]) == 2
    assert "No photo folder chosen" in capsys.readouterr().err
    assert cli.main(["--library", str(tmp_path / "missing")]) == 2
    assert "not found" in capsys.readouterr().err
    assert fake_server == []


def test_first_run_asks_for_a_folder_and_remembers_it(tmp_path, monkeypatch, fake_server):
    photos = tmp_path / "Photos"
    photos.mkdir()
    asked = []
    monkeypatch.setattr(cli, "pick_folder", lambda: asked.append(1) or photos)

    assert cli.main(["--data", str(tmp_path / "data")]) == 0
    assert len(asked) == 1 and len(fake_server) == 1
    assert load_config(None).library_path == photos

    # second launch: no question, same folder
    assert cli.main(["--data", str(tmp_path / "data")]) == 0
    assert len(asked) == 1 and len(fake_server) == 2


def test_change_library_asks_again(tmp_path, monkeypatch, fake_server):
    a, b = tmp_path / "A", tmp_path / "B"
    a.mkdir(), b.mkdir()
    save_library_path(a)
    monkeypatch.setattr(cli, "pick_folder", lambda: b)
    assert cli.main(["--change-library", "--data", str(tmp_path / "data")]) == 0
    assert load_config(None).library_path == b


def test_library_flag_skips_the_dialog(tmp_path, monkeypatch, fake_server):
    monkeypatch.setattr(cli, "pick_folder", lambda: pytest.fail("should not ask"))
    assert cli.main(["--library", str(tmp_path), "--data", str(tmp_path / "d"), "--port", "9123"]) == 0
    assert fake_server[0]["port"] == 9123 and fake_server[0]["host"] == "127.0.0.1"


def test_browser_opens_only_when_asked(tmp_path, monkeypatch, fake_server):
    opened = []
    monkeypatch.setattr(cli, "open_browser_when_ready", lambda *a: opened.append(a))
    base = ["--library", str(tmp_path), "--data", str(tmp_path / "d")]
    cli.main(base)
    assert opened == []
    cli.main(base + ["--open"])
    cli.main(base + ["--open", "--no-browser"])
    import time
    time.sleep(0.2)  # the helper runs on a thread
    assert len(opened) == 1


# ------------------------------------------------------------------ running as the exe

def test_exe_finds_its_bundled_web_files(tmp_path, monkeypatch):
    """Inside the PyInstaller exe the web files sit under sys._MEIPASS/snapshots/static."""
    import importlib
    import sys

    from snapshots import app as app_module

    bundle = tmp_path / "bundle"
    (bundle / "snapshots").mkdir(parents=True)
    import shutil
    shutil.copytree(app_module.STATIC_DIR, bundle / "snapshots" / "static")

    monkeypatch.setattr(sys, "_MEIPASS", str(bundle), raising=False)
    try:
        reloaded = importlib.reload(app_module)
        assert reloaded.STATIC_DIR == bundle / "snapshots" / "static"
        assert (reloaded.STATIC_DIR / "index.html").is_file()
    finally:
        monkeypatch.undo()
        importlib.reload(app_module)  # back to normal for the other tests


def test_exe_window_waits_before_closing(monkeypatch):
    asked = []
    monkeypatch.setattr("builtins.input", lambda prompt="": asked.append(prompt) or "")
    monkeypatch.setattr(cli, "FROZEN", False)
    cli.pause_if_frozen()
    assert asked == []  # plain command line: never waits
    monkeypatch.setattr(cli, "FROZEN", True)
    cli.pause_if_frozen()
    assert len(asked) == 1  # double-clicked exe: waits so the message can be read


def test_exe_window_does_not_hang_without_a_keyboard(monkeypatch):
    def no_stdin(prompt=""):
        raise EOFError
    monkeypatch.setattr("builtins.input", no_stdin)
    monkeypatch.setattr(cli, "FROZEN", True)
    cli.pause_if_frozen()  # must return, not raise (e.g. in CI)
