from dataclasses import dataclass
from pathlib import Path

from robot_orchestrator.boot.hardware import resolve_camera_device, resolve_mcu_port
from robot_orchestrator.config import CameraConfig, McuConfig
from robot_orchestrator.hal.base import ProbeResult
from robot_orchestrator.hal.fake.fakes import FakeUsbInventory
from robot_orchestrator.hal.fake.scenario import Scenario


def make_config(**overrides) -> McuConfig:
    defaults = dict(usb_ids=["0483:5740"], serial_glob="usb-STMicroelectronics*")
    defaults.update(overrides)
    return McuConfig(**defaults)


@dataclass
class _FakeCameraProbe:
    working_devices: set

    def probe(self, device: str) -> ProbeResult:
        return ProbeResult(ok=device in self.working_devices)


def test_resolves_single_by_id_candidate(tmp_path: Path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    target = by_id / "usb-STMicroelectronics_Virtual_COM_Port_ABC123"
    target.write_text("")

    usb = FakeUsbInventory(Scenario(name="s"))
    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id)

    assert result == str(target)


def test_no_by_id_and_no_fallback_match_returns_none(tmp_path: Path):
    by_id = tmp_path / "by-id"
    tty_dir = tmp_path / "dev"
    tty_dir.mkdir()
    usb = FakeUsbInventory(Scenario(name="s", usb={"stlink": False, "mcu_cdc": False, "swd_target": False}))

    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id, tty_dir=tty_dir)

    assert result is None


def test_fallback_to_ttyacm_when_by_id_empty_and_usb_id_matches(tmp_path: Path):
    by_id = tmp_path / "by-id"
    tty_dir = tmp_path / "dev"
    tty_dir.mkdir()
    tty = tty_dir / "ttyACM0"
    tty.write_text("")

    usb = FakeUsbInventory(Scenario(name="s"))

    def fake_udev(device: Path, key: str) -> str | None:
        if device == tty:
            return {"ID_VENDOR_ID": "0483", "ID_MODEL_ID": "5740"}[key]
        return None

    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id, tty_dir=tty_dir, udev_query=fake_udev)

    assert result == str(tty)


def test_fallback_ignores_ttyacm_with_non_matching_vid_pid(tmp_path: Path):
    by_id = tmp_path / "by-id"
    tty_dir = tmp_path / "dev"
    tty_dir.mkdir()
    tty = tty_dir / "ttyACM0"
    tty.write_text("")

    usb = FakeUsbInventory(Scenario(name="s"))

    def fake_udev(device: Path, key: str) -> str | None:
        return {"ID_VENDOR_ID": "1234", "ID_MODEL_ID": "5678"}[key]

    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id, tty_dir=tty_dir, udev_query=fake_udev)

    assert result is None


def test_multiple_by_id_candidates_prefers_previous_port(tmp_path: Path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    first = by_id / "usb-STMicroelectronics_A"
    second = by_id / "usb-STMicroelectronics_B"
    first.write_text("")
    second.write_text("")

    usb = FakeUsbInventory(Scenario(name="s"))
    result = resolve_mcu_port(make_config(), usb, previous_port=str(second), by_id_dir=by_id)

    assert result == str(second)


def test_multiple_by_id_candidates_without_previous_picks_deterministic_first(tmp_path: Path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    first = by_id / "usb-STMicroelectronics_A"
    second = by_id / "usb-STMicroelectronics_B"
    first.write_text("")
    second.write_text("")

    usb = FakeUsbInventory(Scenario(name="s"))
    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id)

    assert result == str(sorted([first, second])[0])


def test_missing_by_id_directory_falls_back_cleanly(tmp_path: Path):
    by_id = tmp_path / "does-not-exist"
    tty_dir = tmp_path / "dev"
    tty_dir.mkdir()

    usb = FakeUsbInventory(Scenario(name="s", usb={"stlink": False, "mcu_cdc": False, "swd_target": False}))
    result = resolve_mcu_port(make_config(), usb, by_id_dir=by_id, tty_dir=tty_dir)

    assert result is None


def test_resolve_camera_device_explicit_skips_probing_entirely(tmp_path: Path):
    config = CameraConfig(device="/dev/video7")
    probe = _FakeCameraProbe(working_devices=set())

    result = resolve_camera_device(config, probe, by_id_dir=tmp_path / "does-not-exist")

    assert result == "/dev/video7"


def test_resolve_camera_device_auto_picks_first_working_non_excluded(tmp_path: Path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    isp = by_id / "platform-rkisp0-video-index0"
    usb_cam = by_id / "usb-Generic_Webcam_ABC123"
    isp.write_text("")
    usb_cam.write_text("")

    config = CameraConfig(device="auto", exclude_substrings=["rkisp"])
    probe = _FakeCameraProbe(working_devices={str(usb_cam)})

    result = resolve_camera_device(config, probe, by_id_dir=by_id)

    assert result == str(usb_cam)


def test_resolve_camera_device_auto_no_candidates_returns_none(tmp_path: Path):
    config = CameraConfig(device="auto")
    probe = _FakeCameraProbe(working_devices=set())

    result = resolve_camera_device(config, probe, by_id_dir=tmp_path / "does-not-exist")

    assert result is None


def test_resolve_camera_device_auto_skips_excluded_even_if_it_would_work(tmp_path: Path):
    by_id = tmp_path / "by-id"
    by_id.mkdir()
    isp = by_id / "platform-rkisp0-video-index0"
    isp.write_text("")

    config = CameraConfig(device="auto", exclude_substrings=["rkisp"])
    probe = _FakeCameraProbe(working_devices={str(isp)})

    result = resolve_camera_device(config, probe, by_id_dir=by_id)

    assert result is None
