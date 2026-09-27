import subprocess
from pathlib import Path
from typing import Callable

from robot_orchestrator.config import FirmwareFlashConfig
from robot_orchestrator.hal.base import FlashResult

RunnerT = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class OpenOcdFlasher:
    def __init__(
        self,
        openocd_binary: Path,
        scripts_dir: Path,
        config: FirmwareFlashConfig,
        runner: RunnerT | None = None,
    ):
        self.openocd_binary = openocd_binary
        self.scripts_dir = scripts_dir
        self.config = config
        self.runner = runner or _default_runner

    def flash(self, image_path: Path, attempt: int) -> FlashResult:
        argv = [
            str(self.openocd_binary),
            "-s", str(self.scripts_dir),
            "-f", self.config.interface_cfg,
            "-f", self.config.target_cfg,
        ]
        if attempt >= 2:
            argv += ["-c", "adapter speed 1000"]
        argv += ["-c", f"program {image_path} verify reset exit"]

        try:
            result = self.runner(argv, self.config.timeout_s)
        except subprocess.TimeoutExpired:
            return FlashResult(ok=False, detail=f"openocd timed out after {self.config.timeout_s}s")

        if result.returncode == 0:
            return FlashResult(ok=True)
        return FlashResult(ok=False, detail=result.stderr or f"openocd exited with code {result.returncode}")
