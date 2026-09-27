import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
import websockets

from robot_orchestrator.kiosk.cdp import CdpError, KioskController


class _JsonListHandler(BaseHTTPRequestHandler):
    targets: list = []

    def do_GET(self):
        if self.path == "/json/list":
            body = json.dumps(self.targets).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass


def _start_http_server(targets: list) -> tuple[HTTPServer, int]:
    handler_cls = type("Handler", (_JsonListHandler,), {"targets": targets})
    server = HTTPServer(("127.0.0.1", 0), handler_cls)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, port


@pytest.fixture
def http_server():
    servers = []

    def _make(targets: list) -> int:
        server, port = _start_http_server(targets)
        servers.append(server)
        return port

    yield _make
    for server in servers:
        server.shutdown()


async def test_navigate_full_round_trip_success(http_server):
    received = {}

    async def handler(websocket):
        raw = await websocket.recv()
        received["message"] = json.loads(raw)
        await websocket.send(json.dumps({"id": 1, "result": {}}))

    async with websockets.serve(handler, "127.0.0.1", 0) as ws_server:
        ws_port = ws_server.sockets[0].getsockname()[1]
        ws_url = f"ws://127.0.0.1:{ws_port}/devtools/page/ABC"
        http_port = http_server([{"type": "page", "webSocketDebuggerUrl": ws_url}])

        controller = KioskController(debug_host="127.0.0.1", debug_port=http_port)
        await controller.navigate("http://example.com/target")

    assert received["message"]["method"] == "Page.navigate"
    assert received["message"]["params"]["url"] == "http://example.com/target"


async def test_navigate_raises_cdperror_on_ws_error_response(http_server):
    async def handler(websocket):
        await websocket.recv()
        await websocket.send(json.dumps({"id": 1, "error": {"message": "nope"}}))

    async with websockets.serve(handler, "127.0.0.1", 0) as ws_server:
        ws_port = ws_server.sockets[0].getsockname()[1]
        ws_url = f"ws://127.0.0.1:{ws_port}/devtools/page/ABC"
        http_port = http_server([{"type": "page", "webSocketDebuggerUrl": ws_url}])

        controller = KioskController(debug_host="127.0.0.1", debug_port=http_port)
        with pytest.raises(CdpError):
            await controller.navigate("http://example.com/target")


async def test_navigate_raises_cdperror_when_no_page_target(http_server):
    http_port = http_server([{"type": "background_page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/x"}])

    controller = KioskController(debug_host="127.0.0.1", debug_port=http_port)
    with pytest.raises(CdpError):
        await controller.navigate("http://example.com/target")


async def test_navigate_raises_cdperror_when_json_list_unreachable():
    controller = KioskController(debug_host="127.0.0.1", debug_port=1, timeout_s=1.0)

    with pytest.raises(CdpError):
        await controller.navigate("http://example.com/target")


async def test_navigate_raises_cdperror_on_ws_connect_failure(http_server):
    http_port = http_server([{"type": "page", "webSocketDebuggerUrl": "ws://127.0.0.1:1/devtools/page/ABC"}])

    controller = KioskController(debug_host="127.0.0.1", debug_port=http_port, timeout_s=1.0)
    with pytest.raises(CdpError):
        await controller.navigate("http://example.com/target")
