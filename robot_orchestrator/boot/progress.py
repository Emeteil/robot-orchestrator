import threading
import time
from dataclasses import dataclass

from robot_orchestrator.bootlog import BOOT_LOG, BootEventLog

PENDING = "pending"
RUNNING = "running"
OK = "ok"
WARN = "warn"
FAILED = "failed"
SKIPPED = "skipped"

TERMINAL = {OK, WARN, FAILED, SKIPPED}

DEFAULT_STEPS: list[tuple[str, str]] = [
    ("init", "Инициализация"),
    ("recover", "Восстановление журнала"),
    ("gpio", "Перемычка GPIO"),
    ("secrets", "Секреты"),
    ("qr", "Сканирование QR-кода"),
    ("camera", "Камера"),
    ("microphone", "Микрофон"),
    ("network", "Сеть и интернет"),
    ("tooling", "Инструменты прошивки"),
    ("mcu", "ST-Link и микроконтроллер"),
    ("repos", "Обновление репозиториев"),
    ("firmware", "Прошивка микроконтроллера"),
    ("mode", "Определение режима"),
    ("services", "Запуск сервисов"),
    ("handoff", "Запуск интерфейса"),
]

_LEVEL_FOR_STATUS = {OK: "ok", WARN: "warn", FAILED: "error", SKIPPED: "dim"}
_MARK_FOR_STATUS = {OK: "✔", WARN: "⚠", FAILED: "✖", SKIPPED: "–"}


@dataclass
class BootStep:
    id: str
    title: str
    status: str = PENDING
    detail: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id, "title": self.title, "status": self.status, "detail": self.detail,
            "started_at": self.started_at, "finished_at": self.finished_at,
        }


class BootProgress:
    def __init__(self, log: BootEventLog | None = None, steps: list[tuple[str, str]] | None = None):
        self.log = log or BOOT_LOG
        self.steps = [BootStep(step_id, title) for step_id, title in (steps or DEFAULT_STEPS)]
        self._by_id = {step.id: step for step in self.steps}
        self.step = "starting"
        self.started_at = time.time()
        self.finished_at: float | None = None
        self._lock = threading.Lock()

    def begin(self, step_id: str, detail: str = "") -> None:
        with self._lock:
            step = self._by_id[step_id]
            step.status = RUNNING
            step.detail = detail
            step.started_at = time.time()
            step.finished_at = None
            self.step = step_id
            title = step.title
        self.log.emit("boot", f"▶ {title}" + (f" — {detail}" if detail else ""), "step")

    def update(self, step_id: str, detail: str) -> None:
        with self._lock:
            self._by_id[step_id].detail = detail

    def finish(self, step_id: str, status: str = OK, detail: str = "") -> None:
        with self._lock:
            step = self._by_id[step_id]
            step.status = status
            if detail:
                step.detail = detail
            now = time.time()
            step.started_at = step.started_at or now
            step.finished_at = now
            title, final_detail = step.title, step.detail
        text = f"{_MARK_FOR_STATUS.get(status, '•')} {title}" + (f" — {final_detail}" if final_detail else "")
        self.log.emit("boot", text, _LEVEL_FOR_STATUS.get(status, "info"))

    def skip(self, step_id: str, detail: str = "") -> None:
        self.finish(step_id, SKIPPED, detail)

    def note(self, text: str, level: str = "info", source: str = "boot") -> None:
        self.log.emit(source, text, level)

    def complete(self) -> None:
        with self._lock:
            self.finished_at = time.time()
            self.step = "done"

    def snapshot(self) -> dict:
        with self._lock:
            steps = [step.to_dict() for step in self.steps]
            now = time.time()
            end = self.finished_at or now
            finished = sum(1 for step in self.steps if step.status in TERMINAL)
            running = sum(1 for step in self.steps if step.status == RUNNING)
            percent = round(100 * (finished + 0.5 * running) / len(self.steps))
            return {
                "step": self.step,
                "steps": steps,
                "started_at": self.started_at,
                "elapsed": round(end - self.started_at, 1),
                "percent": 100 if self.finished_at else min(percent, 99),
            }
