import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robot_orchestrator.bootlog import logged_run

RunnerT = Callable[[list[str], dict[str, str], Path], subprocess.CompletedProcess]


class PioBuildError(Exception):
    pass


@dataclass
class BuildResult:
    output_dir: Path
    started_at: float
    finished_at: float


def _default_runner(argv: list[str], env: dict[str, str], cwd: Path) -> subprocess.CompletedProcess:
    return logged_run(argv, source="pio", env=env, cwd=cwd)


class PioFirmwareBuilder:
    def __init__(self, pio_core_dir: Path, python_executable: str, runner: RunnerT | None = None):
        self.pio_core_dir = pio_core_dir
        self.python_executable = python_executable
        self.runner = runner or _default_runner

    def build(self, release_dir: Path, pio_env: str, build_dir: Path) -> BuildResult:
        build_dir.mkdir(parents=True, exist_ok=True)
        env = {
            **os.environ,
            "PLATFORMIO_CORE_DIR": str(self.pio_core_dir),
            "PLATFORMIO_BUILD_DIR": str(build_dir),
            "TZ": "UTC",
            "PLATFORMIO_SETTING_ENABLE_TELEMETRY": "No",
        }
        argv = [self.python_executable, "-m", "platformio", "run", "-d", str(release_dir), "-e", pio_env]

        started_at = time.time()
        result = self.runner(argv, env, release_dir)
        finished_at = time.time()

        if result.returncode != 0:
            raise PioBuildError(f"platformio build failed (exit {result.returncode}): {result.stderr}")

        output_dir = build_dir / pio_env
        if not (output_dir / "firmware.bin").exists() or not (output_dir / "firmware.elf").exists():
            raise PioBuildError(
                f"platformio reported success but firmware.bin/firmware.elf missing under {output_dir}"
            )

        return BuildResult(output_dir=output_dir, started_at=started_at, finished_at=finished_at)
