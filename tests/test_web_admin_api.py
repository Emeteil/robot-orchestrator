import os
import socket
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from robot_orchestrator.config import (
    FirmwareCiConfig,
    FirmwareTargetConfig,
    FirmwareVerifyConfig,
    ReadyProbeConfig,
    RepoConfig,
    RestartPolicyConfig,
    SelfUpdateConfig,
    ServiceConfig,
    Settings,
    StopConfig,
)
from robot_orchestrator.firmware.workflow import FirmwareWorkflow
from robot_orchestrator.hal.base import ArtifactRef
from robot_orchestrator.hal.fake.fakes import (
    FakeArtifactClient,
    FakeFirmwareBuilder,
    FakeFlasher,
    FakeMcuClient,
    FakeSwdProbe,
)
from robot_orchestrator.hal.fake.scenario import Scenario, UsbScenario
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RepoManager
from robot_orchestrator.selfupdate.manager import SelfUpdateManager
from robot_orchestrator.state.store import Database
from robot_orchestrator.supervisor.supervisor import Supervisor
from robot_orchestrator.wal.journal import Journal
from robot_orchestrator.web import auth
from robot_orchestrator.web.app import create_app
from robot_orchestrator.web.context import AdminContext

FIXTURES = Path(__file__).parent / "fixtures"
BASE_ENV = {**os.environ, "PYTHONUNBUFFERED": "1"}


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


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


def _commit_all(path: Path, message: str) -> str:
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path)


