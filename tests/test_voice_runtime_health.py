import time
from types import SimpleNamespace

from robot_orchestrator.boot.context import BootFacts
from robot_orchestrator.boot.decision import decide_mode
from robot_orchestrator.web.context import AdminContext


def current(state, age=0):
    facts = BootFacts(camera_ok=True, mic_ok=True, internet_ok=True, stlink_present=True, mcu_present=True)
    context = AdminContext.__new__(AdminContext)
    context.boot_result = SimpleNamespace(facts=facts, mode=decide_mode(facts), services_ready={})
    context.supervisor = SimpleNamespace(services={})
    context.voice_state = state
    context.voice_checked_at = time.monotonic() - age
    return context.current_boot()[0]


def test_disabled_voice_is_not_a_failure():
    assert current({"protocol_version": 2, "connected": True, "status": "disabled"}).production


def test_engine_failure_and_stale_monitor_are_nonprod():
    assert "voice_engine_failed" in current({"protocol_version": 2, "connected": True, "status": "error"}).reasons
    assert "voice_unavailable" in current({"protocol_version": 2, "connected": True}, age=30).reasons


def test_legacy_status_preserves_compatibility():
    assert current({"connected": True, "status": "idle"}, age=30).production


def test_recovery_removes_dynamic_reason():
    assert current({"protocol_version": 2, "connected": True, "status": "active"}).production
