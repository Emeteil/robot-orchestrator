import os
import secrets as stdlib_secrets

import yaml

from robot_orchestrator.paths import Paths
from robot_orchestrator.secrets.protocol import SecretsPayload, WifiCredential
from robot_orchestrator.wal.atomic import atomic_write

GENERATED_KEYS = ("MASTER_TOKEN", "flask_secret", "ORCH_JWT_SECRET")


def _ensure_secrets_dir(paths: Paths) -> None:
    paths.secrets_dir.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(paths.secrets_dir, 0o700)


def load_secrets(paths: Paths) -> dict:
    if not paths.secrets_file.exists():
        return {}
    with paths.secrets_file.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _load_generated(paths: Paths) -> dict:
    if not paths.generated_secrets_file.exists():
        return {}
    with paths.generated_secrets_file.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def write_secrets(paths: Paths, payload: SecretsPayload) -> None:
    _ensure_secrets_dir(paths)
    data = {
        "secrets": payload.secrets,
        "wifi": [w.model_dump() for w in payload.wifi],
        "config": payload.config,
    }
    blob = yaml.safe_dump(data, sort_keys=True).encode("utf-8")
    atomic_write(paths.secrets_file, blob)


def ensure_generated_secrets(paths: Paths) -> dict:
    if not paths.generated_secrets_file.exists():
        _ensure_secrets_dir(paths)
        generated = {key: stdlib_secrets.token_urlsafe(32) for key in GENERATED_KEYS}
        blob = yaml.safe_dump(generated, sort_keys=True).encode("utf-8")
        atomic_write(paths.generated_secrets_file, blob)
    return _load_generated(paths)


def missing_required_secrets(paths: Paths, required: list[str]) -> list[str]:
    secrets_data = load_secrets(paths).get("secrets", {}) or {}
    generated_data = _load_generated(paths)
    combined = {**generated_data, **secrets_data}
    return [key for key in required if not combined.get(key)]


def load_wifi_credentials(paths: Paths) -> list[WifiCredential]:
    data = load_secrets(paths).get("wifi", []) or []
    return [WifiCredential.model_validate(item) for item in data]
