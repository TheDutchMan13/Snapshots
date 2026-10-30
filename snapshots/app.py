"""The web server: JSON API plus the static web interface."""

import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db
from .auth import COOKIE, CSRF_HEADER, LoginThrottle, user_for_token
from .config import Config
from .worker import Worker


def _static_dir() -> Path:
    bundle = getattr(sys, "_MEIPASS", None)  # set when running from the PyInstaller exe
    if bundle:
        return Path(bundle) / "snapshots" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_CSP = (
    "default-src 'self'; img-src 'self' data:{tiles}; media-src 'self'; "
    "style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
)


def tile_source(template: str) -> str:
    """The web address map pictures come from, for the security policy ("" if none)."""
    m = re.match(r"^(https?://)([^/]+)/", template or "")
    if not m:
        return ""
    host = m.group(2)
    host = re.sub(r"^\{[a-z]\}\.", "*.", host)  # {s}.tile.example.org: any of its servers
    if "{" in host or "}" in host or "'" in host or ";" in host or " " in host:
        return ""
    return m.group(1) + host
# API addresses anyone may call without logging in.
PUBLIC_API = {"/api/health", "/api/auth/state", "/api/auth/login", "/api/auth/setup"}


def safe_filename(name: str | None) -> str:
    """A file name that is safe on Windows: no folders, no illegal characters."""
    name = Path((name or "").replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    stem = (stem or "upload")[:120]
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    return stem + ext.lower()


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Please log in.")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do that.")
    return user


def create_app(cfg: Config, start_worker: bool = True) -> FastAPI:
    cfg.ensure_dirs()
    db.init(cfg.db_path)
    worker = Worker(cfg)
    ctx = SimpleNamespace(cfg=cfg, worker=worker, throttle=LoginThrottle())

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
        yield
        worker.stop()

    app = FastAPI(title="Snapshots", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    tiles = tile_source(cfg.map_tiles)
    csp = _CSP.format(tiles=" " + tiles if tiles else "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            authz = request.headers.get("authorization", "")
            bearer = authz[7:].strip() if authz.lower().startswith("bearer ") else None
            token = bearer or request.cookies.get(COOKIE)
            # Browsers send cookies along with requests made by other websites; those
            # requests cannot carry this header, so changes without it are refused.
            if request.method not in ("GET", "HEAD", "OPTIONS") and not bearer \
                    and request.headers.get(CSRF_HEADER) != "1":
                return JSONResponse({"detail": "This request is missing its X-Snapshots-Request header."},
                                    status_code=403)
            user = None
            if token:
                with db.session(cfg.db_path) as conn:
                    user = user_for_token(conn, token)
            request.state.user = user
            request.state.token = token
            if user is None and path not in PUBLIC_API:
                return JSONResponse({"detail": "Please log in."}, status_code=401)
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = csp
        response.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    from . import (routes_auth, routes_collections, routes_explore, routes_library, routes_organize,
                   routes_people)

    for module in (routes_auth, routes_library, routes_collections, routes_people, routes_explore, routes_organize):
        app.include_router(module.router(ctx))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    return app
