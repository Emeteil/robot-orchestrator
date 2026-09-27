import asyncio
import time
from dataclasses import dataclass

import httpx

from robot_orchestrator.config import ReadyProbeConfig


@dataclass
class ReadinessResult:
    ready: bool
    detail: str = ""


def dot_get(data, path: str):
    current = data
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


async def check_once(config: ReadyProbeConfig) -> ReadinessResult:
    if config.type == "none":
        return ReadinessResult(ready=True)

    if config.type in ("http", "http_json"):
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                response = await client.get(config.url)
        except httpx.HTTPError as e:
            return ReadinessResult(ready=False, detail=str(e))
        if response.status_code >= 400:
            return ReadinessResult(ready=False, detail=f"status {response.status_code}")
        if config.type == "http_json":
            try:
                data = response.json()
            except ValueError:
                return ReadinessResult(ready=False, detail="invalid json")
            value = dot_get(data, config.path) if config.path else data
            if value is not True:
                return ReadinessResult(ready=False, detail=f"{config.path}={value!r}")
        return ReadinessResult(ready=True)

    if config.type == "tcp":
        host, _, port = config.url.rpartition(":")
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(host, int(port)), timeout=5.0
            )
        except (OSError, asyncio.TimeoutError) as e:
            return ReadinessResult(ready=False, detail=str(e))
        writer.close()
        return ReadinessResult(ready=True)

    return ReadinessResult(ready=False, detail=f"unknown probe type {config.type!r}")


async def wait_until_ready(config: ReadyProbeConfig, poll_interval_s: float = 1.0) -> ReadinessResult:
    deadline = time.time() + config.timeout_s
    last = ReadinessResult(ready=False, detail="not checked yet")
    while True:
        last = await check_once(config)
        if last.ready:
            return last
        if time.time() >= deadline:
            return last
        await asyncio.sleep(poll_interval_s)
