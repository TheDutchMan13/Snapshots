import pytest


@pytest.fixture(autouse=True)
def isolated_environment(tmp_path, monkeypatch):
    """Tests must never read or write the developer's real settings or data folder."""
    home = tmp_path / "home"
    local = home / "AppData" / "Local"
    local.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    cwd = tmp_path / "cwd"  # so a config.toml in the project folder is not picked up
    cwd.mkdir()
    monkeypatch.chdir(cwd)
