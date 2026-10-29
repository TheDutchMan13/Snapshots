"""Logging in and out, and managing accounts."""

from datetime import datetime

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from . import __version__, auth, db
from .app import current_user, require_admin


class Credentials(BaseModel):
    username: str
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


def _is_https(request: Request) -> bool:
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def _start_session(request: Request, response: Response, conn, user_row) -> dict:
    token = auth.create_session(conn, user_row["id"], request.headers.get("user-agent"))
    response.set_cookie(
        auth.COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True,
        samesite="lax", secure=_is_https(request), path="/",
    )
    user = {"id": user_row["id"], "username": user_row["username"], "is_admin": bool(user_row["is_admin"])}
    # The token is also returned so that apps that are not browsers can use it.
    return {"user": user, "token": token}


def _user_json(row) -> dict:
    return {"id": row["id"], "username": row["username"], "is_admin": bool(row["is_admin"]),
            "created_at": row["created_at"]}


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
        return {"needs_setup": needs_setup, "user": request.state.user, "version": __version__}

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

    @r.post("/api/auth/login")
    def login(body: Credentials, request: Request, response: Response):
        keys = (f"user:{body.username.strip().lower()}", f"addr:{request.client.host if request.client else '-'}")
        wait = ctx.throttle.retry_after(*keys)
        if wait:
            raise HTTPException(429, f"Too many wrong passwords. Try again in {max(1, wait // 60)} minute(s).",
                                headers={"Retry-After": str(wait)})
        with db.session(cfg.db_path) as conn:
            row = auth.check_login(conn, body.username, body.password)
            if row is None:
                ctx.throttle.failed(*keys)
                raise HTTPException(401, "Wrong user name or password.")
            ctx.throttle.succeeded(*keys)
            return _start_session(request, response, conn, row)

    @r.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        with db.session(cfg.db_path) as conn:
            auth.end_session(conn, request.state.token)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @r.post("/api/auth/password")
    def change_password(body: PasswordChange, request: Request):
        user = current_user(request)
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
            return {"users": [_user_json(u) for u in conn.execute("SELECT * FROM users ORDER BY username")]}

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
