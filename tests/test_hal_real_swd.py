import subprocess
from pathlib import Path

from robot_orchestrator.config import StlinkConfig
from robot_orchestrator.hal.real.usb import RealSwdProbe


class _FakeUsbInventory:
    def __init__(self, devices: list[dict]):
        self.devices = devices

    def list_devices(self) -> list[dict]:
        return self.devices


def _swd_probe(usb_inventory, runner=None) -> RealSwdProbe:
    return RealSwdProbe(
        config=StlinkConfig(usb_ids=["0483:374b"]),
        openocd_binary=Path("openocd"),
        scripts_dir=Path("/scripts"),
        interface_cfg="interface/stlink.cfg",
        target_cfg="target/stm32f4x.cfg",
        usb_inventory=usb_inventory,
        runner=runner,
    )


def test_stlink_present_true_when_matching_usb_id():
    usb = _FakeUsbInventory([{"vendor_id": "0483", "product_id": "374b"}])
    probe = _swd_probe(usb)

    assert probe.stlink_present() is True


def test_stlink_present_false_when_no_matching_usb_id():
    usb = _FakeUsbInventory([{"vendor_id": "1234", "product_id": "5678"}])
    probe = _swd_probe(usb)

    assert probe.stlink_present() is False


def test_target_present_true_on_exit_zero():
    usb = _FakeUsbInventory([{"vendor_id": "0483", "product_id": "374b"}])

    def fake_runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    probe = _swd_probe(usb, runner=fake_runner)

    assert probe.target_present() is True


def test_target_present_false_on_nonzero_exit():
    usb = _FakeUsbInventory([{"vendor_id": "0483", "product_id": "374b"}])

    def fake_runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="target not found")

    probe = _swd_probe(usb, runner=fake_runner)

    assert probe.target_present() is False


def test_target_present_false_on_timeout():
    usb = _FakeUsbInventory([{"vendor_id": "0483", "product_id": "374b"}])

    def fake_runner(argv, timeout):
        raise subprocess.TimeoutExpired(cmd=argv, timeout=timeout)

    probe = _swd_probe(usb, runner=fake_runner)

    assert probe.target_present() is False


def test_target_present_false_and_runner_never_called_when_no_stlink():
    usb = _FakeUsbInventory([])
    calls = []

    def fake_runner(argv, timeout):
        calls.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout="", stderr="")

    probe = _swd_probe(usb, runner=fake_runner)

    assert probe.target_present() is False
    assert calls == []
