import dataclasses
import json
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robot_orchestrator.boot.context import BootFacts
from robot_orchestrator.boot.decision import ModeDecision, decide_mode
from robot_orchestrator.boot.hardware import resolve_camera_device, resolve_mcu_port
from robot_orchestrator.config import Settings
from robot_orchestrator.hal.base import ProbeResult
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RELEASE_MARKER, RepoManager
from robot_orchestrator.secrets import store as secrets_store
from robot_orchestrator.secrets.ingest import ingest_payload
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.supervisor.overlays import write_overlay
from robot_orchestrator.supervisor.render import render
from robot_orchestrator.supervisor.supervisor import Supervisor, topological_order
from robot_orchestrator.wal import recovery
from robot_orchestrator.wal.journal import Journal


@dataclass
class BootResult:
    boot_id: str
    mode: ModeDecision
    facts: BootFacts
    services_ready: dict[str, bool]


class BootSequence:
    def __init__(
        self,
        settings: Settings,
        paths: Paths,
        db: Database,
        journal: Journal,
        hal,
        repo_managers: dict[str, RepoManager],
        firmware_workflows: dict[str, object],
        supervisor: Supervisor,
        scan_qr: Callable[[], SecretsPayload | None] | None = None,
        recovery_committing_handlers: dict | None = None,
        wifi_manager=None,
    ):
        self.settings = settings
        self.paths = paths
        self.db = db
        self.journal = journal
        self.hal = hal
        self.repo_managers = repo_managers
        self.firmware_workflows = firmware_workflows
        self.supervisor = supervisor
        self.scan_qr = scan_qr
        self.recovery_committing_handlers = recovery_committing_handlers
        self.wifi_manager = wifi_manager

    def prepare_early_services(self) -> list[str]:
        placeholder_mode = ModeDecision(production=False, reasons=[], capabilities={})
        names = []
        for service in self.settings.services:
            if service.phase != "early":
                continue
            self._register_service(service, placeholder_mode, camera_device=None, mcu_port=None)
            names.append(service.name)
        return names

    async def run(self) -> BootResult:
        boot_id = uuid.uuid4().hex
        started_at = time.time()
        with self.db.transaction() as conn:
            store.insert_boot_history(
                conn, boot_id, started_at,
                gpio_forced=None, mode=None, reasons="[]", facts="{}", final_state="running",
            )

        recovery.run(self.db, self.journal, committing_handlers=self.recovery_committing_handlers)

        facts = BootFacts()
        facts.gpio_forced = self._read_gpio()

        self._maybe_scan_qr(facts)

        camera_device = resolve_camera_device(self.settings.hardware.camera, self.hal.camera_probe)
        self._probe_hardware(facts, camera_device)

        self._update_repos(facts)
        self._run_firmware_targets(facts)

        mode = decide_mode(facts)

        mcu_port = resolve_mcu_port(self.settings.hardware.mcu, self.hal.usb_inventory)
        self._prepare_services(mode, camera_device, mcu_port)

        main_services = [s for s in self.settings.services if s.phase != "early"]
        order = topological_order(main_services)
        services_ready = await self.supervisor.start_ordered(order, mode.capabilities)

        with self.db.transaction() as conn:
            store.update_boot_history(
                conn, boot_id,
                gpio_forced=facts.gpio_forced,
                mode="production" if mode.production else "nonprod",
                reasons=json.dumps(mode.reasons),
                facts=json.dumps(dataclasses.asdict(facts)),
                final_state="running",
            )

        return BootResult(boot_id=boot_id, mode=mode, facts=facts, services_ready=services_ready)

    def _read_gpio(self) -> bool | None:
        gpio_cfg = self.settings.gpio
        if not gpio_cfg.is_configured():
            return None
        try:
            active = self.hal.gpio_reader.read(
                gpio_cfg.chip, gpio_cfg.line, gpio_cfg.active_low,
                gpio_cfg.bias, gpio_cfg.samples, gpio_cfg.sample_interval_ms,
            )
            return bool(active)
        except Exception:
            return gpio_cfg.on_error == "nonprod"

    def _maybe_scan_qr(self, facts: BootFacts) -> None:
        required = self.settings.secrets.required
        missing = secrets_store.missing_required_secrets(self.paths, required)
        if missing and self.scan_qr is not None:
            payload = self.scan_qr()
            if payload is not None:
                ingest_payload(self.paths, self.journal, payload)
                missing = secrets_store.missing_required_secrets(self.paths, required)
        facts.missing_required_secrets = missing

    def _probe_hardware(self, facts: BootFacts, camera_device: str | None) -> None:
        if camera_device is not None:
            camera_result = self.hal.camera_probe.probe(camera_device)
        else:
            camera_result = ProbeResult(ok=False, detail="no camera device resolved")
        facts.camera_ok = camera_result.ok

        facts.mic_ok = self.hal.mic_probe.probe().ok
        facts.internet_ok = self.hal.net_probe.probe().ok
        if not facts.internet_ok and self.wifi_manager is not None:
            known = secrets_store.load_wifi_credentials(self.paths)
            if known and self.wifi_manager.try_known_networks(known) is not None:
                facts.internet_ok = self.hal.net_probe.probe().ok

        facts.stlink_present = self.hal.swd_probe.stlink_present()
        facts.mcu_present = facts.stlink_present and self.hal.swd_probe.target_present()

    def _update_repos(self, facts: BootFacts) -> None:
        for name, manager in self.repo_managers.items():
            result = manager.sync()
            if result.error is not None and result.sha is None:
                facts.repo_update_failures.append(name)

    def _run_firmware_targets(self, facts: BootFacts) -> None:
        for name, workflow in self.firmware_workflows.items():
            repo_state = store.get_repo_state(self.db.conn, workflow.target.repo)
            if repo_state is None or repo_state.get("current_sha") is None or repo_state.get("current_release") is None:
                continue
            outcome = workflow.run(repo_state["current_sha"], Path(repo_state["current_release"]))
            self._apply_flash_outcome(facts, outcome)

    def _apply_flash_outcome(self, facts: BootFacts, outcome) -> None:
        if outcome.state == "failed":
            if outcome.reason == "firmware_unavailable":
                facts.firmware_unavailable = True
            elif outcome.reason == "mcu_flash_failed":
                facts.mcu_flash_failed = True
            elif outcome.reason == "mcu_verify_failed":
                facts.mcu_verify_failed = True
            elif outcome.reason == "mcu_client_incompatible":
                facts.mcu_client_incompatible = True

    def _render_values(self, service, mode: ModeDecision, camera_device: str | None, mcu_port: str | None) -> dict:
        values = {
            "run": str(self.paths.run_dir),
            "mode.production": mode.production,
            "hw.mcu_port": mcu_port,
            "hw.camera_device": camera_device,
        }
        if service.repo:
            repo_state = store.get_repo_state(self.db.conn, service.repo)
            if repo_state and repo_state.get("current_release"):
                release_dir = Path(repo_state["current_release"])
                values["release"] = str(release_dir)
                marker_path = release_dir / RELEASE_MARKER
                if marker_path.exists():
                    marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    venv_hash = marker.get("venv_hash")
                    if venv_hash:
                        values["venv"] = str(self.paths.venv_path(service.repo, venv_hash))

        generated = secrets_store.ensure_generated_secrets(self.paths)
        regular = secrets_store.load_secrets(self.paths).get("secrets", {}) or {}
        for key, value in {**generated, **regular}.items():
            values[f"secret.{key}"] = value
        return values

    def _build_env(self, service, values: dict) -> dict[str, str]:
        env = {**os.environ}
        for key, value in render(service.env, values).items():
            env[key] = str(value)
        for secret_key in service.secret_env:
            if f"secret.{secret_key}" in values and values[f"secret.{secret_key}"] is not None:
                env[secret_key] = str(values[f"secret.{secret_key}"])
        return env

    def _prepare_services(self, mode: ModeDecision, camera_device: str | None, mcu_port: str | None) -> None:
        for service in self.settings.services:
            if service.phase == "early":
                continue
            self._register_service(service, mode, camera_device, mcu_port)

    def _register_service(
        self, service, mode: ModeDecision, camera_device: str | None, mcu_port: str | None
    ) -> None:
        values = self._render_values(service, mode, camera_device, mcu_port)
        if service.overlay:
            write_overlay(self.paths.overlay_path(service.name), service.overlay, values)
        rendered_cmd = render(service.cmd, values)
        rendered_config = service.model_copy(update={"cmd": rendered_cmd})
        cwd = Path(values["release"]) if "release" in values else self.paths.run_dir
        env = self._build_env(service, values)
        self.supervisor.register(rendered_config, cwd=cwd, env=env)
