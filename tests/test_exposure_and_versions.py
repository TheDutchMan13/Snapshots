"""Snapshots only speaks plain HTTP: it says so when it listens beyond this PC. Folder paths are for admins.
The version number lives in one place."""

import re
from pathlib import Path

from snapshots import __version__
from snapshots.config import network_warning
from helpers import add_member, login, make_jpeg, new_client, setup_admin, wait_scan
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent


def test_the_warning_only_when_listening_beyond_this_pc():
    for host in ("127.0.0.1", "localhost", "::1"):
        assert network_warning(host) is None
    for host in ("0.0.0.0", "::", "192.168.1.20"):
        w = network_warning(host)
        assert w and "plain HTTP" in w and "Tailscale Serve" in w
    assert "every network" in network_warning("0.0.0.0") and "192.168.1.20" in network_warning("192.168.1.20")


def test_folder_paths_and_the_warning_are_for_admins(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00")
    c, cfg = new_client(tmp_path, root, host="0.0.0.0")
    with c:
        setup_admin(c)
        wait_scan(c)
        st = c.get("/api/status").json()
        assert st["library_path"] == str(root) and st["libraries"][0]["path"] == str(root)
        assert "plain HTTP" in st["network_warning"]
        add_member(c)
        m = TestClient(c.app)
        m.headers["X-Snapshots-Request"] = "1"
        login(m, "member", "member password 1")
        st = m.get("/api/status").json()
        assert st["library_path"] is None and st["network_warning"] is None
        assert st["libraries"] == [{"main": True, "ok": True}] and st["library_ok"] is True
        assert str(tmp_path) not in m.get("/api/status").text and st["photos"] == 1


def test_the_version_lives_in_one_place():
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    for name in ("README.md", "installer/snapshots.iss"):
        text = (ROOT / name).read_text(encoding="utf-8")
        assert not re.search(r"/DAppVersion=\d", text), f"{name} spells out a version number"
        assert not re.search(r"early development \(v\d", text)
    assert "not built yet" not in (ROOT / "README.md").read_text(encoding="utf-8")
