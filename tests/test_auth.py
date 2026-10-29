import sqlite3
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from snapshots import __main__ as cli
from snapshots import auth
from snapshots.config import Config
from helpers import ADMIN, login, make_jpeg, new_client, setup_admin, wait_scan


@pytest.fixture
def lib(tmp_path):
    make_jpeg(tmp_path / "Photos" / "a.jpg", "2024:01:01 10:00:00")
    return tmp_path / "Photos"


@pytest.fixture
def app(tmp_path, lib):
    c, cfg = new_client(tmp_path, lib)
    with c:
        yield c, cfg


def fresh(c):
    """Another browser on the same server (no cookies)."""
    other = TestClient(c.app)
    other.headers["X-Snapshots-Request"] = "1"
    return other


def test_first_run_needs_setup_and_only_once(app):
    c, cfg = app
    state = c.get("/api/auth/state").json()
    assert state["needs_setup"] is True and state["user"] is None
    assert c.get("/api/status").status_code == 401  # nothing is visible before an account exists

    me = setup_admin(c)["user"]
    assert me["is_admin"] is True
    assert c.get("/api/auth/state").json() == {"needs_setup": False, "user": me, "version": state["version"]}

    again = fresh(c).post("/api/auth/setup", json={"username": "intruder", "password": "x" * 12})
    assert again.status_code == 409


def test_everything_private_needs_login(app):
    c, cfg = app
    setup_admin(c)
    wait_scan(c)
    asset_id = c.get("/api/assets").json()["items"][0]["id"]
    anon = fresh(c)
    for url in ("/api/status", "/api/assets", f"/api/assets/{asset_id}/thumb", f"/api/assets/{asset_id}/original",
                "/api/albums", "/api/people", "/api/places", "/api/users", "/api/timeline"):
        assert anon.get(url).status_code == 401, url
    assert anon.get("/api/health").status_code == 200
    assert anon.get("/").status_code == 200 and anon.get("/login").status_code == 200


def test_login_logout_and_wrong_password(app):
    c, cfg = app
    setup_admin(c)
    b = fresh(c)
    assert b.post("/api/auth/login", json={"username": "admin", "password": "nope nope"}).status_code == 401
    assert b.post("/api/auth/login", json={"username": "nobody", "password": "nope nope"}).status_code == 401
    login(b, "ADMIN", ADMIN[1])  # user names are not case sensitive
    assert b.get("/api/status").status_code == 200
    assert b.post("/api/auth/logout").status_code == 200
    assert b.get("/api/status").status_code == 401


def test_cookie_is_httponly_and_samesite(app):
    c, cfg = app
    r = c.post("/api/auth/setup", json={"username": "admin", "password": ADMIN[1]})
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "max-age" in cookie


def test_changes_need_the_csrf_header(app):
    c, cfg = app
    setup_admin(c)
    no_header = TestClient(c.app, cookies=c.cookies)
    assert no_header.post("/api/albums", json={"name": "x"}).status_code == 403
    assert no_header.post("/api/auth/login", json={"username": "a", "password": "b"}).status_code == 403
    assert no_header.get("/api/albums").status_code == 200  # reading is fine
    assert c.post("/api/albums", json={"name": "x"}).status_code == 201


def test_bearer_token_for_apps(app):
    c, cfg = app
    token = setup_admin(c)["token"]
    phone = TestClient(c.app)  # no cookies, no CSRF header: an app, not a browser
    assert phone.get("/api/status").status_code == 401
    auth_header = {"Authorization": f"Bearer {token}"}
    assert phone.get("/api/status", headers=auth_header).status_code == 200
    assert phone.post("/api/albums", json={"name": "From phone"}, headers=auth_header).status_code == 201
    assert phone.get("/api/status", headers={"Authorization": "Bearer wrong"}).status_code == 401


def test_password_guessing_is_slowed_down(app):
    c, cfg = app
    setup_admin(c)
    b = fresh(c)
    for _ in range(10):
        assert b.post("/api/auth/login", json={"username": "admin", "password": "guess guess"}).status_code == 401
    r = b.post("/api/auth/login", json={"username": "admin", "password": ADMIN[1]})
    assert r.status_code == 429 and int(r.headers["retry-after"]) > 0


