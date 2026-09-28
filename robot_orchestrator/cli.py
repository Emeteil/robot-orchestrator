import argparse
import asyncio
import json
import os
import signal
import subprocess
import sys
import uuid
from pathlib import Path

import uvicorn
import yaml

from robot_orchestrator import log as logmod
from robot_orchestrator.boot.decision import capability
from robot_orchestrator.boot.fsm import BootSequence
from robot_orchestrator.boot.hardware import resolve_camera_device, resolve_mcu_port
from robot_orchestrator.config import Settings, load_settings
from robot_orchestrator.firmware.github_artifacts import GithubArtifactClient
from robot_orchestrator.firmware.mcu_client import SubprocessMcuClient
from robot_orchestrator.firmware.openocd import OpenOcdFlasher
from robot_orchestrator.firmware.pio_build import PioFirmwareBuilder
from robot_orchestrator.firmware.workflow import FirmwareWorkflow
from robot_orchestrator.hal.factory import build_hal
from robot_orchestrator.hal.fake.scenario import load_scenario
from robot_orchestrator.kiosk.cdp import CdpError, KioskController
from robot_orchestrator.network.wifi import WifiManager
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RELEASE_MARKER, RepoManager, make_repo_swap_committing_handler
from robot_orchestrator.repos.venvs import VenvBuilder, venv_pip, venv_python
from robot_orchestrator.secrets import store as secrets_store
from robot_orchestrator.secrets.ingest import ingest_payload
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.sdnotify import SdNotifier
from robot_orchestrator.selfupdate.manager import SelfUpdateManager, make_self_update_committing_handler
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.supervisor.render import render
from robot_orchestrator.supervisor.supervisor import Supervisor, topological_order
from robot_orchestrator.wal.journal import Journal
from robot_orchestrator.web import auth as web_auth
from robot_orchestrator.web.app import create_app
from robot_orchestrator.web.context import AdminContext

PACKAGE_DIR = Path(__file__).resolve().parent
REPO_ROOT = PACKAGE_DIR.parent
DEFAULT_SETTINGS_PATH = REPO_ROOT / "settings.yml"
DEFAULT_LOCAL_SETTINGS_PATH = Path("/etc/robot-orchestrator/settings.local.yml")


class NullMcuClient:
    def ping(self) -> float | None:
        return None

    def version(self) -> dict | None:
        return None


def _load_settings(args: argparse.Namespace) -> Settings:
    default_path = Path(args.config) if args.config else DEFAULT_SETTINGS_PATH
    local_path = Path(args.local_config) if args.local_config else DEFAULT_LOCAL_SETTINGS_PATH
    return load_settings(default_path, local_path)


def _build_paths(settings: Settings, args: argparse.Namespace) -> Paths:
    if args.state_dir:
        base = Path(args.state_dir)
        paths = Paths(base / "state", base / "logs", base / "run")
    else:
        paths = Paths(Path(settings.paths.state_dir), Path(settings.paths.log_dir), Path(settings.paths.run_dir))
    paths.ensure()
    return paths


def _get_secret(paths: Paths, key: str) -> str | None:
    generated = secrets_store.ensure_generated_secrets(paths)
    regular = secrets_store.load_secrets(paths).get("secrets", {}) or {}
    return {**generated, **regular}.get(key)


def _build_hal(settings: Settings, paths: Paths, args: argparse.Namespace):
    if getattr(args, "simulate", None):
        scenario = load_scenario(Path(args.simulate))
        return build_hal(settings, paths, scenario=scenario)
    return build_hal(settings, paths)


def _build_repo_managers(settings: Settings, paths: Paths, journal: Journal, db: Database) -> dict[str, RepoManager]:
    venv_builder = VenvBuilder(paths.venvs_dir, paths.wheelhouse_dir)
    managers = {}
    for repo_cfg in settings.repos:
        managers[repo_cfg.name] = RepoManager(
            paths, journal, db, repo_cfg,
            venv_builder=venv_builder,
            fetch_timeout=settings.updates.fetch_timeout_s,
        )
    return managers


