import json
import logging
import sys
import time
from pathlib import Path


class JsonLineFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": time.time(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in ("args", "msg", "levelname", "levelno", "name", "created", "msecs",
                       "relativeCreated", "exc_info", "exc_text", "stack_info", "pathname",
                       "filename", "module", "lineno", "funcName", "processName", "process",
                       "threadName", "thread"):
                continue
            if key not in payload:
                payload[key] = value
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


class QuietBootPolling(logging.Filter):
    """The boot screen polls its own endpoints every second; keep that out of the access log."""

    def filter(self, record: logging.LogRecord) -> bool:
        args = record.args
        if isinstance(args, tuple) and len(args) >= 3 and str(args[2]).startswith("/boot/api/"):
            return False
        return True


def configure_logging(log_file: Path | None = None, level: int = logging.INFO) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(JsonLineFormatter())
    root.addHandler(stream_handler)

    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(JsonLineFormatter())
        root.addHandler(file_handler)

    access_logger = logging.getLogger("uvicorn.access")
    if not any(isinstance(f, QuietBootPolling) for f in access_logger.filters):
        access_logger.addFilter(QuietBootPolling())
