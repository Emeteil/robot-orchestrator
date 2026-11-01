import asyncio
import hashlib
import io
import json
import platform
import socket
import tarfile
import urllib.request
import secrets as stdlib_secrets
import time

import httpx

from robot_orchestrator.bootlog import BOOT_LOG
from robot_orchestrator.secrets import store
from robot_orchestrator.wal.atomic import atomic_write
from robot_orchestrator.supervisor.readiness import ReadinessResult
from robot_orchestrator.supervisor.service import ServiceState

VERSION = "0.71.1"
CHECKSUMS = {
    "arm64": "8edf590b0c396fa42f9c147121a76af83cfc2002b08f5aef0ea1008c43482019",
    "amd64": "1d9875210972a5d1a62b2dc4c48df45ab0af05d3ca1b5e494a014428259df803",
}
SECRET_KEYS = ("FRP_SERVER_ADDR", "FRP_TOKEN", "FRP_CA_CERT")


def install_client(directory):
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64", "amd64": "amd64"}.get(platform.machine().lower())
    if platform.system() != "Linux" or arch not in CHECKSUMS:
        raise ValueError("frp requires Linux arm64 or amd64")
    directory.mkdir(parents=True, exist_ok=True)
    binary = directory / f"frpc-{VERSION}-{arch}"
    digest_file = binary.with_suffix(".sha256")
    if binary.exists() and digest_file.exists():
        if hashlib.sha256(binary.read_bytes()).hexdigest() == digest_file.read_text():
            return binary
    name = f"frp_{VERSION}_linux_{arch}"
    url = f"https://github.com/fatedier/frp/releases/download/v{VERSION}/{name}.tar.gz"
    with urllib.request.urlopen(url, timeout=30) as response:
        archive = response.read(64 * 1024 * 1024 + 1)
    if hashlib.sha256(archive).hexdigest() != CHECKSUMS[arch]:
        raise ValueError("frp archive checksum mismatch")
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        member = tar.getmember(f"{name}/frpc")
        if not member.isfile() or member.size > 64 * 1024 * 1024:
            raise ValueError("invalid frpc archive member")
        data = tar.extractfile(member).read()
    atomic_write(binary, data, mode=0o700)
    atomic_write(digest_file, hashlib.sha256(data).hexdigest().encode())
    return binary


def route_address(host, port):
    for family, kind, proto, _, address in socket.getaddrinfo(host, port, family=socket.AF_INET, type=socket.SOCK_DGRAM):
        with socket.socket(family, kind, proto) as sock:
            sock.connect(address)
            local = sock.getsockname()[0]
            if family == socket.AF_INET and not local.startswith("127."):
                return local
    raise ValueError("no non-loopback IPv4 route to VPS")


def client_config(settings, secrets, directory, local_address):
    cfg = settings.tunnel
    ports = [cfg.server_port, cfg.web_port, cfg.orchestrator_port, cfg.ssh_port]
    if cfg.simulation_enabled:
        ports.append(cfg.simulation_port)
    if len(set(ports)) != len(ports):
        raise ValueError("frp ports must be distinct")
    if settings.web.host not in ("0.0.0.0", local_address):
        raise ValueError("orchestrator must listen on its non-loopback IPv4 address")
    proxies = [
        ("web-core", "127.0.0.1", 80, cfg.web_port),
        ("orchestrator", local_address, settings.web.port, cfg.orchestrator_port),
        ("ssh", "127.0.0.1", 22, cfg.ssh_port),
    ]
    if cfg.simulation_enabled:
        proxies.append(("simulation", "127.0.0.1", 5470, cfg.simulation_port))
    return {
        "serverAddr": secrets["FRP_SERVER_ADDR"], "serverPort": cfg.server_port,
        "loginFailExit": False,
        "auth": {"method": "token", "tokenSource": {"type": "file", "file": {"path": str(directory / "token")}},
                 "additionalScopes": ["HeartBeats", "NewWorkConns"]},
        "transport": {"protocol": "tcp", "dialServerTimeout": 10, "dialServerKeepalive": 10,
                      "tcpMuxKeepaliveInterval": 10,
                      "tls": {"enable": True, "trustedCaFile": str(directory / "ca.pem"),
                              "serverName": secrets.get("FRP_TLS_SERVER_NAME") or secrets["FRP_SERVER_ADDR"]}},
        "log": {"to": "console", "level": "info", "disablePrintColor": True},
        "proxies": [{"name": f"izbushka-{name}", "type": "tcp", "localIP": host,
                     "localPort": local, "remotePort": remote} for name, host, local, remote in proxies],
    }


