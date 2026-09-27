import httpx
import pytest
from fastapi.testclient import TestClient

from robot_orchestrator.paths import Paths
from robot_orchestrator.web import auth
from robot_orchestrator.web.app import create_app


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
