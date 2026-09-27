import socket
import subprocess
import sys
from pathlib import Path

import pytest

from robot_orchestrator.boot.fsm import BootSequence
from robot_orchestrator.config import (
    CameraConfig,
    FirmwareCiConfig,
    FirmwareTargetConfig,
    FirmwareVerifyConfig,
    GpioConfig,
    HardwareConfig,
    McuConfig,
    ReadyProbeConfig,
    RepoConfig,
    RestartPolicyConfig,
    SecretsConfig,
    ServiceConfig,
    Settings,
    StopConfig,
)
from robot_orchestrator.firmware.workflow import FirmwareWorkflow
from robot_orchestrator.hal.base import ArtifactRef
from robot_orchestrator.hal.factory import Hal
from robot_orchestrator.hal.fake.fakes import (
    FakeArtifactClient,
    FakeCameraProbe,
    FakeClock,
    FakeFirmwareBuilder,
    FakeFlasher,
    FakeGpioReader,
    FakeMcuClient,
    FakeMicProbe,
    FakeNetProbe,
    FakeSwdProbe,
    FakeUsbInventory,
)
from robot_orchestrator.hal.fake.scenario import Scenario
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RepoManager
from robot_orchestrator.secrets.protocol import SecretsPayload
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.supervisor.supervisor import Supervisor
from robot_orchestrator.wal.journal import Journal

BASE_ENV_OK = {"PYTHONUNBUFFERED": "1"}


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    for name, content in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return _git(["rev-parse", "HEAD"], path)


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


APP_SCRIPT = """
import socket
import sys
import time

port = int(sys.argv[1])
sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
sock.bind(("127.0.0.1", port))
sock.listen(1)
while True:
    time.sleep(1.0)
"""


class Env:
    def __init__(self, tmp_path: Path, scenario: Scenario, secrets_required: list[str] | None = None):
        self.tmp_path = tmp_path
        self.paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
        self.paths.ensure()
        self.db = Database(self.paths.state_db)
        self.journal = Journal(self.db, boot_id="boot-1")

        self.app_src = tmp_path / "app-src"
        _init_repo(self.app_src, {"app.py": APP_SCRIPT})

        self.mcu_src = tmp_path / "mcu-src"
        _init_repo(self.mcu_src, {"main.cpp": "int main() {}\\n"})

        self.port = free_port()

        self.hal = Hal(
            clock=FakeClock(),
            camera_probe=FakeCameraProbe(scenario),
            mic_probe=FakeMicProbe(scenario),
            net_probe=FakeNetProbe(scenario),
            usb_inventory=FakeUsbInventory(scenario),
            swd_probe=FakeSwdProbe(scenario),
            gpio_reader=FakeGpioReader(scenario),
        )

        app_repo = RepoConfig(name="app", url=str(self.app_src), branch="main", python=None)
        mcu_repo = RepoConfig(name="mcu-repo", url=str(self.mcu_src), branch="main", python=None)

        self.settings = Settings(
            gpio=GpioConfig(),
            hardware=HardwareConfig(
                camera=CameraConfig(device="0"),
                mcu=McuConfig(serial_glob="usb-nonexistent*"),
            ),
            secrets=SecretsConfig(required=secrets_required or []),
            repos=[app_repo, mcu_repo],
            services=[
                ServiceConfig(
                    name="app",
                    repo="app",
                    cmd=[sys.executable, "{release}/app.py", str(self.port)],
                    ready=ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{self.port}", timeout_s=10.0),
                    stop=StopConfig(timeout_s=2.0),
                    restart=RestartPolicyConfig(backoff_initial_s=0.05, backoff_max_s=0.1, window_s=1.0, max_in_window=2),
                ),
            ],
            firmware_targets=[
                FirmwareTargetConfig(
                    name="stm32",
                    repo="mcu-repo",
                    pio_env="black_f407ve",
                    ci=FirmwareCiConfig(owner="x", repo="y", workflow_file="build.yml", artifact_name="z"),
                    verify=FirmwareVerifyConfig(client_repo="app", client_subdir="com_link_rt"),
                ),
            ],
        )

        image_dir = tmp_path / "firmware-image"
        image_dir.mkdir()
        (image_dir / "firmware.bin").write_bytes(b"\x00" * 32)
        (image_dir / "firmware.elf").write_bytes(b"\x7fELF")

        self.artifact_client = FakeArtifactClient(
            ref=ArtifactRef(run_id="1", download_url="http://example.invalid"),
            image_dir=image_dir,
        )
        self.mcu_client = FakeMcuClient(ping_ms=5.0, version={"build_date": "Sep 27 2026", "build_time": "12:00:00"})

        self.firmware_workflows = {
            "stm32": FirmwareWorkflow(
                target=self.settings.firmware_targets[0],
                paths=self.paths,
                journal=self.journal,
                db=self.db,
                swd_probe=self.hal.swd_probe,
                artifact_client=self.artifact_client,
                builder=FakeFirmwareBuilder(),
                flasher=FakeFlasher(),
                mcu_client_factory=lambda: self.mcu_client,
            ),
        }

        self.repo_managers = {
            "app": RepoManager(self.paths, self.journal, self.db, app_repo),
            "mcu-repo": RepoManager(self.paths, self.journal, self.db, mcu_repo),
        }

        self.supervisor = Supervisor(log_dir=self.paths.log_dir)

    def sequence(self, scan_qr=None, wifi_manager=None) -> BootSequence:
        return BootSequence(
            self.settings, self.paths, self.db, self.journal, self.hal,
            self.repo_managers, self.firmware_workflows, self.supervisor,
            scan_qr=scan_qr, wifi_manager=wifi_manager,
        )

    def close(self) -> None:
        self.db.close()


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path, Scenario(name="happy"))
    yield e
    e.close()


