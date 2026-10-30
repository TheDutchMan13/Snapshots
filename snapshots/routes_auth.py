"""Logging in and out, and managing accounts."""

import time
from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import __version__, auth, db
from .app import current_user, require_admin, require_session


class Credentials(BaseModel):
    username: str
    password: str


class CodeBody(BaseModel):
    ticket: str
    code: str


class TwoStepConfirm(BaseModel):
    code: str


class TwoStepOff(BaseModel):
    password: str


class PasswordChange(BaseModel):
    current: str
    new: str


class NewUser(BaseModel):
    username: str
    password: str
    is_admin: bool = False


class UserUpdate(BaseModel):
    password: str | None = None
    is_admin: bool | None = None
    two_step_off: bool = False


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _start_session(request: Request, response: Response, conn, user_row) -> dict:
    token = auth.create_session(conn, user_row["id"], request.headers.get("user-agent"))
    secure = _is_https(request)
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=secure, path="/",
    )
    # This browser is now a known device for this user: it gets its own allowance of wrong passwords.
    device = auth.remember_device(conn, user_row["id"], request.cookies.get(auth.DEVICE_COOKIE),
                                  request.headers.get("user-agent"))
    response.set_cookie(auth.DEVICE_COOKIE, device, max_age=auth.DEVICE_DAYS * 86400, httponly=True,
                        samesite="lax", secure=secure, path="/")
    user = {"id": user_row["id"], "username": user_row["username"], "is_admin": bool(user_row["is_admin"])}
    # The token is also returned so that apps that are not browsers can use it.
    return {"user": user, "token": token}


def _user_json(row) -> dict:
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"], "two_step": bool(row["totp_secret"])}


