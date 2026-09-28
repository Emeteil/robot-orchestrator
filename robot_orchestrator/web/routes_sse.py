import asyncio
import json

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from robot_orchestrator.supervisor.logsink import parse_line
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

        if service == "all":
            sinks = dict(context.supervisor.log_sinks)
        else:
            sink = context.supervisor.log_sinks.get(service)
            if sink is None:
                raise HTTPException(status_code=404, detail=f"unknown service {service!r}")
            sinks = {service: sink}

        queue: asyncio.Queue = asyncio.Queue()
        loop = asyncio.get_running_loop()
        subscriptions = []

        for sink_name, sink in sinks.items():
            def on_line(line: str, sink_name=sink_name) -> None:
                loop.call_soon_threadsafe(queue.put_nowait, (sink_name, line))
            sink.subscribe(on_line)
            subscriptions.append((sink, on_line))

        async def event_stream():
            try:
                initial = [
                    parse_line(sink_name, raw)
                    for sink_name, sink in sinks.items()
                    for raw in sink.tail(50)
                ]
                initial.sort(key=lambda item: item["ts"])
                for item in initial:
                    yield f"data: {json.dumps(item)}\n\n"
                while True:
                    sink_name, raw = await queue.get()
                    yield f"data: {json.dumps(parse_line(sink_name, raw))}\n\n"
            finally:
                for sink, on_line in subscriptions:
                    sink.unsubscribe(on_line)

        return StreamingResponse(event_stream(), media_type="text/event-stream")
