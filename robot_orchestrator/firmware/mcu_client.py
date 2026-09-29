import json
import subprocess
from pathlib import Path
from typing import Callable

from robot_orchestrator.bootlog import logged_run

RunnerT = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return logged_run(argv, source="mcu", timeout=timeout)


class SubprocessMcuClient:
    def __init__(
        self,
        python_executable: Path,
        mcu_probe_script: Path,
        port: str,
        connect_timeout_s: float = 10.0,
        ping_tries: int = 3,
        runner: RunnerT | None = None,
    ):
        self.python_executable = python_executable
        self.mcu_probe_script = mcu_probe_script
        self.port = port
        self.connect_timeout_s = connect_timeout_s
        self.ping_tries = ping_tries
        self.runner = runner or _default_runner
        self._ran = False
        self._result: dict | None = None

    def _run_once(self) -> dict | None:
        if self._ran:
            return self._result
        self._ran = True

        argv = [
            str(self.python_executable),
            str(self.mcu_probe_script),
            self.port,
            "--ping-tries", str(self.ping_tries),
            "--connect-timeout", str(self.connect_timeout_s),
        ]
        subprocess_timeout = self.connect_timeout_s + self.ping_tries * 2.0 + 10.0

        try:
            completed = self.runner(argv, subprocess_timeout)
        except Exception:
            self._result = None
            return None

        try:
            self._result = json.loads(completed.stdout)
        except (ValueError, TypeError):
            self._result = None
        return self._result

    def ping(self) -> float | None:
        result = self._run_once()
        if result is None or not result.get("ok"):
            return None
        return result.get("ping_ms")

    def version(self) -> dict | None:
        result = self._run_once()
        if result is None or not result.get("ok"):
            return None
        return result.get("version")