class Env:
    def __init__(self, tmp_path: Path):
        self.tmp_path = tmp_path
        self.paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
        self.paths.ensure()
        auth.ensure_default_admin(self.paths, default_password="admin")
        self.db = Database(self.paths.state_db)
        self.journal = Journal(self.db, boot_id="boot-1")

        self.src = tmp_path / "src"
        self.sha = _init_repo(self.src, {"main.py": "print(1)\n"})
        self.repo_cfg = RepoConfig(name="demo-repo", url=str(self.src), branch="main", python=None)
        self.repo_manager = RepoManager(self.paths, self.journal, self.db, self.repo_cfg)
        self.repo_manager.sync()

        self.port = free_port()
        self.service_cfg = ServiceConfig(
            name="demo",
            cmd=[sys.executable, str(FIXTURES / "tcp_listener.py"), str(self.port)],
            ready=ReadyProbeConfig(type="tcp", url=f"127.0.0.1:{self.port}", timeout_s=5.0),
            stop=StopConfig(signal="SIGINT", timeout_s=3.0),
            restart=RestartPolicyConfig(
                backoff_initial_s=0.05, backoff_max_s=0.2, stable_after_s=5.0, max_in_window=3, window_s=2.0,
            ),
        )
        self.supervisor = Supervisor(log_dir=self.paths.log_dir)
        self.supervisor.register(self.service_cfg, cwd=tmp_path, env=BASE_ENV)

        image_dir = tmp_path / "firmware-image"
        image_dir.mkdir()
        (image_dir / "firmware.bin").write_bytes(b"\x00" * 32)
        (image_dir / "firmware.elf").write_bytes(b"\x7fELF")
        self.target = FirmwareTargetConfig(
            name="stm32", repo="demo-repo", pio_env="black_f407ve",
            ci=FirmwareCiConfig(owner="x", repo="y", workflow_file="build.yml", artifact_name="z"),
            verify=FirmwareVerifyConfig(client_repo="demo-repo", client_subdir="com_link_rt"),
        )
        self.mcu_client = FakeMcuClient(ping_ms=3.0, version={"build_date": "Jan  1 2026", "build_time": "00:00:00"})
        self.workflow = FirmwareWorkflow(
            target=self.target, paths=self.paths, journal=self.journal, db=self.db,
            swd_probe=FakeSwdProbe(Scenario(name="s", usb=UsbScenario(stlink=True, swd_target=True))),
            artifact_client=FakeArtifactClient(ref=ArtifactRef(run_id="1", download_url="x"), image_dir=image_dir),
            builder=FakeFirmwareBuilder(),
            flasher=FakeFlasher(),
            mcu_client_factory=lambda: self.mcu_client,
        )

        self.unsynced_target = FirmwareTargetConfig(
            name="stm32-unreleased", repo="never-synced-repo", pio_env="black_f407ve",
            ci=FirmwareCiConfig(owner="x", repo="y", workflow_file="build.yml", artifact_name="z"),
            verify=FirmwareVerifyConfig(client_repo="demo-repo", client_subdir="com_link_rt"),
        )
        self.unsynced_workflow = FirmwareWorkflow(
            target=self.unsynced_target, paths=self.paths, journal=self.journal, db=self.db,
            swd_probe=FakeSwdProbe(Scenario(name="s", usb=UsbScenario(stlink=True, swd_target=True))),
            artifact_client=FakeArtifactClient(), builder=FakeFirmwareBuilder(), flasher=FakeFlasher(),
            mcu_client_factory=lambda: FakeMcuClient(),
        )

        self.self_update_src = tmp_path / "self-src"
        self.self_update_sha1 = _init_repo(
            self.self_update_src, {"main.py": "print(1)\n", "requirements.txt": ""}
        )
        self_update_venv_calls: list = []

        def fake_venv_builder(venv_path: Path, release_dir: Path) -> None:
            self_update_venv_calls.append((venv_path, release_dir))
            venv_path.mkdir(parents=True, exist_ok=True)

        def fake_validator(venv_path: Path, release_dir: Path):
            return True, ""

        self.self_update_manager = SelfUpdateManager(
            self.paths, self.journal, self.db,
            SelfUpdateConfig(enabled=True, url=str(self.self_update_src), branch="main"),
            venv_builder=fake_venv_builder, validator=fake_validator,
        )
        self.db.set_meta("self_update.current_sha", self.self_update_sha1)

        self.settings = Settings(
            repos=[self.repo_cfg], services=[self.service_cfg],
            firmware_targets=[self.target, self.unsynced_target],
            self_update=SelfUpdateConfig(enabled=True, url=str(self.self_update_src), branch="main"),
        )
        self.restart_calls = 0
        self.context = AdminContext(
            settings=self.settings, paths=self.paths, db=self.db, journal=self.journal,
            repo_managers={"demo-repo": self.repo_manager},
            firmware_workflows={"stm32": self.workflow, "stm32-unreleased": self.unsynced_workflow},
            supervisor=self.supervisor, self_update_manager=self.self_update_manager,
            request_restart=self._on_restart,
        )
        self.app = create_app(self.paths, jwt_secret="test-secret", jwt_ttl_s=600, context=self.context)

    def _on_restart(self) -> None:
        self.restart_calls += 1

    async def start_service(self) -> None:
        await self.supervisor.start_ordered(["demo"], capabilities={})

    async def close(self) -> None:
        await self.supervisor.stop_ordered(["demo"])
        self.db.close()

    def client(self) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app)
        return httpx.AsyncClient(transport=transport, base_url="http://testserver")


@pytest.fixture
async def env(tmp_path):
    e = Env(tmp_path)
    await e.start_service()
    yield e
    await e.close()


async def _token(client: httpx.AsyncClient) -> str:
    response = await client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    return response.json()["access_token"]


async def _auth_headers(client: httpx.AsyncClient) -> dict:
    return {"Authorization": f"Bearer {await _token(client)}"}


async def test_admin_routes_require_auth(env):
    async with env.client() as client:
        response = await client.get("/api/services")
        assert response.status_code == 401


