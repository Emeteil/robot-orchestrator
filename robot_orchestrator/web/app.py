import asyncio
import json
from pathlib import Path
from typing import Awaitable, Callable

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from robot_orchestrator import bootlog
from robot_orchestrator.paths import Paths
from robot_orchestrator.web import auth, routes_api, routes_sse
from robot_orchestrator.web.context import AdminContext

LOOPBACK_HOSTS = ("127.0.0.1", "::1", "localhost")
STATIC_DIR = Path(__file__).resolve().parent / "static"


class LoginRequest(BaseModel):
    username: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    expires_in: int


def require_loopback(request: Request) -> None:
    host = request.client.host if request.client else None
    if host not in LOOPBACK_HOSTS:
        raise HTTPException(status_code=403, detail="loopback only")


def _resume_position(request: Request) -> int:
    raw = request.headers.get("last-event-id") or request.query_params.get("last") or ""
    epoch, _, seq = raw.partition(":")
    if epoch == bootlog.BOOT_LOG.epoch and seq.isdigit():
        return int(seq)
    return 0


def _format_event(event: dict, epoch: str) -> str:
    return f"id: {epoch}:{event['seq']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


async def boot_event_stream(
    log: bootlog.BootEventLog,
    after_seq: int,
    is_disconnected: Callable[[], Awaitable[bool]],
    backlog_limit: int = 600,
    keepalive_s: float = 15.0,
):
    queue: asyncio.Queue = asyncio.Queue(maxsize=5000)
    loop = asyncio.get_running_loop()

    def offer(event: dict) -> None:
        try:
            queue.put_nowait(event)
        except asyncio.QueueFull:
            pass

    def on_event(event: dict) -> None:
        try:
            loop.call_soon_threadsafe(offer, event)
        except RuntimeError:
            pass

    log.subscribe(on_event)
    try:
        yield "retry: 2000\n\n"
        last = after_seq
        for event in log.tail(after_seq, backlog_limit):
            last = event["seq"]
            yield _format_event(event, log.epoch)
        # tells the page that everything before this point is history and everything after is live
        yield "event: live\ndata: {}\n\n"
        while True:
            try:
                event = await asyncio.wait_for(queue.get(), timeout=keepalive_s)
            except asyncio.TimeoutError:
                if await is_disconnected():
                    return
                yield ": keepalive\n\n"
                continue
            if event["seq"] <= last:
                continue
            last = event["seq"]
            yield _format_event(event, log.epoch)
    finally:
        log.unsubscribe(on_event)


def create_app(
    paths: Paths, jwt_secret: str, jwt_ttl_s: int = 600, context: AdminContext | None = None
) -> FastAPI:
    app = FastAPI()
    throttle = auth.LoginThrottle()
    bearer = HTTPBearer(auto_error=False)

    def require_auth(
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> dict:
        if credentials is None:
            raise HTTPException(status_code=401, detail="missing token")
        try:
            return auth.verify_token(jwt_secret, credentials.credentials)
        except auth.TokenExpired:
            raise HTTPException(status_code=401, detail="token expired")
        except auth.TokenInvalid:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.post("/api/auth/login", response_model=LoginResponse)
    def login(body: LoginRequest, request: Request):
        client_ip = request.client.host if request.client else "unknown"
        if throttle.is_blocked(client_ip):
            raise HTTPException(status_code=429, detail="too many attempts")
        if not auth.verify_login(paths, body.username, body.password):
            throttle.record_failure(client_ip)
            raise HTTPException(status_code=401, detail="invalid credentials")
        throttle.clear(client_ip)
        token = auth.issue_token(jwt_secret, body.username, jwt_ttl_s)
        return LoginResponse(access_token=token, expires_in=jwt_ttl_s)

    @app.get("/api/auth/whoami")
    def whoami(claims: dict = Depends(require_auth)):
        return {"username": claims["sub"]}

    @app.get("/boot", include_in_schema=False)
    def boot_page(request: Request):
        require_loopback(request)
        boot_html = STATIC_DIR / "boot" / "index.html"
        if boot_html.exists():
            return FileResponse(boot_html)
        return {"page": "boot"}

    @app.get("/boot/api/state")
    def boot_state(request: Request):
        require_loopback(request)
        progress = context.boot_progress.snapshot() if context and context.boot_progress else {}
        base = {**progress, "epoch": bootlog.BOOT_LOG.epoch}
        if context is None or context.boot_result is None:
            base.setdefault("step", "starting")
            return {"state": "booting", **base}
        boot = context.boot_result
        finished = bool(context.boot_progress and context.boot_progress.finished_at)
        return {
            "state": "running",
            **base,
            "finished": finished,
            "production": boot.mode.production,
            "reasons": boot.mode.reasons,
            "capabilities": boot.mode.capabilities,
            "services_ready": boot.services_ready,
            "operator_url": context.operator_url,
        }

    @app.get("/boot/api/stream", include_in_schema=False)
    async def boot_stream(request: Request):
        require_loopback(request)
        after = _resume_position(request)
        return StreamingResponse(
            boot_event_stream(bootlog.BOOT_LOG, after, request.is_disconnected),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/boot/api/qr-preview", include_in_schema=False)
    def qr_preview(request: Request):
        require_loopback(request)
        preview_path = paths.qr_preview
        if not preview_path.exists():
            raise HTTPException(status_code=404, detail="no qr preview available yet")
        return FileResponse(preview_path, media_type="image/jpeg")

    @app.get("/admin", include_in_schema=False)
    def admin_page():
        admin_html = STATIC_DIR / "admin" / "index.html"
        if admin_html.exists():
            return FileResponse(admin_html)
        return {"page": "admin"}

    if STATIC_DIR.exists():
        app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")

    routes_api.register_routes(app, context, require_auth)
    routes_sse.register_routes(app, context, jwt_secret)

    app.state.require_auth = require_auth
    app.state.throttle = throttle
    return app
