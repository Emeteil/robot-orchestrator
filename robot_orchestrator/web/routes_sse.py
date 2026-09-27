import asyncio
import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from robot_orchestrator.web import auth
from robot_orchestrator.web.context import AdminContext


def register_routes(app: FastAPI, context: AdminContext | None, jwt_secret: str) -> None:
    bearer = HTTPBearer(auto_error=False)

    def require_sse_auth(
        request: Request,
        credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    ) -> dict:
        token = credentials.credentials if credentials is not None else request.query_params.get("token")
        if not token:
            raise HTTPException(status_code=401, detail="missing token")
        try:
            return auth.verify_token(jwt_secret, token)
        except auth.TokenExpired:
            raise HTTPException(status_code=401, detail="token expired")
        except auth.TokenInvalid:
            raise HTTPException(status_code=401, detail="invalid token")

    @app.get("/api/logs/{service}/stream")
    async def stream_logs(service: str, claims: dict = Depends(require_sse_auth)):
        if context is None:
            raise HTTPException(status_code=503, detail="orchestrator context not available")
        sink = context.supervisor.log_sinks.get(service)
        if sink is None:
            raise HTTPException(status_code=404, detail=f"unknown service {service!r}")

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()

        def on_line(line: str) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, line)

        sink.subscribe(on_line)

        async def event_stream():
            try:
                for line in sink.tail(50):
                    yield f"data: {json.dumps(line)}\n\n"
                while True:
                    line = await queue.get()
                    yield f"data: {json.dumps(line)}\n\n"
            finally:
                sink.unsubscribe(on_line)

        return StreamingResponse(event_stream(), media_type="text/event-stream")