class TunnelService:
    def __init__(self, settings, paths, log_sink=None):
        self.settings = settings
        self.paths = paths
        self.process = None
        self.log_sink = log_sink
        self.status_password = stdlib_secrets.token_urlsafe(32)
        self.expected_proxies = set()
        self._checked_at = 0
        self.status = {"state": "stopped", "ready": False, "detail": "остановлен", "proxies": {}}

    def _log(self, message, level="info"):
        if self.log_sink is not None:
            self.log_sink.write("stderr" if level == "warn" else "stdout", message + "\n")
        else:
            BOOT_LOG.emit("tunnel", message, level)

    def _status(self, state, detail, proxies=None):
        status = {"state": state, "ready": state == "connected", "detail": detail, "proxies": proxies or {}}
        if status != self.status:
            self._log(detail, "warn" if state == "error" else "info")
        self.status = status

    def snapshot(self):
        status = dict(self.status)
        if status["ready"] and (self.process is None or self.process.returncode is not None or
                                time.monotonic() - self._checked_at > self.settings.tunnel.status_interval_s + 5):
            status.update(ready=False, state="connecting", detail="состояние frpc не подтверждено")
        return status

    async def wait_ready(self, timeout=None):
        deadline = time.monotonic() + (self.settings.tunnel.ready_timeout_s if timeout is None else timeout)
        while True:
            status = self.snapshot()
            if status["ready"]:
                return ReadinessResult(True, status["detail"])
            missing = store.missing_required_secrets(self.paths, list(SECRET_KEYS))
            if missing:
                return ReadinessResult(False, "ожидаю секреты: " + ", ".join(missing))
            if time.monotonic() >= deadline:
                return ReadinessResult(False, status["detail"])
            await asyncio.sleep(0.1)

    async def check_status(self):
        try:
            async with httpx.AsyncClient(timeout=2, trust_env=False, auth=("tunnel", self.status_password)) as client:
                response = await client.get(f"http://127.0.0.1:{self.settings.tunnel.status_port}/api/status")
                response.raise_for_status()
                data = response.json()
            entries = data.get("tcp", [])
            proxies = {entry["name"]: ("error" if entry.get("err") else entry.get("status", "unknown"))
                       for entry in entries if isinstance(entry, dict)}
            ready = bool(self.expected_proxies) and all(proxies.get(name) == "running" for name in self.expected_proxies)
            ready = ready and not any(entry.get("err") or entry.get("error") for entry in entries
                                      if isinstance(entry, dict) and entry.get("name") in self.expected_proxies)
            self._checked_at = time.monotonic()
            if ready:
                self._status("connected", "VPS подключён, все пробросы активны", proxies)
            else:
                pending = sorted(name for name in self.expected_proxies if proxies.get(name) != "running")
                self._status("connecting", "не готовы пробросы: " + ", ".join(pending), proxies)
        except (httpx.HTTPError, ValueError, TypeError, AttributeError, KeyError):
            self._status("connecting", "нет подтверждения от локального API frpc")

    async def stop(self, update_status=True):
        if update_status:
            self._status("stopped", "остановлен")
        if self.process is not None:
            if self.process.returncode is None:
                try:
                    self.process.terminate()
                    try:
                        await asyncio.wait_for(self.process.wait(), 5)
                    except asyncio.TimeoutError:
                        self.process.kill()
                        await self.process.wait()
                except ProcessLookupError:
                    await self.process.wait()
            self.process = None

    async def run(self):
        previous = None
        pump = None
        try:
            while self.settings.tunnel.enabled:
                try:
                    secrets = store.load_secrets(self.paths).get("secrets", {}) or {}
                    missing = [key for key in SECRET_KEYS if not secrets.get(key)]
                    if missing:
                        await self.stop(update_status=False)
                        marker = tuple(missing)
                        if previous != marker:
                            self._status("waiting_secrets", "ожидаю секреты: " + ", ".join(missing))
                        previous = marker
                    else:
                        address = await asyncio.to_thread(route_address, secrets["FRP_SERVER_ADDR"], self.settings.tunnel.server_port)
                        directory = self.paths.run_dir / "frp"
                        config = client_config(self.settings, secrets, directory, address)
                        config["webServer"] = {"addr": "127.0.0.1", "port": self.settings.tunnel.status_port,
                                               "user": "tunnel", "password": self.status_password}
                        payload = json.dumps([config, secrets["FRP_TOKEN"], secrets["FRP_CA_CERT"]]).encode()
                        fingerprint = hashlib.sha256(payload).hexdigest()
                        if fingerprint != previous or self.process is None or self.process.returncode is not None:
                            await self.stop(update_status=False)
                            self._status("installing", "подготавливаю клиент frp")
                            if pump is not None:
                                await pump
                            binary = await asyncio.to_thread(install_client, self.paths.state_dir / "tools" / "frp")
                            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                            atomic_write(directory / "token", secrets["FRP_TOKEN"].encode())
                            atomic_write(directory / "ca.pem", secrets["FRP_CA_CERT"].encode())
                            atomic_write(directory / "frpc.json", json.dumps(config).encode())
                            BOOT_LOG.set_known_secrets(store.all_secret_values(self.paths))
                            self.expected_proxies = {proxy["name"] for proxy in config["proxies"]}
                            self.process = await asyncio.create_subprocess_exec(
                                str(binary), "-c", str(directory / "frpc.json"),
                                stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE,
                                stderr=asyncio.subprocess.STDOUT,
                            )
                            pump = asyncio.create_task(self._pump(self.process.stdout))
                            previous = fingerprint
                            self._status("connecting", "клиент frp запущен, подключаюсь к VPS")
                        await self.check_status()
                except Exception as exc:
                    await self.stop(update_status=False)
                    message = f"ошибка туннеля ({type(exc).__name__}); повтор через {self.settings.tunnel.retry_s:g} с"
                    self._status("error", message)
                interval = self.settings.tunnel.status_interval_s if self.process is not None else self.settings.tunnel.retry_s
                await asyncio.sleep(interval)
        finally:
            await self.stop()
            if pump is not None:
                await pump

    async def _pump(self, stream):
        while line := await stream.readline():
            self._log(line.decode(errors="replace").rstrip())


