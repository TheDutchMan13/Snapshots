"""The web server: JSON API plus the static web interface."""

import logging
import os
import re
import sys
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, db, webhooks
from .auth import COOKIE, CSRF_HEADER, USER_LIMIT, LoginThrottle, Tickets, host_allowed, user_for_token
from .config import Config
from .updates import UpdateChecker
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
PUBLIC_API = {"/api/health", "/api/auth/state", "/api/auth/login", "/api/auth/login/code", "/api/auth/setup"}


def safe_filename(name: str | None) -> str:
    """A file name that is safe on Windows: no folders, no illegal characters."""
    name = Path((name or "").replace("\\", "/")).name
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .")
    stem, ext = os.path.splitext(name)
    stem = (stem or "upload")[:120]
    if stem.upper() in _RESERVED:
        stem = "_" + stem
    return stem + ext.lower()


UPLOAD_RESERVE = 1024 ** 3        # an upload must leave at least this much free on the library's drive ...
TEMP_RESERVE = 100 * 1024 ** 2    # ... and this much in the temporary folder (where it waits until it is read)


def upload_problem(cfg, length: str | None) -> tuple[int, str] | None:
    """Why an upload of `length` bytes is refused before it is read (HTTP status, message), or None.
    Files are first stored in the temporary folder, then in the library, so both need the room."""
    import shutil
    import tempfile

    try:
        size = int(length)
    except (TypeError, ValueError):
        return 411, "Uploads need their size (Content-Length)."
    if size > cfg.max_upload_gb * 1024 ** 3:
        return 413, f"Upload failed: this is more than {cfg.max_upload_gb:g} GB at once. Send fewer files at a time."
    for folder, reserve in ((str(cfg.library_path), UPLOAD_RESERVE), (tempfile.gettempdir(), TEMP_RESERVE)):
        try:
            free = shutil.disk_usage(folder).free
        except OSError:
            continue
        if free < size + reserve:
            return 507, "Upload failed: the PC's drive is almost full."
    return None


def current_user(request: Request) -> dict:
    user = getattr(request.state, "user", None)
    if user is None:
        raise HTTPException(401, "Please log in.")
    return user


def require_session(request: Request) -> dict:
    """The logged-in user, refusing API keys (for managing keys, passwords and the locked folder)."""
    user = current_user(request)
    if user.get("api_key"):
        raise HTTPException(403, "An API key can't do that. Please use the app.")
    return user


def require_admin(request: Request) -> dict:
    user = current_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Only an administrator can do that.")
    return user


def require_admin_session(request: Request) -> dict:
    """An administrator signed in with a password (not an API key): for managing accounts, which can
    reset passwords and so reach everything a key is kept away from."""
    require_session(request)
    return require_admin(request)


MAX_BODY = 1024 * 1024        # requests other than uploads are small: anything bigger is refused unread


class BodyLimit:
    """Refuses request bodies over MAX_BODY on every address except the upload, also when the size
    isn't given up front (chunked), so nobody can fill the PC's memory with one huge request."""

    def __init__(self, app, limit: int = MAX_BODY, exempt: tuple = ("/api/upload",)):
        self.app, self.limit, self.exempt = app, limit, exempt

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("path") in self.exempt:
            return await self.app(scope, receive, send)
        for name, value in scope.get("headers") or ():
            if name == b"content-length":
                try:
                    too_big = int(value) > self.limit
                except ValueError:
                    too_big = True
                if too_big:
                    return await self._refuse(send)
        # Read the body first (it is small, or it is refused), then hand it on as it came.
        messages, seen = [], 0
        while True:
            message = await receive()
            messages.append(message)
            if message["type"] != "http.request":
                break
            seen += len(message.get("body", b""))
            if seen > self.limit:
                return await self._refuse(send)
            if not message.get("more_body"):
                break

        async def replay():
            if messages:
                return messages.pop(0)
            return await receive()

        await self.app(scope, replay, send)

    @staticmethod
    async def _refuse(send):
        body = b'{"detail":"That request is too big."}'
        await send({"type": "http.response.start", "status": 413,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(body)).encode()),
                                (b"connection", b"close")]})
        await send({"type": "http.response.body", "body": body})


def create_app(cfg: Config, start_worker: bool = True) -> FastAPI:
    cfg.ensure_dirs()
    db.init(cfg.db_path, cfg.backups_path)
    try:        # kept old files whose item is gone never stay under its number (filechange.py)
        from . import filechange
        with db.session(cfg.db_path) as conn:
            filechange.detach_orphans(cfg, conn)
    except Exception:
        logging.getLogger("snapshots").exception("could not tidy the kept old files")
    worker = Worker(cfg)
    sender = webhooks.Sender(cfg)
    ctx = SimpleNamespace(cfg=cfg, worker=worker, throttle=LoginThrottle(limits={"user:": USER_LIMIT}), pin_throttle=LoginThrottle(limit=5), tickets=Tickets(), pending_totp={},
                          webhooks=sender, quit=None, updates=UpdateChecker(cfg.check_updates))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if start_worker:
            worker.start()
            sender.start()
        yield
        worker.stop()
        sender.stop()

    app = FastAPI(title="Snapshots", version=__version__, lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.ctx = ctx
    tiles = tile_source(cfg.map_tiles)
    csp = _CSP.format(tiles=" " + tiles if tiles else "")

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        if not host_allowed(request.headers.get("host"), cfg.allowed_hosts):
            name = (request.headers.get("host") or "").split(":")[0][:100]
            logging.getLogger("snapshots").warning("refused a request for %r (not one of this PC's names)", name)
            return JSONResponse(
                {"detail": f"Snapshots doesn't answer to the name {name}. If that is your own address for it "
                           f"(a reverse proxy), add it to allowed_hosts in config.toml, like "
                           f"allowed_hosts = [\"{name}\"], and restart Snapshots."}, status_code=421)
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
            if path == "/api/upload" and request.method == "POST":
                problem = upload_problem(cfg, request.headers.get("content-length"))
                if problem:
                    return JSONResponse({"detail": problem[1]}, status_code=problem[0])
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Content-Security-Policy"] = csp
        response.headers["Referrer-Policy"] = "no-referrer"
        if path.startswith("/api/") and "cache-control" not in response.headers:
            response.headers["Cache-Control"] = "no-store"
        return response

    from . import (routes_auth, routes_collections, routes_explore, routes_imports, routes_library, routes_organize,
                   routes_people, routes_reports, routes_settings, routes_social, routes_tools)

    for module in (routes_auth, routes_library, routes_collections, routes_people, routes_explore, routes_organize,
                   routes_social, routes_settings, routes_imports, routes_tools, routes_reports):
        app.include_router(module.router(ctx))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"})

    @app.get("/login", include_in_schema=False)
    def login_page():
        return FileResponse(STATIC_DIR / "login.html", headers={"Cache-Control": "no-cache"})

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.add_middleware(BodyLimit)
    return app
