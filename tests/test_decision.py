from robot_orchestrator.boot.context import BootFacts
from robot_orchestrator.boot.decision import capability, decide_mode


def healthy_facts() -> BootFacts:
    return BootFacts(
        camera_ok=True,
        mic_ok=True,
        internet_ok=True,
        stlink_present=True,
        mcu_present=True,
    )


def test_all_healthy_is_production_with_all_capabilities():
    decision = decide_mode(healthy_facts())
    assert decision.production is True
    assert decision.reasons == []
    assert decision.capabilities == {"kiosk_browser": True, "wake_word": True, "voice_interface": True}


def test_camera_unavailable_alone_is_nonprod_but_keeps_voice_interface():
    facts = healthy_facts()
    facts.camera_ok = False
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["kiosk_browser"] is False
    assert decision.capabilities["wake_word"] is False
    assert decision.capabilities["voice_interface"] is True


def test_mic_unavailable_disables_voice_interface():
    facts = healthy_facts()
    facts.mic_ok = False
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["voice_interface"] is False


def test_internet_down_disables_voice_interface():
    facts = healthy_facts()
    facts.internet_ok = False
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["voice_interface"] is False


def test_missing_gemini_key_disables_voice_interface():
    facts = healthy_facts()
    facts.missing_required_secrets = ["GEMINI_API_KEY"]
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["voice_interface"] is False


def test_missing_other_secret_keeps_voice_interface_though_nonprod():
    facts = healthy_facts()
    facts.missing_required_secrets = ["GITHUB_PAT"]
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["voice_interface"] is True


def test_mcu_flash_failed_alone_is_nonprod_but_keeps_voice_interface():
    facts = healthy_facts()
    facts.mcu_flash_failed = True
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.capabilities["voice_interface"] is True


def test_stlink_missing_alone_is_nonprod():
    facts = healthy_facts()
    facts.stlink_present = False
    assert decide_mode(facts).production is False


def test_mcu_missing_alone_is_nonprod():
    facts = healthy_facts()
    facts.mcu_present = False
    assert decide_mode(facts).production is False


def test_repo_update_failure_alone_is_nonprod():
    facts = healthy_facts()
    facts.repo_update_failures = ["voice-interface"]
    assert decide_mode(facts).production is False


def test_gpio_forced_alone_is_nonprod_with_only_that_reason_and_keeps_voice_interface():
    facts = healthy_facts()
    facts.gpio_forced = True
    decision = decide_mode(facts)
    assert decision.production is False
    assert decision.reasons == ["gpio_forced"]
    assert decision.capabilities["voice_interface"] is True


def test_capability_helper_reads_from_decision():
    decision = decide_mode(healthy_facts())
    assert capability(decision, "kiosk_browser") is True
    assert capability(decision, "nonexistent") is False
