import os
import socket
import sys
from pathlib import Path

import pytest

from robot_orchestrator.config import (
    DependsOnConfig,
    ReadyProbeConfig,
    RestartPolicyConfig,
    ServiceConfig,
    StopConfig,
)
from robot_orchestrator.supervisor.service import ServiceState
from robot_orchestrator.supervisor.supervisor import Supervisor

BASE_ENV = {**os.environ, "PYTHONUNBUFFERED": "1"}
FIXTURES = Path(__file__).parent / "fixtures"


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def fast_restart_policy(**overrides) -> RestartPolicyConfig:
    defaults = dict(
        backoff_initial_s=0.05,
        backoff_max_s=0.2,
        stable_after_s=5.0,
        max_in_window=3,
        window_s=2.0,
        failed_cooldown_s=60.0,
    )
    defaults.update(overrides)
    return RestartPolicyConfig(**defaults)


def tcp_service(name: str, port: int, delay: float = 0.0, **overrides) -> ServiceConfig:
    defaults = dict(
        name=name,
        cmd=[sys.executable, str(FIXTURES / "tcp_listener.py"), str(port), str(delay)],
        ready=ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{port}", timeout_s=5.0),
        stop=StopConfig(signal="SIGINT", timeout_s=3.0),
        restart=fast_restart_policy(),
    )
    defaults.update(overrides)
    return ServiceConfig(**defaults)


@pytest.fixture
def supervisor(tmp_path):
    return Supervisor(log_dir=tmp_path / "logs")


async def test_service_starts_and_is_marked_ready(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("demo", port)
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    ready_map = await supervisor.start_ordered(["demo"], capabilities={})

    assert ready_map["demo"] is True
    assert supervisor.services["demo"].state == ServiceState.READY

    await supervisor.stop_ordered(["demo"])
    assert supervisor.services["demo"].state == ServiceState.STOPPED


async def test_service_logs_are_captured(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("demo", port)
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    await supervisor.start_ordered(["demo"], capabilities={})
    await supervisor.stop_ordered(["demo"])

    lines = supervisor.log_sinks["demo"].tail()
    joined = "\n".join(lines)
    assert "starting" in joined
    assert "listening" in joined


async def test_ready_probe_times_out_if_service_never_listens(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("demo", port, delay=10.0, ready=ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{port}", timeout_s=0.5))
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    ready_map = await supervisor.start_ordered(["demo"], capabilities={})

    assert ready_map["demo"] is False
    await supervisor.stop_ordered(["demo"])


async def test_depends_on_blocks_dependent_when_dependency_not_ready(supervisor, tmp_path):
    port_a = free_port()
    port_b = free_port()
    a = tcp_service("a", port_a, delay=10.0, ready=ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{port_a}", timeout_s=0.3))
    b = tcp_service("b", port_b, depends_on=[DependsOnConfig(service="a", condition="ready")])
    supervisor.register(a, cwd=tmp_path, env=BASE_ENV)
    supervisor.register(b, cwd=tmp_path, env=BASE_ENV)

    ready_map = await supervisor.start_ordered(["a", "b"], capabilities={})

    assert ready_map["a"] is False
    assert ready_map["b"] is False
    assert supervisor.services["b"].process is None

    await supervisor.stop_ordered(["a", "b"])


async def test_enabled_when_false_skips_service_without_spawning(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("voice", port, enabled_when="capabilities.voice_interface")
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    ready_map = await supervisor.start_ordered(["voice"], capabilities={"voice_interface": False})

    assert ready_map["voice"] is True
    assert supervisor.services["voice"].state == ServiceState.SKIPPED
    assert supervisor.services["voice"].process is None


async def test_enabled_when_true_starts_service_normally(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("voice", port, enabled_when="capabilities.voice_interface")
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    ready_map = await supervisor.start_ordered(["voice"], capabilities={"voice_interface": True})

    assert ready_map["voice"] is True
    assert supervisor.services["voice"].state == ServiceState.READY
    await supervisor.stop_ordered(["voice"])


async def test_device_lease_held_while_running_and_released_after_stop(supervisor, tmp_path):
    port = free_port()
    config = tcp_service("demo", port, holds_devices=["camera"])
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    await supervisor.start_ordered(["demo"], capabilities={})
    assert supervisor.devices.holder_of("camera") == "demo"

    await supervisor.stop_ordered(["demo"])
    assert supervisor.devices.holder_of("camera") is None


async def test_crash_loop_reaches_failed_and_stops_auto_restarting(supervisor, tmp_path):
    config = ServiceConfig(
        name="crashy",
        cmd=[sys.executable, str(FIXTURES / "exit_immediately.py"), "1"],
        ready=ReadyProbeConfig(type="none"),
        stop=StopConfig(timeout_s=1.0),
        restart=fast_restart_policy(max_in_window=3, window_s=5.0, backoff_initial_s=0.02, backoff_max_s=0.05),
    )
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)

    await supervisor.start_ordered(["crashy"], capabilities={})

    import asyncio

    for _ in range(100):
        if supervisor.services["crashy"].state == ServiceState.FAILED:
            break
        await asyncio.sleep(0.05)

    assert supervisor.services["crashy"].state == ServiceState.FAILED
    assert supervisor.backoffs["crashy"].is_failed() is True


async def test_manual_restart_clears_failed_state(supervisor, tmp_path):
    import asyncio

    config = ServiceConfig(
        name="crashy",
        cmd=[sys.executable, str(FIXTURES / "exit_immediately.py"), "1"],
        ready=ReadyProbeConfig(type="none"),
        stop=StopConfig(timeout_s=1.0),
        restart=fast_restart_policy(max_in_window=2, window_s=5.0, backoff_initial_s=0.02, backoff_max_s=0.05),
    )
    supervisor.register(config, cwd=tmp_path, env=BASE_ENV)
    await supervisor.start_ordered(["crashy"], capabilities={})

    for _ in range(100):
        if supervisor.services["crashy"].state == ServiceState.FAILED:
            break
        await asyncio.sleep(0.05)
    assert supervisor.services["crashy"].state == ServiceState.FAILED

    port = free_port()
    supervisor.services["crashy"].config.cmd = [sys.executable, str(FIXTURES / "tcp_listener.py"), str(port), "0"]
    supervisor.services["crashy"].config.ready = ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{port}", timeout_s=3.0)

    ready = await supervisor.restart("crashy")

    assert ready is True
    assert supervisor.services["crashy"].state == ServiceState.READY
    await supervisor.stop_ordered(["crashy"])