def _ensure_platformio_venv(paths: Paths) -> Path:
    venv_path = paths.platformio_venv
    marker = venv_path / ".complete"
    if marker.exists():
        return venv_path
    venv_path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True, capture_output=True, text=True)
    subprocess.run([str(venv_pip(venv_path)), "install", "platformio"], check=True, capture_output=True, text=True)
    marker.write_text("")
    return venv_path


def _make_mcu_client_factory(paths: Paths, db: Database, target, mcu_port_resolver):
    mcu_probe_script = PACKAGE_DIR / "workers" / "mcu_probe.py"

    def factory():
        port = mcu_port_resolver()
        if port is None:
            return NullMcuClient()

        repo_state = store.get_repo_state(db.conn, target.verify.client_repo)
        if not repo_state or not repo_state.get("current_release"):
            return NullMcuClient()

        release_dir = Path(repo_state["current_release"])
        marker_path = release_dir / RELEASE_MARKER
        if not marker_path.exists():
            return NullMcuClient()

        marker = json.loads(marker_path.read_text(encoding="utf-8"))
        venv_hash = marker.get("venv_hash")
        if not venv_hash:
            return NullMcuClient()

        python_executable = venv_python(paths.venv_path(target.verify.client_repo, venv_hash))

        def runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
            env = {**os.environ, "PYTHONPATH": str(release_dir), "PYTHONNOUSERSITE": "1"}
            return subprocess.run(argv, env=env, cwd=release_dir, capture_output=True, text=True, timeout=timeout)

        return SubprocessMcuClient(
            python_executable, mcu_probe_script, port,
            connect_timeout_s=10.0, ping_tries=target.verify.ping_tries, runner=runner,
        )

    return factory


def _build_firmware_workflows(settings: Settings, paths: Paths, journal: Journal, db: Database, hal) -> dict:
    workflows = {}
    pat = _get_secret(paths, "GITHUB_PAT") or ""
    openocd_pkg_dir = paths.pio_core_dir / "packages" / "tool-openocd"
    for target in settings.firmware_targets:
        artifact_client = GithubArtifactClient(target.ci, token=pat)
        pio_venv = _ensure_platformio_venv(paths)
        builder = PioFirmwareBuilder(paths.pio_core_dir, str(venv_python(pio_venv)))
        flasher = OpenOcdFlasher(
            openocd_binary=openocd_pkg_dir / "bin" / "openocd",
            scripts_dir=openocd_pkg_dir / "openocd" / "scripts",
            config=target.flash,
        )

        def mcu_port_resolver(mcu_config=settings.hardware.mcu, usb_inventory=hal.usb_inventory):
            return resolve_mcu_port(mcu_config, usb_inventory)

        workflows[target.name] = FirmwareWorkflow(
            target=target, paths=paths, journal=journal, db=db,
            swd_probe=hal.swd_probe, artifact_client=artifact_client,
            builder=builder, flasher=flasher,
            mcu_client_factory=_make_mcu_client_factory(paths, db, target, mcu_port_resolver),
        )
    return workflows


def _build_qr_scanner(settings: Settings, paths: Paths, hal):
    def scan_qr() -> SecretsPayload | None:
        device = resolve_camera_device(settings.hardware.camera, hal.camera_probe)
        if device is None:
            return None

        argv = [
            sys.executable, "-m", "robot_orchestrator.workers.qr_scan", device,
            "--timeout", str(settings.qr.scan_timeout_s),
            "--preview-path", str(paths.qr_preview),
            "--preview-fps", str(settings.qr.preview_fps),
        ]
        process = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        try:
            for line in process.stdout:
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if event.get("event") == "complete":
                    return SecretsPayload.model_validate(event["payload"])
                if event.get("event") in ("timeout", "camera_error"):
                    return None
            return None
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()

    return scan_qr


async def _navigate_kiosk(settings: Settings, generated: dict) -> None:
    url = render(settings.kiosk.operator_url, {"secret.MASTER_TOKEN": generated.get("MASTER_TOKEN", "")})
    controller = KioskController()
    try:
        await controller.navigate(url)
    except CdpError:
        pass


