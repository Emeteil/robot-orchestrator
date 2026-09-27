import time

import jwt
import pytest

from robot_orchestrator.paths import Paths
from robot_orchestrator.web import auth


@pytest.fixture
def paths(tmp_path) -> Paths:
    p = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    p.ensure()
    return p


def test_ensure_default_admin_creates_with_given_defaults(paths):
    record = auth.ensure_default_admin(paths, username="admin", default_password="admin")
    assert record.username == "admin"
    assert auth.verify_login(paths, "admin", "admin") is True


def test_ensure_default_admin_is_idempotent(paths):
    first = auth.ensure_default_admin(paths)
    second = auth.ensure_default_admin(paths)
    assert first.hash_hex == second.hash_hex
    assert first.salt_hex == second.salt_hex


def test_verify_login_rejects_wrong_password(paths):
    auth.ensure_default_admin(paths, default_password="correct")
    assert auth.verify_login(paths, "admin", "wrong") is False
    assert auth.verify_login(paths, "admin", "correct") is True


def test_verify_login_rejects_unknown_username(paths):
    auth.ensure_default_admin(paths)
    assert auth.verify_login(paths, "someone-else", "admin") is False


def test_verify_login_with_no_admin_yet_returns_false(paths):
    assert auth.verify_login(paths, "admin", "admin") is False


def test_set_password_changes_login(paths):
    auth.ensure_default_admin(paths)
    auth.save_admin(paths, "admin", "new-password")
    assert auth.verify_login(paths, "admin", "admin") is False
    assert auth.verify_login(paths, "admin", "new-password") is True


def test_is_default_password_detection(paths):
    auth.ensure_default_admin(paths, default_password="admin")
    assert auth.is_default_password(paths) is True
    auth.save_admin(paths, "admin", "something-else")
    assert auth.is_default_password(paths) is False


def test_password_hash_is_scrypt_derived_not_plaintext(paths):
    auth.ensure_default_admin(paths, username="theuser", default_password="sekret-password")
    record = auth.load_admin(paths)
    assert record.hash_hex != "sekret-password".encode().hex()
    assert len(bytes.fromhex(record.salt_hex)) == 16
    assert len(bytes.fromhex(record.hash_hex)) == 64


def test_issue_and_verify_token_roundtrip():
    token = auth.issue_token("s3cr3t", "admin", ttl_s=600)
    claims = auth.verify_token("s3cr3t", token)
    assert claims["sub"] == "admin"
    assert claims["exp"] - claims["iat"] == 600
    assert "jti" in claims


def test_verify_token_rejects_wrong_secret():
    token = auth.issue_token("s3cr3t", "admin", ttl_s=600)
    with pytest.raises(auth.TokenInvalid):
        auth.verify_token("different-secret", token)


def test_verify_token_rejects_expired_token():
    now = int(time.time())
    token = jwt.encode({"sub": "admin", "iat": now - 700, "exp": now - 100, "jti": "x"}, "s3cr3t", algorithm="HS256")
    with pytest.raises(auth.TokenExpired):
        auth.verify_token("s3cr3t", token)


def test_verify_token_rejects_garbage():
    with pytest.raises(auth.TokenInvalid):
        auth.verify_token("s3cr3t", "not-a-jwt-at-all")


def test_login_throttle_blocks_after_max_failures():
    throttle = auth.LoginThrottle(max_failures=3, window_s=60.0)
    for _ in range(2):
        throttle.record_failure("1.2.3.4", now=1000.0)
    assert throttle.is_blocked("1.2.3.4", now=1000.0) is False
    throttle.record_failure("1.2.3.4", now=1000.0)
    assert throttle.is_blocked("1.2.3.4", now=1000.0) is True


def test_login_throttle_window_expires_old_failures():
    throttle = auth.LoginThrottle(max_failures=2, window_s=10.0)
    throttle.record_failure("1.2.3.4", now=0.0)
    throttle.record_failure("1.2.3.4", now=1.0)
    assert throttle.is_blocked("1.2.3.4", now=1.5) is True
    assert throttle.is_blocked("1.2.3.4", now=20.0) is False


def test_login_throttle_is_per_ip():
    throttle = auth.LoginThrottle(max_failures=1, window_s=60.0)
    throttle.record_failure("1.2.3.4", now=0.0)
    assert throttle.is_blocked("1.2.3.4", now=0.0) is True
    assert throttle.is_blocked("5.6.7.8", now=0.0) is False


def test_login_throttle_clear_resets():
    throttle = auth.LoginThrottle(max_failures=1, window_s=60.0)
    throttle.record_failure("1.2.3.4", now=0.0)
    assert throttle.is_blocked("1.2.3.4", now=0.0) is True
    throttle.clear("1.2.3.4")
    assert throttle.is_blocked("1.2.3.4", now=0.0) is False
