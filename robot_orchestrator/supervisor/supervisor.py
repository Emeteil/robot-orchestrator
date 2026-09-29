import asyncio
import time
from pathlib import Path

from robot_orchestrator.bootlog import BOOT_LOG, forward_service_line
from robot_orchestrator.config import ServiceConfig
from robot_orchestrator.supervisor.backoff import BackoffState
from robot_orchestrator.supervisor.devices import DeviceLeases
from robot_orchestrator.supervisor.logsink import LogSink
from robot_orchestrator.supervisor.readiness import wait_until_ready
from robot_orchestrator.supervisor.service import ManagedService, ServiceState


class DependencyCycleError(Exception):
    pass


def topological_order(services: list[ServiceConfig]) -> list[str]:
    remaining = {s.name: [d.service for d in s.depends_on] for s in services}
    ordered: list[str] = []
    while remaining:
        ready = [name for name, deps in remaining.items() if all(d in ordered for d in deps)]
        if not ready:
            raise DependencyCycleError(f"unresolvable service dependencies among {list(remaining)}")
        for name in ready:
            ordered.append(name)
            del remaining[name]
    return ordered


class Supervisor:
    def __init__(self, log_dir: Path):
        self.log_dir = log_dir
        self.services: dict[str, ManagedService] = {}
        self.backoffs: dict[str, BackoffState] = {}
        self.log_sinks: dict[str, LogSink] = {}
        self.devices = DeviceLeases()
        self._lifecycle_tasks: dict[str, asyncio.Task] = {}
        self._stop_requested: set[str] = set()

    def register(self, config: ServiceConfig, cwd: Path, env: dict[str, str]) -> ManagedService:
        sink = LogSink(self.log_dir / "services" / f"{config.name}.log")
        sink.subscribe(lambda line, name=config.name: forward_service_line(name, line))
        self.log_sinks[config.name] = sink
        service = ManagedService(config, cwd, env, sink)
        self.services[config.name] = service
        self.backoffs[config.name] = BackoffState(policy=config.restart)
        return service

    def is_enabled(self, name: str, capabilities: dict[str, bool]) -> bool:
        expr = self.services[name].config.enabled_when
        if not expr:
            return True
        if expr.startswith("capabilities."):
            key = expr.split(".", 1)[1]
            return capabilities.get(key, False)
        return True

    async def start_ordered(self, order: list[str], capabilities: dict[str, bool]) -> dict[str, bool]:
        ready_map: dict[str, bool] = {}
        for name in order:
            if not self.is_enabled(name, capabilities):
                self.services[name].state = ServiceState.SKIPPED
                ready_map[name] = True
                continue
            deps_ok = all(
                ready_map.get(dep.service, False)
                for dep in self.services[name].config.depends_on
                if dep.condition == "ready"
            )
            if not deps_ok:
                ready_map[name] = False
                continue
            ready_map[name] = await self._start_one(name)
        return ready_map

    async def _start_one(self, name: str) -> bool:
        service = self.services[name]
        for device in service.config.holds_devices:
            self.devices.acquire(device, name)
        self._stop_requested.discard(name)
        self.backoffs[name].note_start()
        BOOT_LOG.emit("supervisor", f"▶ запускаю {name}: {' '.join(service.config.cmd)}"[:260], "cmd")
        started = time.monotonic()
        await service.start()
        self._lifecycle_tasks[name] = asyncio.create_task(self._lifecycle(name))
        result = await wait_until_ready(service.config.ready)
        if result.ready:
            service.state = ServiceState.READY
            BOOT_LOG.emit("supervisor", f"✔ {name} готов за {time.monotonic() - started:.1f}s", "ok")
        else:
            BOOT_LOG.emit("supervisor", f"✖ {name} не прошёл проверку готовности", "error")
        return result.ready

    async def _lifecycle(self, name: str) -> None:
        service = self.services[name]
        backoff = self.backoffs[name]
        await service.wait_exit()
        for device in service.config.holds_devices:
            self.devices.release(device, name)
        if name in self._stop_requested:
            service.state = ServiceState.STOPPED
            return
        backoff.on_exit()
        if backoff.has_exceeded_crash_loop_limit():
            backoff.enter_failed()
            service.state = ServiceState.FAILED
            return
        service.state = ServiceState.BACKOFF
        await asyncio.sleep(backoff.current_delay)
        if name in self._stop_requested:
            service.state = ServiceState.STOPPED
            return
        await self._start_one(name)

    async def stop(self, name: str) -> None:
        self._stop_requested.add(name)
        service = self.services[name]
        await service.stop()
        task = self._lifecycle_tasks.get(name)
        if task is not None:
            await task
        for device in service.config.holds_devices:
            self.devices.release(device, name)

    async def stop_ordered(self, order: list[str]) -> None:
        for name in reversed(order):
            service = self.services.get(name)
            if service is not None and service.state != ServiceState.STOPPED:
                await self.stop(name)

    async def restart(self, name: str) -> bool:
        self.backoffs[name].clear_failed()
        if self.services[name].state not in (ServiceState.STOPPED, ServiceState.FAILED, ServiceState.SKIPPED):
            await self.stop(name)
        return await self._start_one(name)