async def test_state_reflects_no_boot_result_yet(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/state", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"boot": None}


async def test_services_list_shows_registered_service_as_ready(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/services", headers=headers)
        assert response.status_code == 200
        services = response.json()
        assert any(s["name"] == "demo" and s["state"] == "ready" and s["pid"] is not None for s in services)


async def test_service_stop_then_start_round_trips(env):
    async with env.client() as client:
        headers = await _auth_headers(client)

        stop_response = await client.post("/api/services/demo/stop", headers=headers)
        assert stop_response.json() == {"stopped": True}
        assert env.supervisor.services["demo"].state.value == "stopped"

        start_response = await client.post("/api/services/demo/start", headers=headers)
        assert start_response.json()["started"] is True
        assert env.supervisor.services["demo"].state.value == "ready"


async def test_unknown_service_returns_404(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/services/does-not-exist/stop", headers=headers)
        assert response.status_code == 404


async def test_repos_list_shows_synced_repo(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/repos", headers=headers)
        assert response.status_code == 200
        repos = response.json()
        assert repos[0]["name"] == "demo-repo"
        assert repos[0]["current_sha"] == env.sha


async def test_repos_check_reports_no_pending_update(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/repos/demo-repo/check", headers=headers)
        assert response.status_code == 200
        assert response.json()["pending"] is False


async def test_repos_apply_picks_up_new_commit(env):
    (env.src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(env.src, "second")

    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/repos/demo-repo/apply", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["changed"] is True
        assert body["sha"] == sha2


async def test_repos_rollback_without_previous_release_returns_false(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/repos/demo-repo/rollback", headers=headers)
        assert response.status_code == 200
        assert response.json()["rolled_back"] is False


async def test_unknown_repo_returns_404(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/repos", headers=headers)
        assert response.status_code == 200
        response = await client.post("/api/repos/does-not-exist/check", headers=headers)
        assert response.status_code == 404


async def test_firmware_list_and_reverify_after_manual_flash_state(env):
    from robot_orchestrator.state import store

    with env.db.transaction() as conn:
        store.upsert_flash_state(conn, "stm32", flashed_sha="abc123", dirty=1, verified=0)

    async with env.client() as client:
        headers = await _auth_headers(client)
        list_response = await client.get("/api/firmware", headers=headers)
        assert list_response.status_code == 200
        assert list_response.json()[0]["name"] == "stm32"

        reverify_response = await client.post("/api/firmware/stm32/reverify", headers=headers)
        assert reverify_response.status_code == 200
        assert reverify_response.json()["state"] == "verified"


async def test_firmware_reflash_without_release_returns_409(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/firmware/stm32-unreleased/reflash", headers=headers)
        assert response.status_code == 409


async def test_firmware_reflash_forces_flash_even_when_up_to_date(env):
    from robot_orchestrator.state import store

    with env.db.transaction() as conn:
        store.upsert_flash_state(conn, "stm32", flashed_sha=env.sha, dirty=0, verified=1)

    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/firmware/stm32/reflash", headers=headers)
        assert response.status_code == 200
        assert response.json()["state"] == "flashed"


async def test_unknown_firmware_target_returns_404(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/firmware/does-not-exist/reverify", headers=headers)
        assert response.status_code == 404


async def test_logs_tail_contains_startup_lines(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/logs/demo?lines=50", headers=headers)
        assert response.status_code == 200
        lines = response.json()["lines"]
        assert all(item["service"] == "demo" for item in lines)
        joined = "\n".join(item["text"] for item in lines)
        assert "listening" in joined


async def test_logs_unknown_service_returns_404(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/logs/does-not-exist", headers=headers)
        assert response.status_code == 404


async def test_journal_lists_recorded_operations(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/journal?limit=10", headers=headers)
        assert response.status_code == 200
        op_types = {row["op_type"] for row in response.json()}
        assert "repo_swap" in op_types


async def test_secrets_endpoint_reports_missing_required_then_present_after_import(env):
    env.settings.secrets.required = ["GEMINI_API_KEY"]
    async with env.client() as client:
        headers = await _auth_headers(client)

        before = await client.get("/api/secrets", headers=headers)
        assert before.json()["required"] == [{"key": "GEMINI_API_KEY", "present": False}]

        import_response = await client.post(
            "/api/secrets/import", headers=headers,
            json={"v": 1, "issued_at": "2026-09-28T00:00:00Z", "secrets": {"GEMINI_API_KEY": "x"}},
        )
        assert import_response.status_code == 200

        after = await client.get("/api/secrets", headers=headers)
        assert after.json()["required"] == [{"key": "GEMINI_API_KEY", "present": True}]


async def test_secrets_import_rejects_invalid_payload(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/secrets/import", headers=headers, json={"not": "valid"})
        assert response.status_code == 400


async def test_secrets_rescan_without_scanner_returns_503(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/secrets/rescan", headers=headers)
        assert response.status_code == 503


async def test_wifi_endpoint_without_manager_returns_empty_visible_list(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/wifi", headers=headers)
        assert response.status_code == 200
        assert response.json() == {"known_ssids": [], "visible_ssids": []}


async def test_wifi_connect_without_manager_returns_503(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/wifi/connect", headers=headers)
        assert response.status_code == 503


async def test_change_password_then_relogin_with_new_password(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post(
            "/api/settings/password", headers=headers,
            json={"current_password": "admin", "new_password": "new-secret-pw"},
        )
        assert response.status_code == 200

        login = await client.post("/api/auth/login", json={"username": "admin", "password": "new-secret-pw"})
        assert login.status_code == 200


async def test_change_password_rejects_wrong_current_password(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post(
            "/api/settings/password", headers=headers,
            json={"current_password": "wrong", "new_password": "new-secret-pw"},
        )
        assert response.status_code == 401


async def test_self_update_status_reports_current_sha(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/self-update", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["enabled"] is True
        assert body["current_sha"] == env.self_update_sha1
        assert body["staged_sha"] is None


async def test_self_update_check_stages_new_commit(env):
    (env.self_update_src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(env.self_update_src, "second")

    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/self-update/check", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert body["staged_sha"] == sha2
        assert body["error"] is None


async def test_self_update_apply_flips_pointer_and_requests_restart(env):
    from robot_orchestrator.wal.atomic import ReleasePointer

    (env.self_update_src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(env.self_update_src, "second")

    async with env.client() as client:
        headers = await _auth_headers(client)
        await client.post("/api/self-update/check", headers=headers)

        response = await client.post("/api/self-update/apply", headers=headers)
        assert response.status_code == 200
        assert response.json()["applied"] is True

    assert env.restart_calls == 1
    assert ReleasePointer(env.paths.self_current_pointer).read() == str(env.paths.self_release(sha2[:12]))


async def test_self_update_apply_without_staged_release_does_not_restart(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/self-update/apply", headers=headers)
        assert response.status_code == 200
        assert response.json()["applied"] is False

    assert env.restart_calls == 0


async def test_system_restart_invokes_callback(env):
    async with env.client() as client:
        headers = await _auth_headers(client)
        response = await client.post("/api/system/restart", headers=headers)
        assert response.status_code == 200
        assert env.restart_calls == 1


async def test_admin_routes_return_503_when_context_is_none(tmp_path):
    paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    paths.ensure()
    auth.ensure_default_admin(paths, default_password="admin")
    app = create_app(paths, jwt_secret="s", jwt_ttl_s=600, context=None)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
        headers = await _auth_headers(client)
        response = await client.get("/api/services", headers=headers)
        assert response.status_code == 503


async def test_log_stream_requires_auth_via_header_or_query_token(env):
    async with env.client() as client:
        unauthenticated = await client.get("/api/logs/demo/stream")
        assert unauthenticated.status_code == 401

        garbage_query_token = await client.get("/api/logs/demo/stream?token=not-a-real-token")
        assert garbage_query_token.status_code == 401
        # the unbounded generator itself can't be exercised here: httpx's ASGITransport buffers the
        # whole in-process call before returning, so it would hang forever on a never-ending stream
