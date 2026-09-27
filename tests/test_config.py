from pathlib import Path

from robot_orchestrator.config import Settings, deep_merge, load_settings

REPO_ROOT = Path(__file__).resolve().parents[1]


def test_default_settings_yml_loads_and_validates():
    settings = load_settings(REPO_ROOT / "settings.yml")
    assert isinstance(settings, Settings)
    names = [r.name for r in settings.repos]
    assert names == ["web-core", "voice-interface", "com-link-RT"]
    assert settings.gpio.enabled is False
    assert settings.hardware.camera.device == "auto"


def test_local_override_deep_merges_without_dropping_siblings(tmp_path: Path):
    local = tmp_path / "settings.local.yml"
    local.write_text("gpio:\n  enabled: true\n  chip: /dev/gpiochip1\n  line: 11\n")
    settings = load_settings(REPO_ROOT / "settings.yml", local)
    assert settings.gpio.enabled is True
    assert settings.gpio.chip == "/dev/gpiochip1"
    assert settings.gpio.line == 11
    assert settings.gpio.is_configured() is True
    assert settings.hardware.mcu.usb_ids == ["0483:5740"]


def test_deep_merge_replaces_lists_not_concatenates():
    base = {"a": [1, 2, 3], "b": {"c": 1}}
    override = {"a": [9], "b": {"d": 2}}
    merged = deep_merge(base, override)
    assert merged == {"a": [9], "b": {"c": 1, "d": 2}}


def test_voice_interface_repo_has_pip_no_deps_for_openwakeword():
    settings = load_settings(REPO_ROOT / "settings.yml")
    voice = next(r for r in settings.repos if r.name == "voice-interface")
    assert voice.python.pip_no_deps == ["openwakeword"]


def test_gpio_not_configured_by_default():
    settings = load_settings(REPO_ROOT / "settings.yml")
    assert settings.gpio.is_configured() is False
