import collections
import logging
import re
import subprocess
import threading
import time
import uuid
from typing import Callable

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[a-zA-Z]")
_ERROR_RE = re.compile(r"\b(error|traceback|fatal|exception|failed|segmentation fault|critical)\b", re.IGNORECASE)
_WARN_RE = re.compile(r"\b(warn|warning|deprecated|retry|retrying)\b", re.IGNORECASE)
_MAX_LINE = 600


_URL_USERINFO_RE = re.compile(r"(\b[a-zA-Z][a-zA-Z0-9+.\-]*://)([^/\s:@]+):([^@\s/]+)@")
_QUERY_SECRET_RE = re.compile(
    r"([?&](?:token|key|api_key|apikey|access_token|password|passwd|psk|secret)=)[^&\s\"']+", re.IGNORECASE
)
_KV_SECRET_RE = re.compile(
    r"\b(password|passwd|psk|secret|token|api_key|apikey)\b(\s*[=:]\s*)(\"?)[^\s\"',;]+", re.IGNORECASE
)
_BEARER_RE = re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=\-]+", re.IGNORECASE)
_MASK = "••••"


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def redact(text: str, known_secrets: tuple[str, ...] = ()) -> str:
    """Hide credentials before a line can reach a screen or a network client."""
    for secret in known_secrets:
        if secret in text:
            text = text.replace(secret, _MASK)
    text = _URL_USERINFO_RE.sub(r"\1***:***@", text)
    text = _QUERY_SECRET_RE.sub(rf"\1{_MASK}", text)
    text = _KV_SECRET_RE.sub(rf"\1\2\3{_MASK}", text)
    return _BEARER_RE.sub(rf"\1{_MASK}", text)


def classify_level(text: str, default: str = "info") -> str:
    if _ERROR_RE.search(text):
        return "error"
    if _WARN_RE.search(text):
        return "warn"
    return default


class BootEventLog:
    """Thread-safe ring buffer of boot events that any number of consumers can follow live."""

    def __init__(self, max_events: int = 4000):
        self.epoch = uuid.uuid4().hex[:12]
        self._events: collections.deque[dict] = collections.deque(maxlen=max_events)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[Callable[[dict], None]] = []
        self._known_secrets: tuple[str, ...] = ()

    def set_known_secrets(self, values) -> None:
        cleaned = {str(v) for v in values if v and len(str(v)) >= 6}
        self._known_secrets = tuple(sorted(cleaned, key=len, reverse=True))

    def emit(self, source: str, text: str, level: str = "info") -> dict | None:
        text = redact(strip_ansi(text).rstrip(), self._known_secrets)
        if not text:
            return None
        if len(text) > _MAX_LINE:
            text = text[:_MAX_LINE] + "…"
        with self._lock:
            self._seq += 1
            event = {"seq": self._seq, "ts": time.time(), "source": source, "level": level, "text": text}
            self._events.append(event)
            subscribers = list(self._subscribers)
        for callback in subscribers:
            try:
                callback(event)
            except Exception:
                pass
        return event

    def tail(self, after_seq: int = 0, limit: int = 500) -> list[dict]:
        with self._lock:
            events = [e for e in self._events if e["seq"] > after_seq]
        return events[-limit:]

    def subscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable[[dict], None]) -> None:
        with self._lock:
            if callback in self._subscribers:
                self._subscribers.remove(callback)

    @property
    def last_seq(self) -> int:
        with self._lock:
            return self._seq


BOOT_LOG = BootEventLog()


def forward_service_line(name: str, raw: str, log: BootEventLog | None = None) -> None:
    """Mirror a LogSink line ('<ts> <stream> <text>') into the boot event log."""
    from robot_orchestrator.supervisor.logsink import parse_line

    parsed = parse_line(name, raw)
    (log or BOOT_LOG).emit(name, parsed["text"], classify_level(parsed["text"]))


class BootLogHandler(logging.Handler):
    """Forwards orchestrator log records (not uvicorn/asyncio chatter) to the boot event log."""

    def __init__(self, log: BootEventLog | None = None):
        super().__init__(level=logging.INFO)
        self._log = log or BOOT_LOG

    def emit(self, record: logging.LogRecord) -> None:
        if record.name.startswith(("uvicorn", "asyncio", "http")):
            return
        try:
            level = {"WARNING": "warn", "ERROR": "error", "CRITICAL": "error"}.get(record.levelname, "info")
            self._log.emit("orchestrator", record.getMessage(), level)
        except Exception:
            pass


def _mask_values(text: str, mask: tuple[str, ...]) -> str:
    for value in mask:
        if value:
            text = text.replace(value, _MASK)
    return text


def _pump(stream, source: str, sink: list[str], log: BootEventLog, mask: tuple[str, ...]) -> None:
    last = None
    try:
        for raw in stream:
            sink.append(raw)
            line = _mask_values(strip_ansi(raw).rstrip(), mask)
            if not line or line == last:
                continue
            last = line
            log.emit(source, line, classify_level(line))
    except (ValueError, OSError):
        pass


def _describe(argv, mask: tuple[str, ...] = ()) -> str:
    text = argv if isinstance(argv, str) else " ".join(str(part) for part in argv)
    text = _mask_values(text, mask)
    return text if len(text) <= 240 else text[:237] + "..."


def logged_run(
    argv,
    *,
    source: str,
    cwd=None,
    env=None,
    timeout: float | None = None,
    check: bool = False,
    shell: bool = False,
    announce: bool = True,
    mask: tuple[str, ...] = (),
    log: BootEventLog | None = None,
) -> subprocess.CompletedProcess:
    """Drop-in for subprocess.run(capture_output=True, text=True) that also streams every output line live."""
    log = log or BOOT_LOG
    if announce:
        log.emit(source, f"$ {_describe(argv, mask)}", "cmd")

    process = subprocess.Popen(
        argv, cwd=cwd, env=env, shell=shell,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        text=True, encoding="utf-8", errors="replace", bufsize=1,
    )
    out_lines: list[str] = []
    err_lines: list[str] = []
    threads = [
        threading.Thread(target=_pump, args=(process.stdout, source, out_lines, log, mask), daemon=True),
        threading.Thread(target=_pump, args=(process.stderr, source, err_lines, log, mask), daemon=True),
    ]
    for thread in threads:
        thread.start()

    try:
        process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
        for thread in threads:
            thread.join(timeout=2.0)
        log.emit(source, f"таймаут {timeout}s: процесс убит", "error")
        raise subprocess.TimeoutExpired(argv, timeout, output="".join(out_lines), stderr="".join(err_lines))
    except BaseException:
        process.kill()
        process.wait()
        raise

    for thread in threads:
        thread.join(timeout=5.0)

    stdout, stderr = "".join(out_lines), "".join(err_lines)
    if announce and process.returncode != 0:
        log.emit(source, f"завершилось с кодом {process.returncode}", "error")
    if check and process.returncode != 0:
        raise subprocess.CalledProcessError(process.returncode, argv, output=stdout, stderr=stderr)
    return subprocess.CompletedProcess(argv, process.returncode, stdout, stderr)