def test_passwords_are_hashed_and_validated(app):
    c, cfg = app
    assert c.post("/api/auth/setup", json={"username": "admin", "password": "short"}).status_code == 400
    setup_admin(c)
    conn = sqlite3.connect(cfg.db_path)
    stored = conn.execute("SELECT pw_hash FROM users").fetchone()[0]
    tokens = [r[0] for r in conn.execute("SELECT token_hash FROM sessions")]
    conn.close()
    assert ADMIN[1] not in stored and stored.startswith("scrypt$")
    assert c.cookies.get(auth.COOKIE) not in tokens  # only a hash of the token is stored


def test_change_password_logs_out_other_devices(app):
    c, cfg = app
    setup_admin(c)
    laptop = fresh(c)
    login(laptop, *ADMIN)
    assert c.post("/api/auth/password", json={"current": "wrong", "new": "brand new pass"}).status_code == 400
    assert c.post("/api/auth/password", json={"current": ADMIN[1], "new": "brand new pass"}).status_code == 200
    assert c.get("/api/status").status_code == 200        # this device stays logged in
    assert laptop.get("/api/status").status_code == 401   # the other one is logged out
    login(fresh(c), "admin", "brand new pass")


def test_expired_sessions_are_refused(app):
    c, cfg = app
    setup_admin(c)
    conn = sqlite3.connect(cfg.db_path)
    conn.execute("UPDATE sessions SET expires_at = ?", ((datetime.now() - timedelta(minutes=1)).isoformat(),))
    conn.commit()
    conn.close()
    assert c.get("/api/status").status_code == 401


def test_admin_manages_accounts(app):
    c, cfg = app
    me = setup_admin(c)["user"]
    r = c.post("/api/users", json={"username": "mum", "password": "mum's password"})
    assert r.status_code == 201 and r.json()["is_admin"] is False
    mum_id = r.json()["id"]
    assert c.post("/api/users", json={"username": "MUM", "password": "x" * 10}).status_code == 409

    mum = fresh(c)
    login(mum, "mum", "mum's password")
    assert mum.get("/api/status").json()["user"]["username"] == "mum"
    assert mum.get("/api/users").status_code == 403
    assert mum.post("/api/users", json={"username": "x", "password": "y" * 10}).status_code == 403
    assert mum.post("/api/trash/empty", json={}).status_code == 403

    # an administrator can reset a forgotten password; that logs the account out everywhere
    assert c.patch(f"/api/users/{mum_id}", json={"password": "a new one!"}).status_code == 200
    assert mum.get("/api/status").status_code == 401
    login(mum, "mum", "a new one!")

    assert c.delete(f"/api/users/{me['id']}").status_code == 400                     # not yourself
    assert c.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 400  # last admin
    assert c.patch(f"/api/users/{mum_id}", json={"is_admin": True}).json()["is_admin"] is True
    assert c.patch(f"/api/users/{me['id']}", json={"is_admin": False}).status_code == 200  # now allowed
    assert c.delete(f"/api/users/{mum_id}").status_code == 403  # no longer an admin myself
    assert mum.delete(f"/api/users/{me['id']}").status_code == 200
    assert c.get("/api/status").status_code == 401  # deleted accounts are logged out


def test_reset_password_from_the_command_line(tmp_path):
    cfg = Config(data_dir=tmp_path / "data")
    answers = iter(["new secret 123", "new secret 123"])
    assert cli.reset_password(cfg, "admin", lambda prompt: next(answers)) == 0  # creates an admin
    answers = iter(["another one 456", "different"])
    assert cli.reset_password(cfg, "admin", lambda prompt: next(answers)) == 2  # mismatch: nothing changed
    answers = iter(["another one 456", "another one 456"])
    assert cli.reset_password(cfg, "admin", lambda prompt: next(answers)) == 0

    from snapshots import db
    with db.session(cfg.db_path) as conn:
        assert auth.check_login(conn, "admin", "another one 456") is not None
        assert conn.execute("SELECT is_admin FROM users").fetchone()[0] == 1