async def _wait_for_port(host: str, port: int, timeout_s: float = 15.0) -> None:
    connect_host = "127.0.0.1" if host == "0.0.0.0" else host
    deadline = asyncio.get_event_loop().time() + timeout_s
    while True:
        try:
            _, writer = await asyncio.open_connection(connect_host, port)
            writer.close()
            return
        except OSError:
            if asyncio.get_event_loop().time() >= deadline:
                return
            await asyncio.sleep(0.2)


async def _wait_for_requirements(services: list, timeout_s: float = 30.0) -> None:
    requirements = {req for service in services for req in service.requires}
    if "x11" in requirements:
        deadline = asyncio.get_event_loop().time() + timeout_s
        while not Path("/tmp/.X11-unix/X0").exists():
            if asyncio.get_event_loop().time() >= deadline:
                break
            await asyncio.sleep(0.5)


async def _watchdog_loop(notifier: SdNotifier, stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        notifier.watchdog()
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=20.0)
        except asyncio.TimeoutError:
            continue


async def _run_orchestrator(settings: Settings, paths: Paths, args: argparse.Namespace) -> None:
    logmod.configure_logging(paths.orchestrator_log)
    db = Database(paths.state_db)
    journal = Journal(db, boot_id=uuid.uuid4().hex)

    web_auth.ensure_default_admin(paths, username=settings.web.admin_user)
    generated = secrets_store.ensure_generated_secrets(paths)

    hal = _build_hal(settings, paths, args)
    repo_managers = _build_repo_managers(settings, paths, journal, db)
    firmware_workflows = _build_firmware_workflows(settings, paths, journal, db, hal)
    supervisor = Supervisor(log_dir=paths.log_dir)
    scan_qr = _build_qr_scanner(settings, paths, hal)
    wifi_manager = WifiManager()
    self_update_manager = SelfUpdateManager(paths, journal, db, settings.self_update)
    recovery_handlers = {
        "repo_swap": make_repo_swap_committing_handler(paths),
        "repo_rollback": make_repo_swap_committing_handler(paths),
        "self_update_swap": make_self_update_committing_handler(paths),
    }

    stop_event = asyncio.Event()
    context = AdminContext(
        settings=settings, paths=paths, db=db, journal=journal,
        repo_managers=repo_managers, firmware_workflows=firmware_workflows,
        supervisor=supervisor, scan_qr=scan_qr, wifi_manager=wifi_manager,
        self_update_manager=self_update_manager, request_restart=stop_event.set,
    )

    sequence = BootSequence(
        settings, paths, db, journal, hal,
        repo_managers, firmware_workflows, supervisor,
        scan_qr=scan_qr, recovery_committing_handlers=recovery_handlers, wifi_manager=wifi_manager,
    )

    notifier = SdNotifier()
    notifier.status("booting")

    app = create_app(paths, jwt_secret=generated["ORCH_JWT_SECRET"], jwt_ttl_s=settings.web.jwt_ttl_s, context=context)
    web_config = uvicorn.Config(app, host=settings.web.host, port=settings.web.port, log_config=None)
    server = uvicorn.Server(web_config)
    server_task = asyncio.create_task(server.serve())

    early_names = sequence.prepare_early_services()
    if early_names:
        early_services = [s for s in settings.services if s.name in early_names]
        await _wait_for_port(settings.web.host, settings.web.port)
        await _wait_for_requirements(early_services)
        await supervisor.start_ordered(topological_order(early_services), capabilities={})

    result = await sequence.run()
    context.boot_result = result
    notifier.status(f"running mode={'production' if result.mode.production else 'nonprod'}")
    notifier.ready()

    if result.mode.production and capability(result.mode, "kiosk_browser"):
        await _navigate_kiosk(settings, generated)

    self_update_task = None
    if settings.self_update.enabled:
        self_update_task = asyncio.create_task(asyncio.to_thread(self_update_manager.check_and_stage))

    order = topological_order(settings.services)
    loop = asyncio.get_running_loop()
    if os.name != "nt":
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, stop_event.set)

    watchdog_task = asyncio.create_task(_watchdog_loop(notifier, stop_event))

    try:
        await stop_event.wait()
    except KeyboardInterrupt:
        pass

    notifier.stopping()
    watchdog_task.cancel()
    if self_update_task is not None:
        self_update_task.cancel()
    server.should_exit = True
    await supervisor.stop_ordered(order)
    await server_task
    db.close()


