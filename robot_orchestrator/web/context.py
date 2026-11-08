from dataclasses import dataclass, field, replace
import time
from typing import Callable

from robot_orchestrator.boot.fsm import BootProgress, BootResult
from robot_orchestrator.boot.decision import decide_mode
from robot_orchestrator.supervisor.service import ServiceState
from robot_orchestrator.config import Settings
from robot_orchestrator.firmware.workflow import FirmwareWorkflow
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RepoManager
from robot_orchestrator.state.store import Database
from robot_orchestrator.supervisor.supervisor import Supervisor
from robot_orchestrator.wal.journal import Journal


@dataclass
class AdminContext:
    settings: Settings
    paths: Paths
    db: Database
    journal: Journal
    repo_managers: dict[str, RepoManager]
    firmware_workflows: dict[str, FirmwareWorkflow]
    supervisor: Supervisor
    scan_qr: Callable[[], object | None] | None = None
    wifi_manager: object | None = None
    self_update_manager: object | None = None
    request_restart: Callable[[], None] | None = None
    boot_result: BootResult | None = None
    boot_progress: BootProgress | None = None
    operator_url: str | None = None
    voice_state: dict = field(default_factory=dict)
    voice_checked_at: float = 0.0

    def current_boot(self):
        boot = self.boot_result
        facts = boot.facts
        mode = boot.mode
        services_ready = dict(boot.services_ready)
        if facts.tunnel_required:
            service = self.supervisor.services.get("tunnel")
            ready = service is not None and service.state == ServiceState.READY
            facts = replace(facts, tunnel_ok=ready)
            mode = decide_mode(facts)
            services_ready["tunnel"] = ready
        voice_error = None
        voice = self.supervisor.services.get("voice-interface")
        if voice and voice.state == ServiceState.FAILED:
            voice_error = "voice_unavailable"
        if self.voice_state.get("protocol_version", 1) >= 2:
            if time.monotonic() - self.voice_checked_at > 20 or not self.voice_state.get("connected"):
                voice_error = "voice_unavailable"
            elif self.voice_state.get("status") in ("error", "unavailable") or self.voice_state.get("fresh") is False:
                voice_error = "voice_engine_failed"
        if voice_error:
            facts = replace(facts, voice_error=voice_error)
            mode = decide_mode(facts)
        return mode, facts, services_ready
