import asyncio
import dataclasses
import json
import os
import subprocess
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robot_orchestrator.bootlog import BOOT_LOG, logged_run
from robot_orchestrator.boot.context import BootFacts
from robot_orchestrator.boot.decision import ModeDecision, decide_mode
from robot_orchestrator.boot.hardware import resolve_camera_device, resolve_chromium_bin, resolve_mcu_port
from robot_orchestrator.boot.progress import FAILED, OK, SKIPPED, WARN, BootProgress
from robot_orchestrator.config import Settings
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RELEASE_MARKER, RepoManager
from robot_orchestrator.repos.venvs import venv_python
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
from robot_orchestrator.web import auth as web_auth


@dataclass
class BootResult:
    boot_id: str
    mode: ModeDecision
    facts: BootFacts
    services_ready: dict[str, bool]


__all__ = ["BootProgress", "BootResult", "BootSequence"]


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
        progress: "BootProgress | None" = None,
        ensure_tooling: Callable[[], None] | None = None,
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
        self.progress = progress or BootProgress()
        self.ensure_tooling = ensure_tooling

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
        progress = self.progress
        boot_id = uuid.uuid4().hex
        started_at = time.time()

        progress.begin("init", "база состояния")
        with self.db.transaction() as conn:
            store.insert_boot_history(
                conn, boot_id, started_at,
                gpio_forced=None, mode=None, reasons="[]", facts="{}", final_state="running",
            )
        progress.finish("init", OK, f"загрузка {boot_id[:8]}")

        progress.begin("recover", "проверяю журнал операций после возможного обрыва питания")
        report = recovery.run(self.db, self.journal, committing_handlers=self.recovery_committing_handlers)
        progress.finish("recover", OK, self._describe_recovery(report))

        facts = BootFacts()
        self._step_gpio(facts)
        await self._step_secrets(facts)
        camera_device = await self._step_hardware(facts)
        await self._step_tooling()
        await self._step_mcu(facts)

        progress.begin("repos", f"{len(self.repo_managers)} репозитория")
        await asyncio.to_thread(self._update_repos, facts)
        failed_repos = facts.repo_update_failures
        if failed_repos:
            progress.finish("repos", WARN, "не обновились: " + ", ".join(failed_repos))
        else:
            progress.finish("repos", OK, "все репозитории актуальны")

        progress.begin("firmware")
        outcomes = await asyncio.to_thread(self._run_firmware_targets, facts)
        self._finish_firmware_step(outcomes)

        progress.begin("mode", "сверяю все факты")
        mode = decide_mode(facts)
        if mode.production:
            progress.finish("mode", OK, "PROD — все условия выполнены")
        else:
            progress.finish("mode", WARN, "NON-PROD — " + ", ".join(mode.reasons))

        progress.begin("services", "запускаю по цепочке зависимостей")
        mcu_port = resolve_mcu_port(self.settings.hardware.mcu, self.hal.usb_inventory)
        self._prepare_services(mode, camera_device, mcu_port)

        main_services = [s for s in self.settings.services if s.phase != "early"]
        order = topological_order(main_services)
        services_ready = await self.supervisor.start_ordered(order, mode.capabilities)
        not_ready = [name for name, ready in services_ready.items() if not ready]
        if not_ready:
            progress.finish("services", WARN, "не поднялись: " + ", ".join(not_ready))
        else:
            progress.finish("services", OK, ", ".join(order) + " готовы")

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

    @staticmethod
    def _describe_recovery(report) -> str:
        total = len(report.rolled_back) + len(report.rolled_forward) + len(report.abandoned)
        if total == 0:
            return "незавершённых операций нет"
        return (
            f"восстановлено операций: {total} "
            f"(откат {len(report.rolled_back)}, докатка {len(report.rolled_forward)}, "
            f"отброшено {len(report.abandoned)})"
        )

    def _step_gpio(self, facts: BootFacts) -> None:
        progress = self.progress
        progress.begin("gpio")
        gpio_cfg = self.settings.gpio
        facts.gpio_forced = self._read_gpio()
        if not gpio_cfg.is_configured():
            progress.skip("gpio", "проверка не настроена")
        elif facts.gpio_forced:
            progress.finish("gpio", WARN, "перемычка замкнута — принудительный NON-PROD")
        else:
            progress.finish("gpio", OK, "перемычка разомкнута")

    async def _step_secrets(self, facts: BootFacts) -> None:
        progress = self.progress
        required = self.settings.secrets.required
        progress.begin("secrets", "проверяю обязательные ключи")
        missing = secrets_store.missing_required_secrets(self.paths, required)
        if not missing:
            progress.finish("secrets", OK, f"все {len(required)} ключа на месте")
            progress.skip("qr", "секреты уже введены")
            facts.missing_required_secrets = []
            return

        progress.finish("secrets", WARN, "не хватает: " + ", ".join(missing))
        if self.scan_qr is None:
            progress.skip("qr", "сканер недоступен")
            facts.missing_required_secrets = missing
            return

        progress.begin("qr", "покажите камере робота лист с QR-кодами")
        payload = await asyncio.to_thread(self.scan_qr)
        if payload is None:
            progress.finish("qr", WARN, "QR-код не получен")
        else:
            ingest_payload(self.paths, self.journal, payload)
            BOOT_LOG.set_known_secrets(secrets_store.all_secret_values(self.paths))
            missing = secrets_store.missing_required_secrets(self.paths, required)
            orch_admin_password = payload.secrets.get("ORCH_ADMIN_PASSWORD")
            if orch_admin_password:
                web_auth.apply_password_if_default(
                    self.paths, self.settings.web.admin_user, orch_admin_password
                )
            progress.finish("qr", OK, "секреты приняты и сохранены")
        facts.missing_required_secrets = missing

    async def _step_hardware(self, facts: BootFacts) -> str | None:
        progress = self.progress

        progress.begin("camera", "ищу веб-камеру")
        camera_device = await asyncio.to_thread(
            resolve_camera_device, self.settings.hardware.camera, self.hal.camera_probe
        )
        if camera_device is None:
            facts.camera_ok = False
            progress.finish("camera", FAILED, "рабочая камера не найдена")
        else:
            camera_result = await asyncio.to_thread(self.hal.camera_probe.probe, camera_device)
            facts.camera_ok = camera_result.ok
            progress.finish("camera", OK if camera_result.ok else FAILED, camera_result.detail)

        progress.begin("microphone", "записываю пробный сигнал")
        mic_result = await asyncio.to_thread(self.hal.mic_probe.probe)
        facts.mic_ok = mic_result.ok
        progress.finish("microphone", OK if mic_result.ok else FAILED, mic_result.detail)

        progress.begin("network", "проверяю выход в интернет")
        net_result = await asyncio.to_thread(self.hal.net_probe.probe)
        facts.internet_ok = net_result.ok
        if not net_result.ok and self.wifi_manager is not None:
            known = secrets_store.load_wifi_credentials(self.paths)
            if known:
                progress.update("network", "интернета нет — пробую известные Wi-Fi сети")
                connected = await asyncio.to_thread(self.wifi_manager.try_known_networks, known)
                if connected is not None:
                    net_result = await asyncio.to_thread(self.hal.net_probe.probe)
                    facts.internet_ok = net_result.ok
        progress.finish("network", OK if facts.internet_ok else FAILED, net_result.detail)
        return camera_device

    async def _step_tooling(self) -> None:
        progress = self.progress
        if self.ensure_tooling is None:
            progress.skip("tooling", "не требуется")
            return
        progress.begin("tooling", "PlatformIO и OpenOCD")
        try:
            await asyncio.to_thread(self.ensure_tooling)
        except Exception as e:
            progress.finish("tooling", FAILED, str(e).splitlines()[0][:160] if str(e) else type(e).__name__)
            return
        progress.finish("tooling", OK, "инструменты готовы")

    async def _step_mcu(self, facts: BootFacts) -> None:
        progress = self.progress
        progress.begin("mcu", "ищу ST-Link на USB")
        facts.stlink_present = await asyncio.to_thread(self.hal.swd_probe.stlink_present)
        if not facts.stlink_present:
            facts.mcu_present = False
            progress.finish("mcu", FAILED, "ST-Link не найден")
            return
        progress.update("mcu", "ST-Link найден — читаю IDCODE микроконтроллера по SWD")
        facts.mcu_present = await asyncio.to_thread(self.hal.swd_probe.target_present)
        if facts.mcu_present:
            progress.finish("mcu", OK, "STM32 отвечает по SWD")
        else:
            progress.finish("mcu", FAILED, "ST-Link есть, но микроконтроллер не отвечает")

    def _update_repos(self, facts: BootFacts) -> None:
        for name, manager in self.repo_managers.items():
            self.progress.update("repos", f"{name}: проверяю обновления")
            self.progress.note(f"{name}: синхронизация с {manager.config.url} ({manager.config.branch})", source=name)
            result = manager.sync()
            if result.error is not None and result.sha is None:
                facts.repo_update_failures.append(name)
                self.progress.note(f"{name}: не удалось обновить — {result.error}", "error", source=name)
                continue
            if result.error is not None:
                self.progress.note(f"{name}: {result.error}", "warn", source=name)
            elif result.changed:
                self.progress.note(f"{name}: новый релиз {(result.sha or '')[:12]}", "ok", source=name)
            else:
                self.progress.note(f"{name}: уже актуален {(result.sha or '')[:12]}", "info", source=name)
            if name == "web-core":
                self._ensure_webcore_admin()

    def _ensure_webcore_admin(self) -> None:
        repo_state = store.get_repo_state(self.db.conn, "web-core")
        if not repo_state or not repo_state.get("current_release"):
            return
        release_dir = Path(repo_state["current_release"])
        marker_path = release_dir / RELEASE_MARKER
        if not marker_path.exists():
            return
        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        venv_hash = marker.get("venv_hash")
        if not venv_hash:
            return
        python = venv_python(self.paths.venv_path("web-core", venv_hash))
        script = release_dir / "tools" / "create_admin.py"
        if not python.exists() or not script.exists():
            return

        generated = secrets_store.ensure_generated_secrets(self.paths)
        regular = secrets_store.load_secrets(self.paths).get("secrets", {}) or {}
        admin_password = regular.get("WEBCORE_ADMIN_PASSWORD") or generated.get("WEBCORE_ADMIN_PASSWORD")
        if not admin_password:
            return

        env = {**os.environ, "ADMIN_PASSWORD": admin_password}
        self.progress.note("web-core: проверяю учётную запись администратора", source="web-core")
        try:
            logged_run(
                [str(python), str(script)], source="web-core", cwd=release_dir, env=env, timeout=30.0,
            )
        except (OSError, subprocess.TimeoutExpired):
            pass

    def _run_firmware_targets(self, facts: BootFacts) -> list[tuple[str, object]]:
        outcomes = []
        for name, workflow in self.firmware_workflows.items():
            repo_state = store.get_repo_state(self.db.conn, workflow.target.repo)
            if repo_state is None or repo_state.get("current_sha") is None or repo_state.get("current_release") is None:
                self.progress.note(f"{name}: репозиторий прошивки ещё не готов — пропускаю", "warn", source=name)
                continue
            self.progress.update("firmware", f"{name}: сверяю версию прошивки")
            outcome = workflow.run(repo_state["current_sha"], Path(repo_state["current_release"]))
            self._apply_flash_outcome(facts, outcome)
            outcomes.append((name, outcome))
        return outcomes

    def _finish_firmware_step(self, outcomes: list[tuple[str, object]]) -> None:
        progress = self.progress
        if not outcomes:
            progress.finish("firmware", SKIPPED, "нет данных о прошивке")
            return
        failed = [(name, o) for name, o in outcomes if o.state == "failed"]
        if failed:
            details = ", ".join(f"{name}: {o.reason}" for name, o in failed)
            progress.finish("firmware", FAILED, details)
            return
        skipped = [(name, o) for name, o in outcomes if o.state == "skipped"]
        if skipped and len(skipped) == len(outcomes):
            details = ", ".join(f"{name}: {o.reason}" for name, o in skipped)
            progress.finish("firmware", SKIPPED, details)
            return
        flashed = [name for name, o in outcomes if o.state == "flashed"]
        if flashed:
            progress.finish("firmware", OK, "прошито и проверено: " + ", ".join(flashed))
        else:
            progress.finish("firmware", OK, "прошивка актуальна")

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
            "hw.chromium_bin": resolve_chromium_bin(),
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
