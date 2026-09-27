import json
import subprocess
from pathlib import Path

import pytest

from robot_orchestrator.firmware.mcu_client import SubprocessMcuClient


def _client(runner, **kwargs) -> SubprocessMcuClient:
    return SubprocessMcuClient(
        python_executable=Path("web-core-venv-python"),
        mcu_probe_script=Path("mcu_probe.py"),
        port="COM4",
        runner=runner,
        **kwargs,
    )


def _stdout_runner(payload: dict):
    calls = []

    def runner(argv, timeout):
        calls.append((argv, timeout))
        return subprocess.CompletedProcess(argv, 0, stdout=json.dumps(payload), stderr="")

    return runner, calls


def test_success_returns_ping_and_version_from_single_subprocess_call():
    runner, calls = _stdout_runner({"ok": True, "ping_ms": 7.5, "version": {"build_date": "a", "build_time": "b"}})
    client = _client(runner)

    assert client.ping() == 7.5
    assert client.version() == {"build_date": "a", "build_time": "b"}
    assert len(calls) == 1


def test_argv_contains_port_and_flags():
    runner, calls = _stdout_runner({"ok": True, "ping_ms": 1.0, "version": {}})
    client = _client(runner, connect_timeout_s=5.0, ping_tries=2)

    client.ping()

    argv, _ = calls[0]
    assert "COM4" in argv
    assert "web-core-venv-python" in argv[0]
    assert "mcu_probe.py" in argv[1]
    assert "--ping-tries" in argv and "2" in argv
    assert "--connect-timeout" in argv and "5.0" in argv


@pytest.mark.parametrize(
    "reason",
    ["connect_failed", "no_ping_response", "no_version_response"],
)
def test_known_failure_shapes_return_none_for_both_methods(reason):
    runner, _ = _stdout_runner({"ok": False, "reason": reason})
    client = _client(runner)

    assert client.ping() is None
    assert client.version() is None


def test_garbage_stdout_returns_none_without_raising():
    def runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout="not json at all", stderr="")

    client = _client(runner)

    assert client.ping() is None
    assert client.version() is None


def test_runner_raising_returns_none_without_raising():
    def runner(argv, timeout):
        raise RuntimeError("subprocess spawn failed")

    client = _client(runner)

    assert client.ping() is None
    assert client.version() is None


def test_runner_timeout_returns_none_without_raising():
    def runner(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    client = _client(runner)

    assert client.ping() is None
    assert client.version() is None


def test_subprocess_only_invoked_once_across_both_methods_even_on_failure():
    calls = []

    def runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="garbage", stderr="")

    client = _client(runner)

    client.ping()
    client.version()
    client.ping()

    assert len(calls) == 1
