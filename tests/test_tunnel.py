import asyncio
import hashlib
import io
import json
import tarfile
from unittest.mock import AsyncMock, Mock, patch

import pytest

from robot_orchestrator.config import Settings
from robot_orchestrator.network import tunnel
from robot_orchestrator.paths import Paths


@pytest.fixture
def paths(tmp_path):
    return Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")


@pytest.fixture
def secrets():
    return {"FRP_SERVER_ADDR": "vps.example.org", "FRP_TOKEN": "private-token", "FRP_CA_CERT": "certificate"}


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


async def test_restart_and_stop(paths, secrets):
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


async def test_install_failure_retried_and_secret_rotation(paths, secrets):
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
