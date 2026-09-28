from dataclasses import dataclass
from typing import Callable

from robot_orchestrator.boot.fsm import BootProgress, BootResult
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
