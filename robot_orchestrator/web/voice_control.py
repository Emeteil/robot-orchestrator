from urllib.parse import urlsplit

import httpx

from robot_orchestrator.secrets import store as secrets_store
from robot_orchestrator.web.context import AdminContext

TIMEOUT_S = 6.0


class VoiceControlError(Exception):
    def __init__(self, message: str, status: int = 502):
        super().__init__(message)
        self.status = status


def _base_and_token(context: AdminContext) -> tuple[str, str]:
    if not context.operator_url:
        raise VoiceControlError("web-core address is unknown", 503)
    parts = urlsplit(context.operator_url)
    token = secrets_store.ensure_generated_secrets(context.paths).get("MASTER_TOKEN", "")
    return f"{parts.scheme}://{parts.netloc}", token


def _call(context: AdminContext, method: str, path: str, payload: dict | None = None) -> dict:
    base, token = _base_and_token(context)
    try:
        response = httpx.request(
            method, base + path, params={"token": token}, json=payload, timeout=TIMEOUT_S,
        )
    except httpx.HTTPError as e:
        raise VoiceControlError(f"web-core is not reachable: {e}", 503)
    if response.status_code >= 400:
        try:
            message = response.json().get("error", {}).get("message") or response.text
        except ValueError:
            message = response.text
        raise VoiceControlError(str(message), response.status_code)
    return response.json().get("data") or {}


def voice_status(context: AdminContext) -> dict:
    return _call(context, "GET", "/api/voice/status")


def set_wake_word(context: AdminContext, enabled: bool) -> dict:
    return _call(context, "POST", "/api/voice/wake-word", {"enabled": enabled})
