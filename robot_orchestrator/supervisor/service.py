import asyncio
import enum
import os
import signal
from pathlib import Path

from robot_orchestrator.config import ServiceConfig
from robot_orchestrator.supervisor.logsink import LogSink


class ServiceState(enum.Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    READY = "ready"
    BACKOFF = "backoff"
    FAILED = "failed"
    STOPPING = "stopping"
    SKIPPED = "skipped"


class ManagedService:
    def __init__(self, config: ServiceConfig, cwd: Path, env: dict[str, str], log_sink: LogSink):
        self.config = config
        self.cwd = cwd
        self.env = env
        self.log_sink = log_sink
        self.state = ServiceState.STOPPED
        self.process: asyncio.subprocess.Process | None = None
        self.last_exit_code: int | None = None
        self._pump_tasks: list[asyncio.Task] = []

    async def start(self) -> None:
        self.state = ServiceState.STARTING
        self.last_exit_code = None
        spawn_kwargs = {}
        if os.name != "nt":
            spawn_kwargs["start_new_session"] = True
        self.process = await asyncio.create_subprocess_exec(
            *self.config.cmd,
            cwd=str(self.cwd),
            env=self.env,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            **spawn_kwargs,
        )
        self._pump_tasks = [
            asyncio.create_task(self._pump(self.process.stdout, "stdout")),
            asyncio.create_task(self._pump(self.process.stderr, "stderr")),
        ]

    async def _pump(self, stream: asyncio.StreamReader, name: str) -> None:
        while True:
            line = await stream.readline()
            if not line:
                break
            self.log_sink.write(name, line.decode(errors="replace"))

    async def wait_exit(self) -> int:
        exit_code = await self.process.wait()
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        self.last_exit_code = exit_code
        return exit_code

    async def stop(self) -> None:
        if self.process is None or self.process.returncode is not None:
            self.state = ServiceState.STOPPED
            return
        self.state = ServiceState.STOPPING
        self._send_signal()
        try:
            await asyncio.wait_for(self.process.wait(), timeout=self.config.stop.timeout_s)
        except asyncio.TimeoutError:
            self._kill()
            await self.process.wait()
        await asyncio.gather(*self._pump_tasks, return_exceptions=True)
        self.state = ServiceState.STOPPED

    def _send_signal(self) -> None:
        if os.name == "nt":
            self.process.terminate()
            return
        sig = getattr(signal, self.config.stop.signal, signal.SIGINT)
        try:
            os.killpg(os.getpgid(self.process.pid), sig)
        except ProcessLookupError:
            pass

    def _kill(self) -> None:
        if os.name == "nt":
            self.process.kill()
            return
        try:
            os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
        except ProcessLookupError:
            pass
