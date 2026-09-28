import shutil
import subprocess
import time
from pathlib import Path
from typing import Callable

from robot_orchestrator.config import CameraConfig, McuConfig
from robot_orchestrator.hal.base import CameraProbe, UsbInventory

UdevQueryT = Callable[[Path, str], "str | None"]
CAMERA_RESOLVE_RETRY_ATTEMPTS = 5
CAMERA_RESOLVE_RETRY_DELAY_S = 1.5


def resolve_chromium_bin(which: Callable[[str], "str | None"] = shutil.which) -> str:
    for candidate in ("chromium-browser", "chromium"):
        if which(candidate) is not None:
            return candidate
    return "chromium"


def resolve_camera_device(
    config: CameraConfig,
    probe: CameraProbe,
    by_id_dir: Path = Path("/dev/v4l/by-id"),
) -> str | None:
    if config.device != "auto":
        return config.device
    for attempt in range(CAMERA_RESOLVE_RETRY_ATTEMPTS):
        if attempt > 0:
            time.sleep(CAMERA_RESOLVE_RETRY_DELAY_S)
        device = _resolve_camera_device_once(config, probe, by_id_dir)
        if device is not None:
            return device
    return None


def _resolve_camera_device_once(config: CameraConfig, probe: CameraProbe, by_id_dir: Path) -> str | None:
    if not by_id_dir.exists():
        return None
    for candidate in sorted(by_id_dir.glob("*")):
        name_lower = candidate.name.lower()
        if any(excl.lower() in name_lower for excl in config.exclude_substrings):
            continue
        if probe.probe(str(candidate)).ok:
            return str(candidate)
    return None


def _glob_by_id(serial_glob: str, by_id_dir: Path) -> list[Path]:
    if not by_id_dir.exists():
        return []
    pattern = Path(serial_glob).name
    return sorted(by_id_dir.glob(pattern))


def _real_udev_property(device: Path, key: str) -> str | None:
    try:
        result = subprocess.run(
            ["udevadm", "info", "-q", "property", str(device)],
            capture_output=True, text=True, timeout=5.0,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith(f"{key}="):
            return line.split("=", 1)[1]
    return None


def _fallback_tty_candidates(
    config: McuConfig,
    usb_inventory: UsbInventory,
    tty_dir: Path,
    udev_query: UdevQueryT,
) -> list[Path]:
    devices = usb_inventory.list_devices()
    wanted = {v.lower() for v in config.usb_ids}
    matched_ids = {
        f"{d.get('vendor_id', '').lower()}:{d.get('product_id', '').lower()}"
        for d in devices
    } & wanted
    if not matched_ids:
        return []
    if not tty_dir.exists():
        return []
    candidates = []
    for tty in sorted(tty_dir.glob("ttyACM*")):
        vendor = udev_query(tty, "ID_VENDOR_ID")
        product = udev_query(tty, "ID_MODEL_ID")
        if vendor is None or product is None:
            continue
        ident = f"{vendor.lower()}:{product.lower()}"
        if ident in matched_ids:
            candidates.append(tty)
    return candidates


def resolve_mcu_port(
    config: McuConfig,
    usb_inventory: UsbInventory,
    previous_port: str | None = None,
    by_id_dir: Path = Path("/dev/serial/by-id"),
    tty_dir: Path = Path("/dev"),
    udev_query: UdevQueryT = _real_udev_property,
) -> str | None:
    by_id_candidates = _glob_by_id(config.serial_glob, by_id_dir)
    if len(by_id_candidates) == 1:
        return str(by_id_candidates[0])
    if len(by_id_candidates) > 1:
        if previous_port is not None and Path(previous_port) in by_id_candidates:
            return previous_port
        return str(sorted(by_id_candidates)[0])

    fallback_candidates = _fallback_tty_candidates(config, usb_inventory, tty_dir, udev_query)
    if len(fallback_candidates) == 1:
        return str(fallback_candidates[0])
    if len(fallback_candidates) > 1:
        if previous_port is not None and Path(previous_port) in fallback_candidates:
            return previous_port
        return str(sorted(fallback_candidates)[0])

    return None