class ManagedTunnelService:
    def __init__(self, config, cwd, env, log_sink, settings, paths):
        self.config = config
        self.cwd = cwd
        self.env = env
        self.log_sink = log_sink
        self.tunnel = TunnelService(settings, paths, log_sink)
        self._state = ServiceState.STOPPED
        self._task = None
        self.last_exit_code = None

    @property
    def process(self):
        return self.tunnel.process

    @property
    def state(self):
        if self._state in (ServiceState.STARTING, ServiceState.READY):
            return ServiceState.READY if self.tunnel.snapshot()["ready"] else ServiceState.STARTING
        return self._state

    @state.setter
    def state(self, value):
        self._state = value

    def snapshot(self):
        return self.tunnel.snapshot()

    async def start(self):
        self.state = ServiceState.STARTING
        self.last_exit_code = None
        self._task = asyncio.create_task(self.tunnel.run())

    async def wait_ready(self):
        return await self.tunnel.wait_ready()

    async def wait_exit(self):
        try:
            await self._task
            self.last_exit_code = 0
        except asyncio.CancelledError:
            self.last_exit_code = 0
        except Exception as exc:
            self.tunnel._log(f"задача туннеля завершилась: {type(exc).__name__}", "warn")
            self.last_exit_code = 1
        return self.last_exit_code

    async def stop(self):
        self.state = ServiceState.STOPPING
        if self._task is not None:
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self.state = ServiceState.STOPPED
