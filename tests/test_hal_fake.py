from pathlib import Path

import pytest

from robot_orchestrator.config import Settings
from robot_orchestrator.hal.factory import build_hal
from robot_orchestrator.hal.fake.fakes import (
    FakeCameraProbe,
    FakeClock,
    FakeGpioReader,
    FakeMicProbe,
    FakeNetProbe,
    FakeSwdProbe,
    FakeUsbInventory,
)
from robot_orchestrator.hal.fake.scenario import Scenario, load_scenario
from robot_orchestrator.paths import Paths


def test_scenario_constructs_directly_with_defaults():
    scenario = Scenario(name="all_healthy")
    assert scenario.camera.present is True
    assert scenario.gpio.forced is None
    assert scenario.gpio.error is False


def test_load_scenario_from_yaml_file(tmp_path: Path):
    path = tmp_path / "scenario.yml"
    path.write_text(
        "name: no_camera\ncamera: {present: false}\ninternet: {up: false}\n"
    )
    scenario = load_scenario(path)
    assert scenario.name == "no_camera"
    assert scenario.camera.present is False
    assert scenario.internet.up is False
    assert scenario.microphone.present is True


def test_fake_camera_probe_reflects_scenario():
    present = Scenario(name="s", camera={"present": True})
    absent = Scenario(name="s", camera={"present": False})
    assert FakeCameraProbe(present).probe("auto").ok is True
    assert FakeCameraProbe(absent).probe("auto").ok is False


def test_fake_mic_probe_reflects_scenario():
    usable = Scenario(name="s", microphone={"present": True, "usable": True})
    unusable = Scenario(name="s", microphone={"present": True, "usable": False})
    assert FakeMicProbe(usable).probe().ok is True
    assert FakeMicProbe(unusable).probe().ok is False


def test_fake_net_probe_reflects_scenario():
    up = Scenario(name="s", internet={"up": True})
    down = Scenario(name="s", internet={"up": False})
    assert FakeNetProbe(up).probe().ok is True
    assert FakeNetProbe(down).probe().ok is False


def test_fake_swd_probe_reflects_scenario():
    scenario = Scenario(name="s", usb={"stlink": True, "mcu_cdc": True, "swd_target": False})
    probe = FakeSwdProbe(scenario)
    assert probe.stlink_present() is True
    assert probe.target_present() is False


def test_fake_usb_inventory_returns_list():
    scenario = Scenario(name="s")
    devices = FakeUsbInventory(scenario).list_devices()
    assert isinstance(devices, list)


def test_fake_gpio_reader_raises_on_error_flag():
    scenario = Scenario(name="s", gpio={"forced": True, "error": True})
    reader = FakeGpioReader(scenario)
    with pytest.raises(RuntimeError):
        reader.read("gpiochip0", 5, True, "pull-up", 7, 5)


def test_fake_gpio_reader_returns_forced_value_when_true():
    scenario = Scenario(name="s", gpio={"forced": True, "error": False})
    reader = FakeGpioReader(scenario)
    assert reader.read("gpiochip0", 5, True, "pull-up", 7, 5) is True


def test_fake_gpio_reader_returns_false_when_forced_absent():
    scenario = Scenario(name="s", gpio={"forced": None})
    reader = FakeGpioReader(scenario)
    assert reader.read("gpiochip0", 5, True, "pull-up", 7, 5) is False


def test_fake_clock_sleep_is_instant_with_zero_time_scale():
    clock = FakeClock()
    clock.sleep(1000.0)
    assert clock.now() == 0.0


def test_build_hal_with_scenario_returns_working_bundle(tmp_path: Path):
    settings = Settings()
    paths = Paths(state_dir=tmp_path / "state", log_dir=tmp_path / "log", run_dir=tmp_path / "run")
    scenario = Scenario(name="s")
    hal = build_hal(settings, paths, scenario=scenario)
    assert hal.clock.now() == 0.0
    hal.clock.sleep(1.0)
    assert hal.camera_probe.probe("auto").ok is True
    assert hal.mic_probe.probe().ok is True
    assert hal.net_probe.probe().ok is True
    assert isinstance(hal.usb_inventory.list_devices(), list)
    assert hal.swd_probe.stlink_present() is True
    assert hal.gpio_reader.read("gpiochip0", 5, True, "pull-up", 7, 5) is False


def test_build_hal_without_scenario_builds_real_hal(tmp_path: Path):
    settings = Settings()
    paths = Paths(state_dir=tmp_path / "state", log_dir=tmp_path / "log", run_dir=tmp_path / "run")
    hal = build_hal(settings, paths, scenario=None)

    assert hal.clock.now() > 0.0
    assert hal.swd_probe.stlink_present() is False
    assert hal.usb_inventory.list_devices() == []
