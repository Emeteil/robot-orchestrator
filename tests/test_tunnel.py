import asyncio
import hashlib
import io
import json
import tarfile
import time
from unittest.mock import AsyncMock, Mock, patch

import pytest
import httpx

from robot_orchestrator.config import Settings, ServiceConfig
from robot_orchestrator.supervisor.supervisor import Supervisor
from robot_orchestrator.supervisor.service import ServiceState
from robot_orchestrator.network import tunnel
from robot_orchestrator.paths import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")


@pytest.fixture
def secrets():
    return {"FRP_SERVER_ADDR": "vps.example.org", "FRP_TOKEN": "private-token", "FRP_CA_CERT": "certificate"}


@pytest.fixture
def fake_status():
    with patch.object(tunnel.TunnelService, "check_status", new_callable=AsyncMock):
        yield


def test_mappings_and_tls(tmp_path, secrets):
    config = tunnel.client_config(Settings(), secrets, tmp_path, "192.168.0.33")
    assert [(p["localIP"], p["localPort"], p["remotePort"]) for p in config["proxies"]] == [
        ("127.0.0.1", 80, 42863), ("192.168.0.33", 8080, 45917), ("127.0.0.1", 22, 49231),
    ]
    assert config["loginFailExit"] is False
    assert config["transport"]["tls"]["trustedCaFile"] == str(tmp_path / "ca.pem")
    assert config["transport"]["tls"]["serverName"] == "vps.example.org"
    assert "private-token" not in json.dumps(config)


def test_simulation_opt_in(tmp_path, secrets):
    settings = Settings(tunnel={"simulation_enabled": True})
    config = tunnel.client_config(settings, secrets, tmp_path, "192.168.0.33")
    assert config["proxies"][-1]["localPort"] == 5470


@pytest.mark.parametrize("overrides", [{"web": {"host": "127.0.0.1"}}, {"tunnel": {"web_port": 38471}}])
def test_unsafe_configuration_rejected(tmp_path, secrets, overrides):
    with pytest.raises(ValueError):
        tunnel.client_config(Settings(**overrides), secrets, tmp_path, "192.168.0.33")


def test_installer_verifies_and_reuses_binary(tmp_path):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        member = tarfile.TarInfo(f"frp_{tunnel.VERSION}_linux_arm64/frpc")
        member.size = 6
        archive.addfile(member, io.BytesIO(b"binary"))
    data = buffer.getvalue()
    with patch.object(tunnel.platform, "system", return_value="Linux"), \
            patch.object(tunnel.platform, "machine", return_value="aarch64"), \
            patch.dict(tunnel.CHECKSUMS, {"arm64": hashlib.sha256(data).hexdigest()}), \
            patch.object(tunnel.urllib.request, "urlopen", return_value=io.BytesIO(data)) as download:
        binary = tunnel.install_client(tmp_path)
        assert binary.read_bytes() == b"binary"
        assert tunnel.install_client(tmp_path) == binary
        assert download.call_count == 1
        binary.write_bytes(b"corrupt")
        download.return_value = io.BytesIO(data)
        assert tunnel.install_client(tmp_path).read_bytes() == b"binary"
        assert download.call_count == 2


def test_installer_rejects_wrong_checksum(tmp_path):
    with patch.object(tunnel.platform, "system", return_value="Linux"), \
            patch.object(tunnel.platform, "machine", return_value="aarch64"), \
            patch.object(tunnel.urllib.request, "urlopen", return_value=io.BytesIO(b"bad")):
        with pytest.raises(ValueError, match="checksum"):
            tunnel.install_client(tmp_path)
    assert not list(tmp_path.glob("frpc-*"))


async def test_missing_secrets_do_not_download(paths):
    service = tunnel.TunnelService(Settings(), paths)
    with patch.object(tunnel, "install_client") as install, \
            patch.object(tunnel.asyncio, "sleep", side_effect=asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError):
            await service.run()
    install.assert_not_called()


async def test_restart_and_stop(paths, secrets, fake_status):
    service = tunnel.TunnelService(Settings(), paths)
    process = AsyncMock()
    process.returncode = 1
    process.stdout.readline.return_value = b""
    process.terminate = lambda: None
    iterations = 0

    async def sleep(_):
        nonlocal iterations
        iterations += 1
        if iterations == 2:
            raise asyncio.CancelledError

    with patch.object(tunnel.store, "load_secrets", return_value={"secrets": secrets}), \
            patch.object(tunnel.store, "all_secret_values", return_value=[]), \
            patch.object(tunnel, "route_address", return_value="192.168.0.33"), \
            patch.object(tunnel, "install_client", return_value=paths.state_dir / "frpc"), \
            patch.object(tunnel.asyncio, "create_subprocess_exec", return_value=process) as spawn, \
            patch.object(tunnel.asyncio, "sleep", side_effect=sleep):
        with pytest.raises(asyncio.CancelledError):
            await service.run()
    assert spawn.call_count == 2
    assert service.process is None
    assert (paths.run_dir / "frp" / "token").read_text() == "private-token"


async def test_stop_kills_unresponsive_process(paths):
    service = tunnel.TunnelService(Settings(), paths)
    process = AsyncMock()
    process.returncode = None
    process.terminate = Mock()
    process.kill = Mock()
    process.wait.side_effect = [asyncio.TimeoutError, 0]
    service.process = process
    await service.stop()
    process.terminate.assert_called_once()
    process.kill.assert_called_once()