def router(ctx) -> APIRouter:
    cfg = ctx.cfg
    r = APIRouter()

    @r.get("/api/health")
    def health():
        return {"ok": True, "version": __version__}

    @r.get("/api/auth/state")
    def state(request: Request):
        with db.session(cfg.db_path) as conn:
            needs_setup = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0
        user = request.state.user
        if user is not None:
            user = {"id": user["id"], "username": user["username"], "is_admin": user["is_admin"]}
        return {"needs_setup": needs_setup, "user": user, "version": __version__}

    @r.post("/api/auth/setup")
    def setup(body: Credentials, request: Request, response: Response):
        problem = auth.validate_username(body.username) or auth.validate_new_password(body.password)
        if problem:
            raise HTTPException(400, problem)
        with db.session(cfg.db_path) as conn:
            # Only ever works while there are no accounts at all.
            cur = conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin, created_at)"
                " SELECT ?, ?, 1, ? WHERE NOT EXISTS (SELECT 1 FROM users)",
                (body.username.strip(), auth.hash_password(body.password),
                 datetime.now().isoformat(timespec="seconds")),
            )
            if cur.rowcount == 0:
                raise HTTPException(409, "Snapshots is already set up. Please log in.")
            row = conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone()
            return _start_session(request, response, conn, row)

    def _too_many(wait: int):
        raise HTTPException(429, f"Too many wrong tries. Try again in {max(1, (wait + 59) // 60)} minute(s).",
                            headers={"Retry-After": str(wait)})

    @r.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        device = request.cookies.get(auth.DEVICE_COOKIE)
        address = request.client.host if request.client else "-"
        with db.session(cfg.db_path) as conn:
            known = auth.is_known_device(conn, device, body.username)
        keys = auth.throttle_keys(known, body.username, device, address)
        wait = ctx.throttle.retry_after(*keys)
        if wait:
            _too_many(wait)
        with db.session(cfg.db_path) as conn:
            row = auth.check_login(conn, body.username, body.password)
            if row is None:
                ctx.throttle.failed(*keys)
                raise HTTPException(401, "Wrong user name or password.")
            if row["totp_secret"]:
                # Right password, but a code is still needed. Failures stay counted until the code is right.
                return {"two_step": True, "ticket": ctx.tickets.create(row["id"], keys)}
            ctx.throttle.succeeded(*keys)
            return _start_session(request, response, conn, row)

    @r.post("/api/auth/login/code")
    def login_code(body: CodeBody, request: Request, response: Response):
        """The second step: a code from the authenticator app, or a recovery code."""
        item = ctx.tickets.get(body.ticket)
        if item is None:
            raise HTTPException(401, "That took too long or had too many wrong codes. Please log in again.")
        wait = ctx.throttle.retry_after(*item["keys"])
        if wait:
            _too_many(wait)
        with db.session(cfg.db_path) as conn:
            how = auth.check_second_step(conn, item["user_id"], body.code)
            if how is None:
                conn.commit()
                ctx.tickets.failed(body.ticket)
                ctx.throttle.failed(*item["keys"])
                raise HTTPException(401, "That code isn't right. Codes change every 30 seconds.")
            ctx.tickets.done(body.ticket)
            ctx.throttle.succeeded(*item["keys"])
            row = conn.execute("SELECT * FROM users WHERE id = ?", (item["user_id"],)).fetchone()
            out = _start_session(request, response, conn, row)
            if how == "recovery":
                out["recovery_left"] = conn.execute(
                    "SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
                    (row["id"],)).fetchone()[0]
            return out

    # ------------------------------------------------------------ two-step login settings

    @r.get("/api/auth/two-step")
    def two_step_state(request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT totp_secret FROM users WHERE id = ?", (user["id"],)).fetchone()
            left = conn.execute("SELECT COUNT(*) FROM recovery_codes WHERE user_id = ? AND used_at IS NULL",
                                (user["id"],)).fetchone()[0]
        return {"enabled": bool(row["totp_secret"]), "recovery_left": left if row["totp_secret"] else 0}

    @r.post("/api/auth/two-step/start")
    def two_step_start(request: Request):
        """A new secret to scan. Nothing changes until a code from it is confirmed."""
        user = require_session(request)
        secret = auth.new_totp_secret()
        for t in [t for t, v in ctx.pending_totp.items() if v[2] < time.time()]:
            ctx.pending_totp.pop(t, None)
        ctx.pending_totp[request.state.token] = (user["id"], secret, time.time() + 15 * 60)
        return {"secret": secret, "uri": auth.otpauth_uri(secret, user["username"])}

    @r.post("/api/auth/two-step/confirm")
    def two_step_confirm(body: TwoStepConfirm, request: Request):
        user = require_session(request)
        pending = ctx.pending_totp.get(request.state.token)
        if not pending or pending[0] != user["id"] or pending[2] < time.time():
            raise HTTPException(400, "Start again: the code to scan has expired.")
        step = auth.check_totp(pending[1], body.code, None)
        if step is None:
            raise HTTPException(400, "That code isn't right. Check the phone's clock, and type the code the app shows now.")
        del ctx.pending_totp[request.state.token]
        with db.session(cfg.db_path) as conn:
            conn.execute("UPDATE users SET totp_secret = ?, totp_last = ? WHERE id = ?", (pending[1], step, user["id"]))
            codes = auth.new_recovery_codes(conn, user["id"])
            auth.end_all_sessions(conn, user["id"], keep_token=request.state.token)  # other devices log in again
        return {"enabled": True, "recovery_codes": codes}

    @r.post("/api/auth/two-step/recovery")
    def two_step_new_codes(body: TwoStepOff, request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT pw_hash, totp_secret FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not row["totp_secret"]:
                raise HTTPException(400, "Two-step login is off.")
            if not auth.verify_password(body.password, row["pw_hash"]):
                raise HTTPException(400, "Your password is not right.")
            return {"recovery_codes": auth.new_recovery_codes(conn, user["id"])}

    @r.post("/api/auth/two-step/off")
    def two_step_off(body: TwoStepOff, request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not auth.verify_password(body.password, row["pw_hash"]):
                raise HTTPException(400, "Your password is not right.")
            conn.execute("UPDATE users SET totp_secret = NULL, totp_last = NULL WHERE id = ?", (user["id"],))
            conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user["id"],))
        return {"enabled": False}

    # ------------------------------------------------------------ devices you log in from

    @r.get("/api/auth/devices")
    def devices(request: Request):
        user = require_session(request)
        mine = request.cookies.get(auth.DEVICE_COOKIE)
        mine_hash = auth._token_hash(mine) if mine else None
        with db.session(cfg.db_path) as conn:
            rows = conn.execute("SELECT rowid, token_hash, created_at, last_used_at, user_agent FROM known_devices"
                                " WHERE user_id = ? ORDER BY last_used_at DESC", (user["id"],)).fetchall()
        return {"devices": [{"id": r["rowid"], "created_at": r["created_at"], "last_used_at": r["last_used_at"],
                             "user_agent": r["user_agent"], "this": r["token_hash"] == mine_hash} for r in rows]}

    @r.delete("/api/auth/devices/{device_id}")
    def forget_device(device_id: int, request: Request):
        user = require_session(request)
        with db.session(cfg.db_path) as conn:
            n = conn.execute("DELETE FROM known_devices WHERE rowid = ? AND user_id = ?", (device_id, user["id"])).rowcount
        if not n:
            raise HTTPException(404, "No such device.")
        return {"ok": True}

    @r.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with db.session(cfg.db_path) as conn:
            auth.end_session(conn, request.state.token)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @r.post("/api/auth/password")
    def change_password(body: PasswordChange, request: Request):
        user = require_session(request)
        problem = auth.validate_new_password(body.new)
        if problem:
            raise HTTPException(400, problem)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT pw_hash FROM users WHERE id = ?", (user["id"],)).fetchone()
            if not auth.verify_password(body.current, row["pw_hash"]):
                raise HTTPException(400, "Your current password is not right.")
            conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (auth.hash_password(body.new), user["id"]))
            auth.end_all_sessions(conn, user["id"], keep_token=request.state.token)  # log out other devices
        return {"ok": True}

    # ------------------------------------------------------------ accounts (administrators)

    @r.get("/api/users")
    def list_users(request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            locked = dict(conn.execute("SELECT locked_by, COUNT(*) FROM assets WHERE locked_by IS NOT NULL"
                                       " GROUP BY locked_by").fetchall())
            return {"users": [dict(_user_json(u), locked=locked.get(u["id"], 0))
                              for u in conn.execute("SELECT * FROM users ORDER BY username")]}

    @r.post("/api/users", status_code=201)
    def create_user(body: NewUser, request: Request):
        require_admin(request)
        problem = auth.validate_username(body.username) or auth.validate_new_password(body.password)
        if problem:
            raise HTTPException(400, problem)
        with db.session(cfg.db_path) as conn:
            if conn.execute("SELECT 1 FROM users WHERE username = ?", (body.username.strip(),)).fetchone():
                raise HTTPException(409, "There is already an account with that name.")
            cur = conn.execute(
                "INSERT INTO users (username, pw_hash, is_admin, created_at) VALUES (?, ?, ?, ?)",
                (body.username.strip(), auth.hash_password(body.password), int(body.is_admin),
                 datetime.now().isoformat(timespec="seconds")),
            )
            return _user_json(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())

    def _admins(conn) -> int:
        return conn.execute("SELECT COUNT(*) FROM users WHERE is_admin = 1").fetchone()[0]

    @r.patch("/api/users/{user_id}")
    def update_user(user_id: int, body: UserUpdate, request: Request):
        require_admin(request)
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "No such account.")
            if body.password is not None:
                problem = auth.validate_new_password(body.password)
                if problem:
                    raise HTTPException(400, problem)
                conn.execute("UPDATE users SET pw_hash = ? WHERE id = ?", (auth.hash_password(body.password), user_id))
                auth.end_all_sessions(conn, user_id)
            if body.two_step_off:
                conn.execute("UPDATE users SET totp_secret = NULL, totp_last = NULL WHERE id = ?", (user_id,))
                conn.execute("DELETE FROM recovery_codes WHERE user_id = ?", (user_id,))
            if body.is_admin is not None and bool(row["is_admin"]) != body.is_admin:
                if not body.is_admin and _admins(conn) <= 1:
                    raise HTTPException(400, "Snapshots needs at least one administrator.")
                conn.execute("UPDATE users SET is_admin = ? WHERE id = ?", (int(body.is_admin), user_id))
            return _user_json(conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone())

    @r.delete("/api/users/{user_id}")
    def delete_user(user_id: int, request: Request):
        me = require_admin(request)
        if user_id == me["id"]:
            raise HTTPException(400, "You can't delete your own account.")
        with db.session(cfg.db_path) as conn:
            row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if row is None:
                raise HTTPException(404, "No such account.")
            if row["is_admin"] and _admins(conn) <= 1:
                raise HTTPException(400, "Snapshots needs at least one administrator.")
            conn.execute("DELETE FROM users WHERE id = ?", (user_id,))
        return {"ok": True}

    return r
