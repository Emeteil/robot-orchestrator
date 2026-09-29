import subprocess
import time
from pathlib import Path
from typing import Callable

from robot_orchestrator.bootlog import BOOT_LOG, logged_run
from robot_orchestrator.config import StlinkConfig
from robot_orchestrator.hal.base import UsbInventory

RunnerT = Callable[[list[str], float], subprocess.CompletedProcess]
TARGET_PRESENT_RETRY_ATTEMPTS = 5
TARGET_PRESENT_RETRY_DELAY_S = 1.5


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return logged_run(argv, source="swd", timeout=timeout)


def _read_attr(device_dir: Path, name: str) -> str | None:
    path = device_dir / name
    try:
        return path.read_text(encoding="utf-8", errors="replace").strip()
    except OSError:
        return None


class RealUsbInventory:
    def __init__(self, sysfs_root: Path = Path("/sys/bus/usb/devices")):
        self.sysfs_root = sysfs_root

    def list_devices(self) -> list[dict]:
        if not self.sysfs_root.exists():
            return []

        devices = []
        for device_dir in sorted(self.sysfs_root.glob("*")):
            vendor_id = _read_attr(device_dir, "idVendor")
            product_id = _read_attr(device_dir, "idProduct")
            if vendor_id is None or product_id is None:
                continue

            product = _read_attr(device_dir, "product")
            manufacturer = _read_attr(device_dir, "manufacturer")
            description = " ".join(part for part in (manufacturer, product) if part)

            entry = {"vendor_id": vendor_id, "product_id": product_id}
            if description:
                entry["description"] = description
            devices.append(entry)
        return devices


class RealSwdProbe:
    def __init__(
        self,
        config: StlinkConfig,
        openocd_binary: Path,
        scripts_dir: Path,
        interface_cfg: str,
        target_cfg: str,
        usb_inventory: UsbInventory,
        runner: RunnerT | None = None,
    ):
        self.config = config
        self.openocd_binary = openocd_binary
        self.scripts_dir = scripts_dir
        self.interface_cfg = interface_cfg
        self.target_cfg = target_cfg
        self.usb_inventory = usb_inventory
        self.runner = runner or _default_runner

    def stlink_present(self) -> bool:
        wanted = {ident.lower() for ident in self.config.usb_ids}
        for device in self.usb_inventory.list_devices():
            ident = f"{device.get('vendor_id', '').lower()}:{device.get('product_id', '').lower()}"
            if ident in wanted:
                return True
        return False

    def target_present(self) -> bool:
        if not self.stlink_present():
            return False

        for attempt in range(TARGET_PRESENT_RETRY_ATTEMPTS):
            if attempt > 0:
                BOOT_LOG.emit(
                    "swd", f"MCU не ответил, попытка {attempt + 1}/{TARGET_PRESENT_RETRY_ATTEMPTS}", "warn"
                )
                time.sleep(TARGET_PRESENT_RETRY_DELAY_S)
            if self._probe_target_once():
                return True
        return False

    def _probe_target_once(self) -> bool:
        argv = [
            str(self.openocd_binary),
            "-s", str(self.scripts_dir),
            "-f", self.interface_cfg,
            "-f", self.target_cfg,
            "-c", "init; targets; exit",
        ]
        try:
            result = self.runner(argv, 5.0)
        except (subprocess.TimeoutExpired, OSError):
            return False
        return result.returncode == 0
