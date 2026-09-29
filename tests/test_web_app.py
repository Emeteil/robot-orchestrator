import types

import httpx
import pytest
from fastapi.testclient import TestClient

from robot_orchestrator import bootlog
from robot_orchestrator.boot.progress import OK, BootProgress
from robot_orchestrator.bootlog import BootEventLog
from robot_orchestrator.paths import Paths
from robot_orchestrator.web import auth
from robot_orchestrator.web.app import _resume_position, boot_event_stream, create_app


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    p.ensure()
    auth.ensure_default_admin(p, default_password="admin")
    return p


@pytest.fixture
def app(paths):
    return create_app(paths, jwt_secret="test-secret", jwt_ttl_s=600)


@pytest.fixture
def client(app):
    return TestClient(app)


def loopback_client(app, host="127.0.0.1", port=123) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=(host, port))
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def test_login_with_correct_credentials_returns_token(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 200
    body = response.json()
    assert body["expires_in"] == 600
    assert isinstance(body["access_token"], str) and len(body["access_token"]) > 10


def test_login_with_wrong_password_returns_401(client):
    response = client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    assert response.status_code == 401


def test_login_throttled_after_repeated_failures(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "wrong"})
    response = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert response.status_code == 429


def test_protected_route_requires_token(client):
    response = client.get("/api/auth/whoami")
    assert response.status_code == 401


def test_protected_route_rejects_garbage_token(client):
    response = client.get("/api/auth/whoami", headers={"Authorization": "Bearer not-a-real-token"})
    assert response.status_code == 401


def test_protected_route_works_with_valid_token(client):
    login = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    token = login.json()["access_token"]
    response = client.get("/api/auth/whoami", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    assert response.json()["username"] == "admin"


def test_protected_route_rejects_token_signed_with_different_secret(client, paths):
    forged = auth.issue_token("wrong-secret", "admin", ttl_s=600)
    response = client.get("/api/auth/whoami", headers={"Authorization": f"Bearer {forged}"})
    assert response.status_code == 401


async def test_boot_route_allowed_from_loopback(app):
    async with loopback_client(app) as c:
        response = await c.get("/boot")
        assert response.status_code == 200


async def test_boot_route_rejected_from_non_loopback(app):
    async with loopback_client(app, host="203.0.113.5", port=51000) as c:
        response = await c.get("/boot")
        assert response.status_code == 403


async def test_boot_state_route_rejected_from_non_loopback(app):
    async with loopback_client(app, host="203.0.113.5", port=51000) as c:
        response = await c.get("/boot/api/state")
        assert response.status_code == 403


async def test_boot_state_route_allowed_from_loopback(app):
    async with loopback_client(app) as c:
        response = await c.get("/boot/api/state")
        assert response.status_code == 200


async def test_boot_page_serves_real_html_file(app):
    async with loopback_client(app) as c:
        response = await c.get("/boot")
        assert response.status_code == 200
        assert "text/html" in response.headers["content-type"]
        assert "robot-orchestrator" in response.text


def test_admin_page_serves_real_html_file_without_loopback_restriction(client):
    response = client.get("/admin")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]


async def test_qr_preview_returns_404_when_no_scan_has_happened(app):
    async with loopback_client(app) as c:
        response = await c.get("/boot/api/qr-preview")
        assert response.status_code == 404


async def test_qr_preview_rejected_from_non_loopback(app):
    async with loopback_client(app, host="203.0.113.5", port=51000) as c:
        response = await c.get("/boot/api/qr-preview")
        assert response.status_code == 403


async def test_qr_preview_serves_the_image_once_present(app, paths):
    paths.qr_preview.parent.mkdir(parents=True, exist_ok=True)
    paths.qr_preview.write_bytes(b"\xff\xd8\xff\xdb-fake-jpeg-bytes")
    async with loopback_client(app) as c:
        response = await c.get("/boot/api/qr-preview")
        assert response.status_code == 200
        assert response.headers["content-type"] == "image/jpeg"


async def test_boot_state_reports_steps_percent_and_epoch_while_booting(paths):
    progress = BootProgress(log=BootEventLog())
    progress.begin("init")
    context = types.SimpleNamespace(boot_progress=progress, boot_result=None, operator_url=None)
    app = create_app(paths, jwt_secret="s", context=context)

    async with loopback_client(app) as c:
        body = (await c.get("/boot/api/state")).json()

    assert body["state"] == "booting"
    assert body["step"] == "init"
    assert body["steps"][0]["status"] == "running"
    assert body["epoch"] == bootlog.BOOT_LOG.epoch
    assert 0 < body["percent"] < 100


async def test_boot_state_exposes_operator_url_and_finished_flag_when_done(paths):
    progress = BootProgress(log=BootEventLog())
    progress.finish("init", OK)
    progress.complete()
    boot_result = types.SimpleNamespace(
        mode=types.SimpleNamespace(production=False, reasons=["mic_unavailable"], capabilities={}),
        services_ready={"web-core": True},
    )
    context = types.SimpleNamespace(
        boot_progress=progress, boot_result=boot_result, operator_url="http://127.0.0.1:80/?token=abc",
    )
    app = create_app(paths, jwt_secret="s", context=context)

    async with loopback_client(app) as c:
        body = (await c.get("/boot/api/state")).json()

    assert body["state"] == "running"
    assert body["finished"] is True
    assert body["production"] is False
    assert body["reasons"] == ["mic_unavailable"]
    assert body["operator_url"] == "http://127.0.0.1:80/?token=abc"
    assert body["percent"] == 100


async def test_boot_stream_rejected_from_non_loopback(app):
    async with loopback_client(app, host="203.0.113.5", port=51000) as c:
        response = await c.get("/boot/api/stream")
        assert response.status_code == 403


def _request(headers=None, query=None):
    return types.SimpleNamespace(headers=headers or {}, query_params=query or {})


def test_resume_position_honours_matching_epoch_only():
    epoch = bootlog.BOOT_LOG.epoch

    assert _resume_position(_request({"last-event-id": f"{epoch}:41"})) == 41
    assert _resume_position(_request({"last-event-id": "someotherepoch:41"})) == 0
    assert _resume_position(_request({"last-event-id": "garbage"})) == 0
    assert _resume_position(_request(query={"last": f"{epoch}:7"})) == 7
    assert _resume_position(_request()) == 0


async def _never_disconnected() -> bool:
    return False


async def test_boot_event_stream_replays_backlog_then_follows_live_events():
    log = BootEventLog()
    log.emit("boot", "one")
    log.emit("boot", "two")

    stream = boot_event_stream(log, 0, _never_disconnected, keepalive_s=5.0)

    assert (await anext(stream)).startswith("retry:")
    first, second = await anext(stream), await anext(stream)
    assert f"id: {log.epoch}:1" in first and '"one"' in first
    assert '"two"' in second

    log.emit("git", "three")
    assert '"three"' in await anext(stream)

    await stream.aclose()
    assert log._subscribers == []


async def test_boot_event_stream_resumes_after_given_sequence():
    log = BootEventLog()
    log.emit("boot", "one")
    log.emit("boot", "two")

    stream = boot_event_stream(log, 1, _never_disconnected)
    await anext(stream)

    assert '"two"' in await anext(stream)
    await stream.aclose()


async def test_boot_event_stream_sends_keepalive_when_idle():
    log = BootEventLog()

    stream = boot_event_stream(log, 0, _never_disconnected, keepalive_s=0.05)
    await anext(stream)

    assert (await anext(stream)).startswith(": keepalive")
    await stream.aclose()
