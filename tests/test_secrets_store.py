import os
import stat
from pathlib import Path

from robot_orchestrator.paths import Paths
from robot_orchestrator.secrets import store
from robot_orchestrator.secrets.protocol import SecretsPayload, WifiCredential


def _make_paths(tmp_path: Path) -> Paths:
    return Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")


def test_write_secrets_then_load_secrets_roundtrips(tmp_path: Path):
    paths = _make_paths(tmp_path)
    payload = SecretsPayload(
        v=1,
        issued_at="2026-09-27T12:00:00Z",
        secrets={"GEMINI_API_KEY": "abc", "GITHUB_PAT": "def"},
        wifi=[WifiCredential(ssid="home", psk="hunter2")],
        config={"gpio.chip": "/dev/gpiochip1", "gpio.line": 11},
    )

    store.write_secrets(paths, payload)
    loaded = store.load_secrets(paths)

    assert loaded["secrets"] == {"GEMINI_API_KEY": "abc", "GITHUB_PAT": "def"}
    assert loaded["wifi"] == [{"ssid": "home", "psk": "hunter2"}]
    assert loaded["config"] == {"gpio.chip": "/dev/gpiochip1", "gpio.line": 11}


def test_load_secrets_returns_empty_dict_when_missing(tmp_path: Path):
    paths = _make_paths(tmp_path)
    assert store.load_secrets(paths) == {}


def test_write_secrets_permissions(tmp_path: Path):
    paths = _make_paths(tmp_path)
    payload = SecretsPayload(v=1, issued_at="2026-09-27T12:00:00Z", secrets={"GEMINI_API_KEY": "abc"})
    store.write_secrets(paths, payload)

    if os.name != "nt":
        file_mode = stat.S_IMODE(paths.secrets_file.stat().st_mode)
        dir_mode = stat.S_IMODE(paths.secrets_dir.stat().st_mode)
        assert file_mode == 0o600
        assert dir_mode == 0o700


def test_ensure_generated_secrets_creates_three_keys(tmp_path: Path):
    paths = _make_paths(tmp_path)
    generated = store.ensure_generated_secrets(paths)

    assert set(generated.keys()) == {"MASTER_TOKEN", "flask_secret", "ORCH_JWT_SECRET"}
    assert all(generated[key] for key in generated)


def test_ensure_generated_secrets_does_not_rotate_on_second_call(tmp_path: Path):
    paths = _make_paths(tmp_path)
    first = store.ensure_generated_secrets(paths)
    second = store.ensure_generated_secrets(paths)

    assert first["MASTER_TOKEN"] == second["MASTER_TOKEN"]
    assert first["flask_secret"] == second["flask_secret"]
    assert first["ORCH_JWT_SECRET"] == second["ORCH_JWT_SECRET"]


def test_missing_required_secrets_all_missing_when_nothing_ingested(tmp_path: Path):
    paths = _make_paths(tmp_path)
    missing = store.missing_required_secrets(paths, ["GEMINI_API_KEY", "GITHUB_PAT"])
    assert set(missing) == {"GEMINI_API_KEY", "GITHUB_PAT"}


def test_missing_required_secrets_partial(tmp_path: Path):
    paths = _make_paths(tmp_path)
    payload = SecretsPayload(v=1, issued_at="2026-09-27T12:00:00Z", secrets={"GEMINI_API_KEY": "abc"})
    store.write_secrets(paths, payload)

    missing = store.missing_required_secrets(paths, ["GEMINI_API_KEY", "GITHUB_PAT"])
    assert missing == ["GITHUB_PAT"]


def test_missing_required_secrets_empty_when_all_present_across_both_files(tmp_path: Path):
    paths = _make_paths(tmp_path)
    payload = SecretsPayload(v=1, issued_at="2026-09-27T12:00:00Z", secrets={"GEMINI_API_KEY": "abc"})
    store.write_secrets(paths, payload)
    store.ensure_generated_secrets(paths)

    missing = store.missing_required_secrets(paths, ["GEMINI_API_KEY", "MASTER_TOKEN"])
    assert missing == []
