import logging.handlers
import re
import time
from collections import deque
from pathlib import Path

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")


def strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def parse_line(service: str, raw: str) -> dict:
    ts_str, _, rest = raw.partition(" ")
    stream, _, text = rest.partition(" ")
    try:
        ts = float(ts_str)
    except ValueError:
        ts = 0.0
    return {"ts": ts, "service": service, "stream": stream, "text": text}


class LogSink:
    def __init__(self, log_file: Path, max_lines: int = 5000, max_bytes: int = 10 * 1024 * 1024, backups: int = 5):
        log_file.parent.mkdir(parents=True, exist_ok=True)
        self.buffer: deque[str] = deque(maxlen=max_lines)
        self._handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=max_bytes, backupCount=backups, encoding="utf-8"
        )
        self._handler.setFormatter(logging.Formatter("%(message)s"))
        self._subscribers: list = []

    def write(self, stream: str, line: str) -> None:
        clean = strip_ansi(line).rstrip("\n")
        formatted = f"{time.time():.3f} {stream} {clean}"
        self.buffer.append(formatted)
        record = logging.LogRecord("service", logging.INFO, "", 0, formatted, None, None)
        self._handler.emit(record)
        for subscriber in self._subscribers:
            subscriber(formatted)

    def tail(self, n: int = 500) -> list[str]:
        return list(self.buffer)[-n:]

    def subscribe(self, callback) -> None:
        self._subscribers.append(callback)

    def unsubscribe(self, callback) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def close(self) -> None:
        self._handler.close()
