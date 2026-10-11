import json
import re
import threading
import time

from robot_orchestrator.paths import Paths
from robot_orchestrator.wal.atomic import atomic_write
from robot_orchestrator.web.auth import hash_password, verify_password_hash

MAX_FAILURES = 5
LOCKOUT_S = 30.0
_FORMAT = re.compile(r"^\d{4}$")


class PinError(Exception):
    pass


class PinLocked(PinError):
    def __init__(self, retry_after: float):
        super().__init__("too many attempts")
        self.retry_after = retry_after


class PinInvalid(PinError):
    def __init__(self, attempts_left: int):
        super().__init__("wrong pin")
        self.attempts_left = attempts_left


class SettingsPin:
    """The 4-digit PIN guarding the settings modal on the robot's own screen.

    It lives next to the other secrets so it survives power loss; the kiosk browser's
    profile is on tmpfs and cannot hold it.
    """

    def __init__(self, paths: Paths, clock=time.monotonic):
        self._path = paths.settings_pin_file
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._locked_until = 0.0

    def is_set(self) -> bool:
        return self._path.exists()

    def locked_for(self) -> float:
        with self._lock:
            return max(0.0, self._locked_until - self._clock())

    def set(self, pin: str) -> None:
        if not _FORMAT.match(pin or ""):
            raise PinError("pin must be 4 digits")
        salt, digest = hash_password(pin)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        atomic_write(self._path, json.dumps({"salt_hex": salt.hex(), "hash_hex": digest.hex()}).encode("utf-8"))
        with self._lock:
            self._failures = 0
            self._locked_until = 0.0

    def verify(self, pin: str) -> None:
        """Returns normally on a correct PIN, raises PinLocked / PinInvalid otherwise."""
        with self._lock:
            now = self._clock()
            if self._locked_until > now:
                raise PinLocked(self._locked_until - now)
            record = self._load()
            ok = bool(record) and isinstance(pin, str) and verify_password_hash(
                pin, bytes.fromhex(record["salt_hex"]), bytes.fromhex(record["hash_hex"])
            )
            if ok:
                self._failures = 0
                return
            self._failures += 1
            if self._failures >= MAX_FAILURES:
                self._failures = 0
                self._locked_until = now + LOCKOUT_S
                raise PinLocked(LOCKOUT_S)
            raise PinInvalid(MAX_FAILURES - self._failures)

    def _load(self) -> dict | None:
        try:
            return json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
