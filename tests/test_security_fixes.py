"""Fixes from the November 2026 security review."""

import time

from fastapi.testclient import TestClient

from helpers import ADMIN, ids_by_name, login, make_jpeg, new_client, setup_admin, wait_scan

from snapshots import app as app_module
from snapshots import auth, db, immich


def _library(tmp_path):
    root = tmp_path / "Photos"
    make_jpeg(root / "a.jpg", "2024:01:01 10:00:00", color=(200, 10, 10))
    make_jpeg(root / "b.jpg", "2024:01:02 10:00:00", color=(10, 200, 10))
    return root


def _member(c, name="bob", password="bob's password 1"):
    assert c.post("/api/users", json={"username": name, "password": password}).status_code == 201
    other = TestClient(c.app)
    other.headers["X-Snapshots-Request"] = "1"
    login(other, name, password)
    return other


def _lock(client, ids):
    assert client.post("/api/locked/pin", json={"pin": "246813", "password": "bob's password 1"}).status_code == 200
    assert client.post("/api/locked", json={"ids": ids, "value": True}).status_code == 200


def _bob_id(c):
    return next(u["id"] for u in c.get("/api/users").json()["users"] if u["username"] == "bob")


def test_deleting_a_user_keeps_their_locked_items_hidden(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        bob = _member(c)
        _lock(bob, [ids["b.jpg"]])
        carol = _member(c, "carol", "carol's password 1")
        assert set(ids_by_name(carol)) == {"a.jpg"}
        r = c.delete(f"/api/users/{_bob_id(c)}")
        assert r.status_code == 200 and r.json()["locked_moved"] == 1
        # Still hidden from everyone else: now in the administrator's locked folder.
        assert set(ids_by_name(carol)) == {"a.jpg"}
        assert carol.get(f"/api/assets/{ids['b.jpg']}/original").status_code == 404
        with db.session(cfg.db_path) as conn:
            admin_id = conn.execute("SELECT id FROM users WHERE username = ?", (ADMIN[0],)).fetchone()[0]
            assert conn.execute("SELECT locked_by FROM assets WHERE id = ?", (ids["b.jpg"],)).fetchone()[0] == admin_id


def test_an_administrator_can_take_back_what_someone_locked(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        wait_scan(c)
        ids = ids_by_name(c)
        bob = _member(c)
        _lock(bob, [ids["a.jpg"], ids["b.jpg"]])
        assert ids_by_name(c) == {}
        bob_row = next(u for u in c.get("/api/users").json()["users"] if u["username"] == "bob")
        assert bob_row["locked"] == 2
        # Members can't.
        assert bob.patch(f"/api/users/{bob_row['id']}", json={"take_locked": True}).status_code == 403
        assert c.patch(f"/api/users/{bob_row['id']}", json={"take_locked": True}).status_code == 200
        assert ids_by_name(bob) == {}         # out of Bob's locked folder, and not shown to him
        with db.session(cfg.db_path) as conn:
            assert conn.execute("SELECT COUNT(*) FROM assets WHERE locked_by IS NOT NULL").fetchone()[0] == 2
            assert conn.execute("SELECT COUNT(*) FROM assets WHERE locked_by = ?", (bob_row["id"],)).fetchone()[0] == 0


def test_immich_dates_never_lead_outside_the_library():
    assert immich._year_month("2024-03-05T10:00:00.000Z") == "2024/03"
    for bad in ("/tmp/evil", "C:\\Users\\x", "../.x..", "2024-13-01", "20-1-01", "", None, "..//..//"):
        assert immich._year_month(bad) == "Unknown"


def test_a_new_password_ends_api_keys(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        bob = _member(c)
        key = bob.post("/api/keys", json={"name": "script"}).json()["key"]
        script = TestClient(c.app)
        script.headers["Authorization"] = f"Bearer {key}"
        assert script.get("/api/assets").status_code == 200
        # An administrator resets Bob's password: the key stops working.
        assert c.patch(f"/api/users/{_bob_id(c)}", json={"password": "a brand new one 1"}).status_code == 200
        assert script.get("/api/assets").status_code == 401
        # Bob changes his own password: his keys go too.
        login(bob, "bob", "a brand new one 1")
        key = bob.post("/api/keys", json={"name": "script"}).json()["key"]
        script.headers["Authorization"] = f"Bearer {key}"
        assert script.get("/api/assets").status_code == 200
        assert bob.post("/api/auth/password", json={"current": "a brand new one 1", "new": "another new one 2"}
                        ).status_code == 200
        assert script.get("/api/assets").status_code == 401


def test_browsers_get_a_strict_cookie_and_no_token(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        app_like = TestClient(c.app)
        app_like.headers["X-Snapshots-Request"] = "1"
        r = app_like.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
        assert r.json()["token"]                              # the phone app (no Origin) gets it
        browser = TestClient(c.app)
        browser.headers["X-Snapshots-Request"] = "1"
        r = browser.post("/api/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]},
                         headers={"Origin": "http://127.0.0.1:2342"})
        assert r.status_code == 200 and "token" not in r.json()
        cookie = next(h for h in r.headers.get_list("set-cookie") if h.startswith(auth.COOKIE + "="))
        assert "samesite=strict" in cookie.lower()
        assert browser.get("/api/assets").status_code == 200


def test_uploads_are_refused_before_they_fill_the_drive(tmp_path, monkeypatch):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    assert app_module.upload_problem(cfg, None)[0] == 411
    assert app_module.upload_problem(cfg, str(int(cfg.max_upload_gb * 1024 ** 3) + 1))[0] == 413
    assert app_module.upload_problem(cfg, "1000") is None

    class Usage:
        free = 500 * 1024 ** 2

    monkeypatch.setattr("shutil.disk_usage", lambda path: Usage)
    code, message = app_module.upload_problem(cfg, "1000")
    assert code == 507 and message.startswith("Upload failed")
    with c:
        setup_admin(c)
        r = c.post("/api/upload", files={"files": ("x.jpg", b"123", "image/jpeg")})
        assert r.status_code == 507


def test_a_stranger_cant_lock_you_out_of_a_new_device():
    throttle = auth.LoginThrottle(limits={"user:": auth.USER_LIMIT})
    stranger = auth.throttle_keys(False, "Anna", None, "203.0.113.9")
    for _ in range(throttle.limit):
        throttle.failed(*stranger)
    assert throttle.retry_after(*stranger) > 0
    # Anna's new phone, from home, can still try.
    assert throttle.retry_after(*auth.throttle_keys(False, "anna", None, "192.168.1.20")) == 0
    # But guessing spread over many addresses still runs into the per-name limit.
    for n in range(auth.USER_LIMIT):
        throttle.failed(*auth.throttle_keys(False, "anna", None, f"198.51.100.{n % 250}"))
    assert throttle.retry_after(*auth.throttle_keys(False, "anna", None, "192.168.1.20")) > 0


def test_pins_need_six_characters():
    assert auth.validate_pin("1234") and auth.validate_pin("12345")
    assert auth.validate_pin("123456") is None


def test_two_step_secrets_are_sealed_in_the_database(tmp_path):
    c, cfg = new_client(tmp_path, _library(tmp_path))
    with c:
        setup_admin(c)
        secret = c.post("/api/auth/two-step/start").json()["secret"]
        code = auth.totp_at(secret, int(time.time()) // 30)
        assert c.post("/api/auth/two-step/confirm", json={"code": code}).status_code == 200
        with db.session(cfg.db_path) as conn:
            stored = conn.execute("SELECT totp_secret FROM users").fetchone()[0]
            assert stored.startswith("v1:") and secret not in stored
            assert auth.unseal(conn, stored) == secret
        assert (cfg.db_path.parent / auth.SECRET_FILE).is_file()
        # A secret stored before sealing still works, and is sealed when it is next used.
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE users SET totp_secret = ?, totp_last = NULL", (secret,))
            assert auth.check_second_step(conn, 1, auth.totp_at(secret, int(time.time()) // 30)) == "totp"
            assert conn.execute("SELECT totp_secret FROM users").fetchone()[0].startswith("v1:")
            # Tampered with: refused, not crashed.
            conn.execute("UPDATE users SET totp_secret = 'v1:AAAA', totp_last = NULL")
            assert auth.check_second_step(conn, 1, "123456") is None
