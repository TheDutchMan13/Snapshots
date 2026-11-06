"""The "a new version is out" note."""

import time

from helpers import new_client, setup_admin, wait_for

from snapshots import __version__, updates


def test_versions_compare_as_numbers():
    assert updates.parse_version("v0.14.4") == (0, 14, 4)
    assert updates.parse_version("0.14.4-beta") is None and updates.parse_version("latest") is None
    assert updates.is_newer("v0.14.10", "0.14.9") and updates.is_newer("0.15.0", "0.14.99")
    assert not updates.is_newer("0.14.4", "0.14.4") and not updates.is_newer("0.13.9", "0.14.0")
    assert not updates.is_newer("garbage") and not updates.is_newer(None)


def test_a_newer_release_is_offered_to_administrators_only(tmp_path, monkeypatch):
    monkeypatch.setattr(updates, "fetch_latest", lambda timeout=10: {"version": "99.0.0", "url": updates.PAGE_URL})
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    with c:
        setup_admin(c)
        c.get("/api/update")                                 # answers at once; GitHub is asked in the background
        wait_for(lambda: c.get("/api/update").json()["newer"])
        status = c.get("/api/update").json()
        assert status["latest"] == "99.0.0" and status["current"] == __version__
        assert c.get("/api/settings").json()["updates"]["newer"] is True


def test_nothing_is_asked_when_switched_off_and_failures_are_quiet(tmp_path, monkeypatch):
    asked = []

    def fetch(timeout=10):
        asked.append(1)
        raise OSError("no internet")

    monkeypatch.setattr(updates, "fetch_latest", fetch)
    c, cfg = new_client(tmp_path, tmp_path / "Photos")
    with c:
        setup_admin(c)
        assert c.put("/api/settings/updates", json={"enabled": False}).json()["enabled"] is False
        c.get("/api/update")
        time.sleep(0.3)
        assert asked == []
        assert c.put("/api/settings/updates", json={"enabled": True}).status_code == 200
        wait_for(lambda: len(asked) == 1)
        c.get("/api/update")
        time.sleep(0.3)
        assert len(asked) == 1                                # a failed try is not repeated straight away
        assert c.get("/api/update").json()["newer"] is False
