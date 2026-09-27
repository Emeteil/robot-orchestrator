from dataclasses import dataclass

from robot_orchestrator.config import FirmwareFlashConfig, Settings
from robot_orchestrator.hal.base import CameraProbe, Clock, GpioReader, MicProbe, NetProbe, SwdProbe, UsbInventory
from robot_orchestrator.hal.fake.scenario import Scenario
from robot_orchestrator.paths import Paths


@dataclass
class Hal:
    clock: Clock
    camera_probe: CameraProbe
    mic_probe: MicProbe
    net_probe: NetProbe
    usb_inventory: UsbInventory
    swd_probe: SwdProbe
    gpio_reader: GpioReader


def build_hal(settings: Settings, paths: Paths, scenario: Scenario | None = None) -> Hal:
    if scenario is not None:
        from robot_orchestrator.hal.fake.fakes import (
            FakeCameraProbe,
            FakeClock,
            FakeGpioReader,
            FakeMicProbe,
            FakeNetProbe,
            FakeSwdProbe,
            FakeUsbInventory,
        )

        return Hal(
            clock=FakeClock(),
            camera_probe=FakeCameraProbe(scenario),
            mic_probe=FakeMicProbe(scenario),
            net_probe=FakeNetProbe(scenario),
            usb_inventory=FakeUsbInventory(scenario),
            swd_probe=FakeSwdProbe(scenario),
            gpio_reader=FakeGpioReader(scenario),
        )

    from robot_orchestrator.hal.real.camera import RealCameraProbe
    from robot_orchestrator.hal.real.clock import RealClock
    from robot_orchestrator.hal.real.gpio import RealGpioReader
    from robot_orchestrator.hal.real.microphone import RealMicProbe
    from robot_orchestrator.hal.real.network import RealNetProbe
    from robot_orchestrator.hal.real.usb import RealSwdProbe, RealUsbInventory

    usb_inventory = RealUsbInventory()
    flash_defaults = FirmwareFlashConfig()
    openocd_pkg_dir = paths.pio_core_dir / "packages" / "tool-openocd"

    return Hal(
        clock=RealClock(),
        camera_probe=RealCameraProbe(),
        mic_probe=RealMicProbe(settings.hardware.microphone),
        net_probe=RealNetProbe(settings.network),
        usb_inventory=usb_inventory,
        swd_probe=RealSwdProbe(
            config=settings.hardware.stlink,
            openocd_binary=openocd_pkg_dir / "bin" / "openocd",
            scripts_dir=openocd_pkg_dir / "openocd" / "scripts",
            interface_cfg=flash_defaults.interface_cfg,
            target_cfg=flash_defaults.target_cfg,
            usb_inventory=usb_inventory,
        ),
        gpio_reader=RealGpioReader(),
    )