async def test_install_failure_retried_and_secret_rotation(paths, secrets, fake_status):
    service = tunnel.TunnelService(Settings(), paths)
    process = AsyncMock()
    process.returncode = None
    process.stdout.readline.return_value = b""
    process.terminate = Mock()
    iterations = 0

    async def sleep(_):
        nonlocal iterations
        iterations += 1
        if iterations == 2:
            secrets["FRP_TOKEN"] = "rotated-token"
        if iterations == 3:
            raise asyncio.CancelledError

    with patch.object(tunnel.store, "load_secrets", return_value={"secrets": secrets}), \
            patch.object(tunnel.store, "all_secret_values", return_value=[]), \
            patch.object(tunnel, "route_address", return_value="192.168.0.33"), \
            patch.object(tunnel, "install_client", side_effect=[OSError(), paths.state_dir / "frpc", paths.state_dir / "frpc"]), \
            patch.object(tunnel.asyncio, "create_subprocess_exec", return_value=process) as spawn, \
            patch.object(tunnel.asyncio, "sleep", side_effect=sleep):
        with pytest.raises(asyncio.CancelledError):
            await service.run()
    assert spawn.call_count == 2
    assert process.terminate.call_count == 2
    assert (paths.run_dir / "frp" / "token").read_text() == "rotated-token"


def test_route_rejects_loopback():
    with patch.object(tunnel.socket, "getaddrinfo", return_value=[
        (tunnel.socket.AF_INET, tunnel.socket.SOCK_DGRAM, 0, "", ("127.0.0.1", 38471)),
    ]):
        with pytest.raises(ValueError, match="non-loopback"):
            tunnel.route_address("localhost", 38471)


async def test_stop_handles_process_exiting_before_signal(paths):
    service = tunnel.TunnelService(Settings(), paths)
    process = AsyncMock()
    process.returncode = None
    process.terminate = Mock(side_effect=ProcessLookupError)
    service.process = process
    await service.stop()
    process.wait.assert_awaited_once()
    assert service.process is None


@pytest.mark.parametrize("statuses,ready", [
    (["running", "running", "running"], True), (["running", "running"], False),
    (["running", "start error", "running"], False), ([], False),
])
async def test_readiness_requires_all_proxies(paths, statuses, ready):
    service = tunnel.TunnelService(Settings(), paths)
    service.process = Mock(returncode=None)
    names = ["izbushka-web-core", "izbushka-orchestrator", "izbushka-ssh"]
    service.expected_proxies = set(names)

    def handler(request):
        assert request.url.host == "127.0.0.1"
        assert request.headers["authorization"].startswith("Basic ")
        entries = [{"name": name, "status": status, "err": ""} for name, status in zip(names, statuses)]
        return httpx.Response(200, json={"tcp": entries})

    client_type = httpx.AsyncClient
    with patch.object(tunnel.httpx, "AsyncClient", side_effect=lambda **kwargs: client_type(
        transport=httpx.MockTransport(handler), **kwargs,
    )):
        await service.check_status()
    assert service.snapshot()["ready"] is ready
    if ready:
        service._checked_at = time.monotonic() - 100
        assert service.snapshot()["ready"] is False


@pytest.mark.parametrize("data,status", [({}, 200), ({"tcp": []}, 200), ({"tcp": "bad"}, 200), ({}, 401)])
async def test_invalid_status_does_not_pass_readiness(paths, data, status):
    service = tunnel.TunnelService(Settings(), paths)
    service.expected_proxies = {"izbushka-ssh"}
    service.process = Mock(returncode=None)
    service._status("connected", "old")
    client_type = httpx.AsyncClient
    with patch.object(tunnel.httpx, "AsyncClient", side_effect=lambda **kwargs: client_type(
        transport=httpx.MockTransport(lambda _: httpx.Response(status, json=data)), **kwargs,
    )):
        await service.check_status()
    assert service.snapshot()["ready"] is False


async def test_managed_tunnel_supports_supervisor_controls(paths):
    supervisor = Supervisor(paths.log_dir)

    async def run(service):
        service.process = Mock(returncode=None)
        service._checked_at = time.monotonic()
        service._status("connected", "готов")
        try:
            await asyncio.Event().wait()
        finally:
            service.process = None
            service._status("stopped", "остановлен")

    config = ServiceConfig(name="tunnel")
    with patch.object(tunnel.TunnelService, "run", run), \
            patch.object(tunnel.store, "missing_required_secrets", return_value=[]):
        service = supervisor.register(config, paths.run_dir, {}, factory=lambda *args: tunnel.ManagedTunnelService(
            *args, Settings(), paths,
        ))
        try:
            assert await supervisor.start_ordered(["tunnel"], {}) == {"tunnel": True}
            assert service.state == ServiceState.READY
            assert await supervisor.restart("tunnel") is True
            assert service.state == ServiceState.READY
            service.tunnel._status("connecting", "связь потеряна")
            assert service.state == ServiceState.STARTING
        finally:
            await supervisor.stop("tunnel")
        assert service.state == ServiceState.STOPPED
        assert service.snapshot()["ready"] is False


async def test_missing_secrets_fail_readiness_immediately(paths):
    service = tunnel.TunnelService(Settings(), paths)
    result = await asyncio.wait_for(service.wait_ready(), timeout=1)
    assert result.ready is False
    assert "FRP_SERVER_ADDR" in result.detail