async def test_happy_path_boots_to_production_with_service_ready(env):
    result = await env.sequence().run()

    assert result.mode.production is True
    assert result.mode.reasons == []
    assert result.services_ready["app"] is True

    flash_state = store.get_flash_state(env.db.conn, "stm32")
    assert flash_state["verified"] == 1
    assert flash_state["dirty"] == 0

    await env.supervisor.stop_ordered(["app"])


async def test_missing_camera_forces_non_prod(tmp_path):
    e = Env(tmp_path, Scenario(name="no-camera", camera={"present": False}))
    result = await e.sequence().run()

    assert result.mode.production is False
    assert "camera_unavailable" in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_gpio_forced_causes_non_prod_but_keeps_voice_capability_default_true(tmp_path):
    e = Env(tmp_path, Scenario(name="gpio-forced", gpio={"forced": True}))
    e.settings.gpio = GpioConfig(enabled=True, chip="/dev/gpiochip1", line=11)

    result = await e.sequence().run()

    assert result.mode.production is False
    assert result.mode.reasons == ["gpio_forced"]
    assert result.mode.capabilities["voice_interface"] is True

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_qr_scan_fills_missing_required_secret(tmp_path):
    e = Env(tmp_path, Scenario(name="needs-secret"), secrets_required=["API_KEY"])
    payload = SecretsPayload(v=1, issued_at="2026-09-27T00:00:00Z", secrets={"API_KEY": "value-from-qr"})

    result = await e.sequence(scan_qr=lambda: payload).run()

    assert result.facts.missing_required_secrets == []
    assert "secrets_missing:API_KEY" not in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_no_qr_scanner_and_missing_secret_forces_non_prod(tmp_path):
    e = Env(tmp_path, Scenario(name="missing-secret-no-scanner"), secrets_required=["API_KEY"])

    result = await e.sequence(scan_qr=None).run()

    assert result.mode.production is False
    assert "secrets_missing:API_KEY" in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_wifi_auto_connect_recovers_internet_and_avoids_non_prod(tmp_path):
    e = Env(tmp_path, Scenario(name="wifi-recovery"), secrets_required=[])

    class FlippingNetProbe:
        def __init__(self):
            self.up = False

        def probe(self):
            from robot_orchestrator.hal.base import ProbeResult

            return ProbeResult(ok=self.up, detail="flipping probe")

    class StubWifiManager:
        def __init__(self, net_probe: FlippingNetProbe):
            self.net_probe = net_probe
            self.calls: list[list] = []

        def try_known_networks(self, known):
            self.calls.append(known)
            self.net_probe.up = True
            return known[0].ssid

    e.hal.net_probe = FlippingNetProbe()
    wifi_manager = StubWifiManager(e.hal.net_probe)

    from robot_orchestrator.secrets.ingest import ingest_payload
    from robot_orchestrator.secrets.protocol import SecretsPayload

    payload = SecretsPayload(
        v=1, issued_at="2026-09-27T00:00:00Z", secrets={},
        wifi=[{"ssid": "RobotWifi", "psk": "secret"}],
    )
    ingest_payload(e.paths, e.journal, payload)

    result = await e.sequence(wifi_manager=wifi_manager).run()

    assert wifi_manager.calls
    assert "internet_down" not in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_wifi_auto_connect_not_attempted_when_no_known_networks(tmp_path):
    e = Env(tmp_path, Scenario(name="no-wifi-known"), secrets_required=[])

    class RecordingWifiManager:
        def __init__(self):
            self.called = False

        def try_known_networks(self, known):
            self.called = True
            return None

    from robot_orchestrator.hal.base import ProbeResult

    e.hal.net_probe.probe = lambda: ProbeResult(ok=False)
    wifi_manager = RecordingWifiManager()

    result = await e.sequence(wifi_manager=wifi_manager).run()

    assert wifi_manager.called is False
    assert "internet_down" in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()


async def test_repo_update_failure_recorded_and_forces_non_prod(tmp_path):
    e = Env(tmp_path, Scenario(name="bad-repo"))
    broken_repo = RepoConfig(name="app", url=str(tmp_path / "does-not-exist"), branch="main", python=None)
    e.settings.repos[0] = broken_repo
    e.repo_managers["app"] = RepoManager(e.paths, e.journal, e.db, broken_repo)
    e.settings.services[0].ready.timeout_s = 0.3

    result = await e.sequence().run()

    assert result.mode.production is False
    assert "repo_update_failed:app" in result.mode.reasons

    await e.supervisor.stop_ordered(["app"])
    e.close()
