"""Fixes from the security audit of 26 November 2026."""

import http.client
import socket
import threading
import time

import pytest
import uvicorn
from fastapi.testclient import TestClient

from snapshots import auth
from snapshots.app import MAX_BODY, create_app
from snapshots.config import Config
from helpers import ADMIN, ids_by_name, login, make_jpeg, new_client, setup_admin, wait_scan


def _library(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "secret.jpg", "2024:01:01 10:00:00")
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00", color=(1, 2, 3))
    return root


def test_an_api_key_cannot_manage_accounts_or_reset_its_owners_password(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        wait_scan(c)
        x = ids_by_name(c)["secret.jpg"]
        c.post("/api/locked/pin", json={"pin": "123456", "password": ADMIN[1]})
        c.post("/api/locked", json={"ids": [x], "value": True})
        me = c.get("/api/auth/state").json()["user"]["id"]
        key = c.post("/api/keys", json={"name": "script"}).json()["key"]
        k = TestClient(c.app)
        k.headers["Authorization"] = "Bearer " + key
        assert k.get("/api/assets").status_code == 200                    # a key still works for the library
        assert k.patch(f"/api/users/{me}", json={"password": "attacker pass 1"}).status_code == 403
        assert k.patch(f"/api/users/{me}", json={"two_step_off": True}).status_code == 403
        assert k.post("/api/users", json={"username": "eve", "password": "eve password 1", "is_admin": True}).status_code == 403
        assert k.get("/api/users").status_code == 403
        assert k.delete(f"/api/users/{me}").status_code == 403
        a = TestClient(c.app)
        a.headers["X-Snapshots-Request"] = "1"
        assert a.post("/api/auth/login", json={"username": "admin", "password": "attacker pass 1"}).status_code == 401


def test_your_own_password_needs_the_current_one_on_the_accounts_page_too(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        me = c.get("/api/auth/state").json()["user"]["id"]
        assert c.patch(f"/api/users/{me}", json={"password": "new password 12"}).status_code == 400
        assert c.patch(f"/api/users/{me}", json={"password": "new password 12", "current": "wrong one"}).status_code == 400
        assert c.patch(f"/api/users/{me}", json={"two_step_off": True}).status_code == 400
        assert c.patch(f"/api/users/{me}", json={"password": "new password 12", "current": ADMIN[1]}).status_code == 200
        login(c, "admin", "new password 12")          # every session ends with a new password
        # someone else's password: an administrator resets it without knowing it, as before
        bob = c.post("/api/users", json={"username": "bob", "password": "bob password 1"}).json()["id"]
        assert c.patch(f"/api/users/{bob}", json={"password": "bob password 2"}).status_code == 200


def test_big_requests_are_refused_unread(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        u = TestClient(c.app)
        u.headers["X-Snapshots-Request"] = "1"
        big = b'{"username":"' + b"a" * (MAX_BODY + 10) + b'","password":"x"}'
        r = u.post("/api/auth/login", content=big, headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        # without a size up front (chunked): counted as it arrives
        def chunks():
            yield b'{"username":"'
            for _ in range(40):
                yield b"a" * 65536
            yield b'","password":"x"}'
        r = u.post("/api/auth/login", content=chunks(), headers={"Content-Type": "application/json"})
        assert r.status_code == 413
        # long names are refused before anything else
        r = u.post("/api/auth/login", json={"username": "n" * 5000, "password": "x"})
        assert r.status_code == 422
        # uploads are not affected by the limit (they have their own)
        big_photo = (tmp_path / "Photos" / "b.jpg").read_bytes() + b"\0" * (MAX_BODY + 10)
        r = c.post("/api/upload", files={"files": ("big.jpg", big_photo, "image/jpeg")})
        assert r.status_code == 200


def test_the_login_throttle_does_not_keep_every_name_for_ever():
    t = auth.LoginThrottle(limit=3, window=60)
    t.MAX_KEYS = 100
    for i in range(1000):
        t.failed(f"name:user{i}@1.2.3.4", "addr:1.2.3.4")
    assert len(t._failures) <= 100
    assert len(t._failures["addr:1.2.3.4"]) == 3              # a blocked key gets no more entries
    assert t.retry_after("addr:1.2.3.4") > 0
    assert t.retry_after("name:never@5.6.7.8") == 0
    assert "name:never@5.6.7.8" not in t._failures            # asking doesn't store anything
    # old failures are forgotten
    t2 = auth.LoginThrottle(limit=3, window=0.05)
    t2.failed("a", "b")
    time.sleep(0.1)
    assert t2.retry_after("a", "b") == 0 and not t2._failures
    assert auth.throttle_keys(False, "x" * 10_000, None, "1.2.3.4")[0] == "name:" + "x" * 100 + "@1.2.3.4"


@pytest.mark.parametrize("host,ok", [
    ("127.0.0.1:8080", True), ("localhost:8080", True), ("[::1]:8080", True), ("100.101.102.103", True),
    ("hans-pc:8080", True), ("hans-pc.tail1234.ts.net", True), ("hans-pc.local", True), ("testserver", True),
    ("192.168.1.20:8080", True), ("", True), ("photos.example.com", False), ("evil.example:8080", False),
    ("127.0.0.1.nip.io", False), ("localhost.evil.com", False),
])
def test_which_names_are_answered(host, ok):
    assert auth.host_allowed(host) is ok


def test_a_foreign_name_is_refused_unless_listed(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        r = c.get("/api/auth/state", headers={"Host": "evil.example"})
        assert r.status_code == 421 and "allowed_hosts" in r.json()["detail"]
        assert c.get("/", headers={"Host": "evil.example"}).status_code == 421
        assert c.get("/api/auth/state", headers={"Host": "127.0.0.1:8080"}).status_code == 200
    c2, cfg2 = new_client(tmp_path / "two", _library(tmp_path / "two"), allowed_hosts=["photos.example.com"])
    with c2:
        assert c2.get("/api/auth/state", headers={"Host": "photos.example.com"}).status_code == 200
        assert c2.get("/api/auth/state", headers={"Host": "other.example.com"}).status_code == 421


def test_allowed_hosts_is_read_from_the_config_file(tmp_path):
    from snapshots.config import load_config
    f = tmp_path / "config.toml"
    f.write_text('library_path = "x"\nallowed_hosts = ["Photos.Example.com"]\n', encoding="utf-8")
    assert load_config(f).allowed_hosts == ["photos.example.com"]
    f.write_text('library_path = "x"\nallowed_hosts = "photos.example.com"\n', encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(f)


def test_a_real_server_refuses_a_huge_chunked_body(tmp_path):
    """The limit also holds through uvicorn (not only the test client)."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    cfg = Config(library_path=_library(tmp_path), data_dir=tmp_path / "data", faces=False, places=False,
                 smart_search=False, text_search=False, scan_interval_minutes=0)
    server = uvicorn.Server(uvicorn.Config(create_app(cfg, start_worker=False), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    try:
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=10)
        conn.putrequest("POST", "/api/auth/login")
        conn.putheader("Content-Type", "application/json")
        conn.putheader("X-Snapshots-Request", "1")
        conn.putheader("Transfer-Encoding", "chunked")
        conn.endheaders()
        status = None
        try:
            for _ in range(64):          # up to 4 MB, 64 KB at a time
                piece = b"a" * 65536
                conn.send(b"%x\r\n%s\r\n" % (len(piece), piece))
            conn.send(b"0\r\n\r\n")
        except OSError:
            pass                          # the server may close the connection early: also fine
        try:
            status = conn.getresponse().status
        except (OSError, http.client.HTTPException):
            status = "closed"
        assert status in (413, "closed")
    finally:
        server.should_exit = True
        thread.join(5)
