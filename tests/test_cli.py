import json
from pathlib import Path

import pytest

from robot_orchestrator import cli
from robot_orchestrator.config import FirmwareCiConfig, FirmwareTargetConfig, FirmwareVerifyConfig
from robot_orchestrator.state.store import Database


def _args(tmp_path: Path, **extra) -> object:
    return type("Args", (), {
        "config": None,
        "local_config": None,
        "state_dir": str(tmp_path),
        **extra,
    })()


def test_build_parser_dispatches_each_subcommand():
    parser = cli.build_parser()
    assert parser.parse_args(["doctor"]).func is cli.cmd_doctor
    assert parser.parse_args(["run"]).func is cli.cmd_run
    assert parser.parse_args(["status"]).func is cli.cmd_status
    assert parser.parse_args(["gpio-test"]).func is cli.cmd_gpio_test
    assert parser.parse_args(["secrets", "import", "x.yml"]).func is cli.cmd_secrets_import


def test_doctor_runs_without_hardware_and_does_not_raise(tmp_path):
    assert cli.cmd_doctor(_args(tmp_path)) == 0


def test_status_reports_empty_state_before_any_boot(tmp_path, capsys):
    assert cli.cmd_status(_args(tmp_path)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["last_boot"] is None
    assert payload["repos"]["web-core"] is None


def test_secrets_import_then_doctor_sees_the_secret(tmp_path, capsys):
    secrets_file = tmp_path / "secrets.yml"
    secrets_file.write_text(
        "v: 1\nissued_at: '2026-09-28T00:00:00Z'\nsecrets:\n  GEMINI_API_KEY: from-file\n",
        encoding="utf-8",
    )
    assert cli.cmd_secrets_import(_args(tmp_path, file=str(secrets_file))) == 0

    cli.cmd_doctor(_args(tmp_path))
    out = capsys.readouterr().out
    assert "secret GEMINI_API_KEY present: True" in out


def test_mcu_client_factory_falls_back_to_null_client_without_release(tmp_path):
    settings = cli._load_settings(_args(tmp_path))
    paths = cli._build_paths(settings, _args(tmp_path))
    db = Database(paths.state_db)
    target = FirmwareTargetConfig(
        name="stm32",
        repo="com-link-RT",
        pio_env="black_f407ve",
        ci=FirmwareCiConfig(owner="x", repo="y", workflow_file="build.yml", artifact_name="z"),
        verify=FirmwareVerifyConfig(client_repo="web-core", client_subdir="com_link_rt"),
    )
    factory = cli._make_mcu_client_factory(paths, db, target, lambda: "/dev/ttyACM0")

    client = factory()

    assert isinstance(client, cli.NullMcuClient)
    assert client.ping() is None
    assert client.version() is None
    db.close()


def test_mcu_client_factory_returns_null_client_when_port_unresolved(tmp_path):
    settings = cli._load_settings(_args(tmp_path))
    paths = cli._build_paths(settings, _args(tmp_path))
    db = Database(paths.state_db)
    target = FirmwareTargetConfig(
        name="stm32",
        repo="com-link-RT",
        pio_env="black_f407ve",
        ci=FirmwareCiConfig(owner="x", repo="y", workflow_file="build.yml", artifact_name="z"),
        verify=FirmwareVerifyConfig(client_repo="web-core", client_subdir="com_link_rt"),
    )
    factory = cli._make_mcu_client_factory(paths, db, target, lambda: None)

    assert isinstance(factory(), cli.NullMcuClient)
    db.close()


def test_qr_scanner_returns_none_when_camera_device_cannot_be_resolved(tmp_path):
    settings = cli._load_settings(_args(tmp_path))
    paths = cli._build_paths(settings, _args(tmp_path))
    hal = cli.build_hal(settings, paths)

    scan_qr = cli._build_qr_scanner(settings, paths, hal)

    assert scan_qr() is None


@pytest.mark.skipif(__import__("os").name == "nt", reason="platformio venv build touches network/pip on Windows CI dev box")
def test_ensure_platformio_venv_is_idempotent_marker_check(tmp_path):
    paths = cli._build_paths(cli._load_settings(_args(tmp_path)), _args(tmp_path))
    marker = paths.platformio_venv / ".complete"
    marker.parent.mkdir(parents=True, exist_ok=True)
    marker.write_text("")

    result = cli._ensure_platformio_venv(paths)

    assert result == paths.platformio_venv
