import subprocess
from pathlib import Path

from robot_orchestrator.config import FirmwareFlashConfig
from robot_orchestrator.firmware.openocd import OpenOcdFlasher


def _flasher(runner, config: FirmwareFlashConfig | None = None) -> OpenOcdFlasher:
    return OpenOcdFlasher(
        openocd_binary=Path("openocd"),
        scripts_dir=Path("/scripts"),
        config=config or FirmwareFlashConfig(),
        runner=runner,
    )


def test_first_attempt_has_no_adapter_speed_override(tmp_path: Path):
    captured = {}

    def fake_runner(argv, timeout):
        captured["argv"] = argv
        captured["timeout"] = timeout
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    flasher = _flasher(fake_runner)
    result = flasher.flash(tmp_path / "firmware.elf", attempt=1)

    assert result.ok is True
    assert "adapter speed 1000" not in " ".join(captured["argv"])
    assert captured["timeout"] == FirmwareFlashConfig().timeout_s


def test_second_attempt_prepends_adapter_speed_before_program(tmp_path: Path):
    def fake_runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    captured_argv = []

    def capturing_runner(argv, timeout):
        captured_argv.extend(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    flasher = _flasher(capturing_runner)
    flasher.flash(tmp_path / "firmware.elf", attempt=2)

    speed_index = captured_argv.index("adapter speed 1000")
    program_index = next(i for i, a in enumerate(captured_argv) if a.startswith("program "))
    assert speed_index < program_index


def test_exit_zero_returns_ok(tmp_path: Path):
    def fake_runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    flasher = _flasher(fake_runner)
    result = flasher.flash(tmp_path / "firmware.elf", attempt=1)

    assert result.ok is True


def test_nonzero_exit_returns_failure_with_stderr(tmp_path: Path):
    def fake_runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="target not found")

    flasher = _flasher(fake_runner)
    result = flasher.flash(tmp_path / "firmware.elf", attempt=1)

    assert result.ok is False
    assert "target not found" in result.detail


def test_timeout_is_translated_to_failure_result_not_raised(tmp_path: Path):
    def fake_runner(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    flasher = _flasher(fake_runner)
    result = flasher.flash(tmp_path / "firmware.elf", attempt=1)

    assert result.ok is False
    assert "timed out" in result.detail
