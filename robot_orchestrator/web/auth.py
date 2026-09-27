import hashlib
import hmac
import json
import os
import time
import uuid
from dataclasses import dataclass

import jwt

from robot_orchestrator.paths import Paths
from robot_orchestrator.wal.atomic import atomic_write


class TokenExpired(Exception):
    pass


class TokenInvalid(Exception):
    pass


def hash_password(password: str, salt: bytes | None = None) -> tuple[bytes, bytes]:
    salt = salt if salt is not None else os.urandom(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1)
    return salt, digest


def verify_password_hash(password: str, salt: bytes, digest: bytes) -> bool:
    _, candidate = hash_password(password, salt)
    return hmac.compare_digest(candidate, digest)


@dataclass
class AdminRecord:
    username: str
    salt_hex: str
    hash_hex: str


def load_admin(paths: Paths) -> AdminRecord | None:
    if not paths.admin_file.exists():
        return None
    data = json.loads(paths.admin_file.read_text(encoding="utf-8"))
    return AdminRecord(**data)


def save_admin(paths: Paths, username: str, password: str) -> None:
    salt, digest = hash_password(password)
    data = {"username": username, "salt_hex": salt.hex(), "hash_hex": digest.hex()}
    paths.admin_file.parent.mkdir(parents=True, exist_ok=True)
    atomic_write(paths.admin_file, json.dumps(data).encode("utf-8"))


def ensure_default_admin(paths: Paths, username: str = "admin", default_password: str = "admin") -> AdminRecord:
    existing = load_admin(paths)
    if existing is not None:
        return existing
    save_admin(paths, username, default_password)
    return load_admin(paths)


def verify_login(paths: Paths, username: str, password: str) -> bool:
    record = load_admin(paths)
    if record is None or record.username != username:
        return False
    return verify_password_hash(password, bytes.fromhex(record.salt_hex), bytes.fromhex(record.hash_hex))


def is_default_password(paths: Paths, default_password: str = "admin") -> bool:
    record = load_admin(paths)
    if record is None:
        return True
    return verify_password_hash(default_password, bytes.fromhex(record.salt_hex), bytes.fromhex(record.hash_hex))


def issue_token(secret: str, username: str, ttl_s: int) -> str:
    now = int(time.time())
    payload = {"sub": username, "iat": now, "exp": now + ttl_s, "jti": uuid.uuid4().hex}
    return jwt.encode(payload, secret, algorithm="HS256")


def verify_token(secret: str, token: str) -> dict:
    try:
        return jwt.decode(token, secret, algorithms=["HS256"])
    except jwt.ExpiredSignatureError as e:
        raise TokenExpired() from e
    except jwt.InvalidTokenError as e:
        raise TokenInvalid() from e


class LoginThrottle:
    def __init__(self, max_failures: int = 5, window_s: float = 60.0):
        self.max_failures = max_failures
        self.window_s = window_s
        self._failures: dict[str, list[float]] = {}

    def record_failure(self, ip: str, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self._failures.setdefault(ip, []).append(now)
        self._prune(ip, now)

    def is_blocked(self, ip: str, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        self._prune(ip, now)
        return len(self._failures.get(ip, [])) >= self.max_failures

    def _prune(self, ip: str, now: float) -> None:
        cutoff = now - self.window_s
        self._failures[ip] = [t for t in self._failures.get(ip, []) if t >= cutoff]

    def clear(self, ip: str) -> None:
        self._failures.pop(ip, None)
