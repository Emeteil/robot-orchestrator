from robot_orchestrator.boot.context import BootFacts


def healthy_facts() -> BootFacts:
    return BootFacts(
        camera_ok=True,
        mic_ok=True,
        internet_ok=True,
        stlink_present=True,
        mcu_present=True,
    )


def test_all_healthy_has_no_reasons():
    assert healthy_facts().reasons() == []


def test_default_facts_report_all_the_missing_checks_but_not_mcu_missing():
    reasons = BootFacts().reasons()
    assert "camera_unavailable" in reasons
    assert "mic_unavailable" in reasons
    assert "internet_down" in reasons
    assert "stlink_missing" in reasons
    assert "mcu_missing" not in reasons


def test_camera_unavailable_alone():
    facts = healthy_facts()
    facts.camera_ok = False
    assert facts.reasons() == ["camera_unavailable"]


def test_mic_unavailable_alone():
    facts = healthy_facts()
    facts.mic_ok = False
    assert facts.reasons() == ["mic_unavailable"]


def test_internet_down_alone():
    facts = healthy_facts()
    facts.internet_ok = False
    assert facts.reasons() == ["internet_down"]


def test_stlink_missing_does_not_also_report_mcu_missing():
    facts = healthy_facts()
    facts.stlink_present = False
    assert facts.reasons() == ["stlink_missing"]


def test_mcu_missing_reported_when_stlink_present_but_mcu_not():
    facts = healthy_facts()
    facts.mcu_present = False
    assert facts.reasons() == ["mcu_missing"]


def test_multiple_missing_secrets_each_produce_own_reason():
    facts = healthy_facts()
    facts.missing_required_secrets = ["GEMINI_API_KEY", "GITHUB_PAT"]
    reasons = facts.reasons()
    assert "secrets_missing:GEMINI_API_KEY" in reasons
    assert "secrets_missing:GITHUB_PAT" in reasons
    assert len(reasons) == 2


def test_multiple_repo_update_failures_each_produce_own_reason():
    facts = healthy_facts()
    facts.repo_update_failures = ["voice-interface", "web-core"]
    reasons = facts.reasons()
    assert "repo_update_failed:voice-interface" in reasons
    assert "repo_update_failed:web-core" in reasons
    assert len(reasons) == 2


def test_firmware_and_mcu_flags_each_produce_own_reason():
    facts = healthy_facts()
    facts.firmware_unavailable = True
    facts.mcu_flash_failed = True
    facts.mcu_verify_failed = True
    facts.mcu_client_incompatible = True
    reasons = facts.reasons()
    assert "firmware_unavailable" in reasons
    assert "mcu_flash_failed" in reasons
    assert "mcu_verify_failed" in reasons
    assert "mcu_client_incompatible" in reasons
    assert len(reasons) == 4


def test_gpio_forced_true_produces_reason():
    facts = healthy_facts()
    facts.gpio_forced = True
    assert facts.reasons() == ["gpio_forced"]


def test_gpio_forced_none_or_false_produce_no_reason():
    facts = healthy_facts()
    facts.gpio_forced = None
    assert facts.reasons() == []
    facts.gpio_forced = False
    assert facts.reasons() == []
