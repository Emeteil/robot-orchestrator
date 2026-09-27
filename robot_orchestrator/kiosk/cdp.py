import asyncio
import json

import httpx
import websockets


class CdpError(Exception):
    pass


class KioskController:
    def __init__(
        self,
        debug_host: str = "127.0.0.1",
        debug_port: int = 9222,
        http_client: httpx.AsyncClient | None = None,
        timeout_s: float = 5.0,
    ):
        self.debug_host = debug_host
        self.debug_port = debug_port
        self.http_client = http_client or httpx.AsyncClient()
        self.timeout_s = timeout_s

    async def _find_page_ws_url(self) -> str:
        list_url = f"http://{self.debug_host}:{self.debug_port}/json/list"
        try:
            response = await self.http_client.get(list_url, timeout=self.timeout_s)
            response.raise_for_status()
            targets = response.json()
        except (httpx.HTTPError, ValueError) as e:
            raise CdpError(f"failed to list kiosk targets: {e}") from e

        for target in targets:
            if target.get("type") == "page" and "webSocketDebuggerUrl" in target:
                return target["webSocketDebuggerUrl"]
        raise CdpError("no page target with a webSocketDebuggerUrl found in /json/list")

    async def navigate(self, url: str) -> None:
        ws_url = await self._find_page_ws_url()

        request = {"id": 1, "method": "Page.navigate", "params": {"url": url}}
        try:
            async with websockets.connect(ws_url, open_timeout=self.timeout_s) as ws:
                await asyncio.wait_for(ws.send(json.dumps(request)), timeout=self.timeout_s)
                raw = await asyncio.wait_for(ws.recv(), timeout=self.timeout_s)
        except (OSError, TimeoutError, websockets.exceptions.WebSocketException) as e:
            raise CdpError(f"cdp navigate failed: {e}") from e

        try:
            reply = json.loads(raw)
        except ValueError as e:
            raise CdpError(f"invalid cdp response: {raw!r}") from e

        if reply.get("id") != request["id"] or "error" in reply:
            raise CdpError(f"cdp navigate rejected: {reply}")