def cmd_run(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    asyncio.run(_run_orchestrator(settings, paths, args))
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    hal = build_hal(settings, paths)

    print(f"python: {sys.version.split()[0]}")
    print(f"state_dir: {paths.state_dir}")
    print(f"bootstrap stamp present: {paths.bootstrap_stamp.exists()}")

    by_id_dir = Path("/dev/v4l/by-id")
    print(f"camera by-id dir exists: {by_id_dir.exists()}")
    if by_id_dir.exists():
        for candidate in sorted(by_id_dir.glob("*")):
            probe_result = hal.camera_probe.probe(str(candidate))
            print(f"  {candidate.name}: ok={probe_result.ok} detail={probe_result.detail}")
    print(f"resolved camera device: {resolve_camera_device(settings.hardware.camera, hal.camera_probe)}")

    usb_devices = hal.usb_inventory.list_devices()
    print(f"usb devices: {len(usb_devices)}")
    for device in usb_devices:
        print(f"  {device.get('vendor_id')}:{device.get('product_id')} {device.get('description', '')}")
    print(f"stlink present: {hal.swd_probe.stlink_present()}")
    print(f"resolved mcu serial port: {resolve_mcu_port(settings.hardware.mcu, hal.usb_inventory)}")

    mic_result = hal.mic_probe.probe()
    print(f"microphone: ok={mic_result.ok} detail={mic_result.detail}")
    net_result = hal.net_probe.probe()
    print(f"internet: ok={net_result.ok} detail={net_result.detail}")

    wifi_manager = WifiManager()
    known_wifi = secrets_store.load_wifi_credentials(paths)
    visible_ssids = wifi_manager.list_visible_ssids()
    print(f"wifi known networks: {[w.ssid for w in known_wifi]}")
    print(f"wifi visible networks: {visible_ssids}")

    if settings.gpio.is_configured():
        try:
            active = hal.gpio_reader.read(
                settings.gpio.chip, settings.gpio.line, settings.gpio.active_low,
                settings.gpio.bias, settings.gpio.samples, settings.gpio.sample_interval_ms,
            )
            print(f"gpio forced: {active}")
        except Exception as e:
            print(f"gpio read error: {e}")
    else:
        print("gpio: not configured (disabled, or chip/line unset)")

    tracked_secret_keys = list(dict.fromkeys([*settings.secrets.required, "GITHUB_PAT"]))
    for key in tracked_secret_keys:
        print(f"secret {key} present: {_get_secret(paths, key) is not None}")

    return 0


def cmd_gpio_test(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    hal = build_hal(settings, paths)

    chip = args.chip or settings.gpio.chip
    line = args.line if args.line is not None else settings.gpio.line
    if chip is None or line is None:
        print("no chip/line given (pass --chip/--line or configure gpio.chip/gpio.line)")
        return 1

    active = hal.gpio_reader.read(
        chip, line, settings.gpio.active_low, settings.gpio.bias,
        settings.gpio.samples, settings.gpio.sample_interval_ms,
    )
    print(f"chip={chip} line={line} active={active}")
    return 0


def cmd_secrets_import(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    db = Database(paths.state_db)
    journal = Journal(db, boot_id=uuid.uuid4().hex)

    with Path(args.file).open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    payload = SecretsPayload.model_validate(data)
    ingest_payload(paths, journal, payload)
    db.close()
    print("secrets imported")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    db = Database(paths.state_db)

    repos = {repo_cfg.name: store.get_repo_state(db.conn, repo_cfg.name) for repo_cfg in settings.repos}
    firmware = {target.name: store.get_flash_state(db.conn, target.name) for target in settings.firmware_targets}
    last_boot = db.conn.execute(
        "SELECT * FROM boot_history ORDER BY started_at DESC LIMIT 1"
    ).fetchone()

    print(json.dumps({
        "repos": repos,
        "firmware": firmware,
        "last_boot": dict(last_boot) if last_boot else None,
    }, indent=2, default=str))
    db.close()
    return 0


def _self_update_manager_for_cli(settings: Settings, paths: Paths) -> tuple[SelfUpdateManager, Database]:
    db = Database(paths.state_db)
    journal = Journal(db, boot_id=uuid.uuid4().hex)
    return SelfUpdateManager(paths, journal, db, settings.self_update), db


def _print_self_update_status(status) -> None:
    print(f"current_sha: {status.current_sha}")
    print(f"staged_sha: {status.staged_sha}")
    print(f"staged_at: {status.staged_at}")
    print(f"error: {status.error}")


def cmd_self_update_status(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    manager, db = _self_update_manager_for_cli(settings, paths)
    _print_self_update_status(manager.status())
    db.close()
    return 0


def cmd_self_update_check(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    manager, db = _self_update_manager_for_cli(settings, paths)
    status = manager.check_and_stage()
    _print_self_update_status(status)
    db.close()
    return 0 if status.error is None else 1


def cmd_self_update_apply(args: argparse.Namespace) -> int:
    settings = _load_settings(args)
    paths = _build_paths(settings, args)
    manager, db = _self_update_manager_for_cli(settings, paths)
    applied = manager.apply()
    db.close()
    if applied:
        print("applied. restart the service for it to take effect: sudo systemctl restart robot-orchestrator")
        return 0
    print("nothing valid staged to apply")
    return 1


def _add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--config", help="path to settings.yml (defaults to the bundled one)")
    parser.add_argument("--local-config", help="path to settings.local.yml (site overrides)")
    parser.add_argument("--state-dir", help="override state/log/run directories with a single base directory")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="robot-orchestrator")
    subparsers = parser.add_subparsers(dest="command", required=True)

    run_parser = subparsers.add_parser("run", help="run the orchestrator (boot sequence + supervisor + web)")
    _add_common_args(run_parser)
    run_parser.add_argument("--simulate", help="path to a scenarios/*.yml file to run against a fake HAL")
    run_parser.set_defaults(func=cmd_run)

    doctor_parser = subparsers.add_parser("doctor", help="print hardware/config diagnostics")
    _add_common_args(doctor_parser)
    doctor_parser.set_defaults(func=cmd_doctor)

    gpio_parser = subparsers.add_parser("gpio-test", help="read the configured (or given) GPIO line once")
    _add_common_args(gpio_parser)
    gpio_parser.add_argument("--chip")
    gpio_parser.add_argument("--line", type=int)
    gpio_parser.set_defaults(func=cmd_gpio_test)

    secrets_parser = subparsers.add_parser("secrets", help="secrets management")
    secrets_sub = secrets_parser.add_subparsers(dest="secrets_command", required=True)
    secrets_import_parser = secrets_sub.add_parser("import", help="import a secrets payload from a local YAML file")
    _add_common_args(secrets_import_parser)
    secrets_import_parser.add_argument("file")
    secrets_import_parser.set_defaults(func=cmd_secrets_import)

    status_parser = subparsers.add_parser("status", help="print repo/firmware/boot state as JSON")
    _add_common_args(status_parser)
    status_parser.set_defaults(func=cmd_status)

    self_update_parser = subparsers.add_parser("self-update", help="orchestrator self-update")
    self_update_sub = self_update_parser.add_subparsers(dest="self_update_command", required=True)

    self_update_status_parser = self_update_sub.add_parser("status", help="show current/staged self-update state")
    _add_common_args(self_update_status_parser)
    self_update_status_parser.set_defaults(func=cmd_self_update_status)

    self_update_check_parser = self_update_sub.add_parser("check", help="fetch, stage and validate a new version")
    _add_common_args(self_update_check_parser)
    self_update_check_parser.set_defaults(func=cmd_self_update_check)

    self_update_apply_parser = self_update_sub.add_parser(
        "apply", help="atomically switch to the staged version (requires a manual service restart afterwards)"
    )
    _add_common_args(self_update_apply_parser)
    self_update_apply_parser.set_defaults(func=cmd_self_update_apply)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
