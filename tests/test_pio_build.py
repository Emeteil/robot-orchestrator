import subprocess
from pathlib import Path

import pytest

from robot_orchestrator.firmware.pio_build import PioBuildError, PioFirmwareBuilder


def _write_outputs(build_dir: Path, pio_env: str) -> None:
    out = build_dir / pio_env
    out.mkdir(parents=True, exist_ok=True)
    (out / "firmware.bin").write_bytes(b"bin")
    (out / "firmware.elf").write_bytes(b"elf")


def test_build_passes_expected_env_and_returns_output_dir(tmp_path: Path):
    captured = {}

    def fake_runner(argv, env, cwd):
        captured["argv"] = argv
        captured["env"] = env
        captured["cwd"] = cwd
        _write_outputs(Path(env["PLATFORMIO_BUILD_DIR"]), "black_f407ve")
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    pio_core_dir = tmp_path / "pio"
    release_dir = tmp_path / "release"
    release_dir.mkdir()
    build_dir = tmp_path / "staging" / "build"

    builder = PioFirmwareBuilder(pio_core_dir, "python3", runner=fake_runner)
    result = builder.build(release_dir, "black_f407ve", build_dir)

    assert result.output_dir == build_dir / "black_f407ve"
    assert result.started_at <= result.finished_at
    assert (result.output_dir / "firmware.bin").exists()
    assert (result.output_dir / "firmware.elf").exists()

    assert captured["argv"] == ["python3", "-m", "platformio", "run", "-d", str(release_dir), "-e", "black_f407ve"]
    assert captured["cwd"] == release_dir
    assert captured["env"]["TZ"] == "UTC"
    assert captured["env"]["PLATFORMIO_SETTING_ENABLE_TELEMETRY"] == "No"
    assert captured["env"]["PLATFORMIO_CORE_DIR"] == str(pio_core_dir)
    assert captured["env"]["PLATFORMIO_BUILD_DIR"] == str(build_dir)
    assert "PATH" in captured["env"] or len(captured["env"]) > 4


def test_build_raises_with_stderr_on_nonzero_exit(tmp_path: Path):
    def fake_runner(argv, env, cwd):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="compile error: missing header")

    builder = PioFirmwareBuilder(tmp_path / "pio", "python3", runner=fake_runner)

    with pytest.raises(PioBuildError, match="missing header"):
        builder.build(tmp_path / "release", "black_f407ve", tmp_path / "build")


def test_build_raises_when_output_files_missing_despite_zero_exit(tmp_path: Path):
    def fake_runner(argv, env, cwd):
        return subprocess.CompletedProcess(argv, 0, stdout="ok", stderr="")

    builder = PioFirmwareBuilder(tmp_path / "pio", "python3", runner=fake_runner)

    with pytest.raises(PioBuildError, match="firmware.bin"):
        builder.build(tmp_path / "release", "black_f407ve", tmp_path / "build")
