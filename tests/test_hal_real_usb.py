from pathlib import Path

from robot_orchestrator.hal.real.usb import RealUsbInventory


def test_returns_empty_list_when_sysfs_root_missing(tmp_path: Path):
    inventory = RealUsbInventory(sysfs_root=tmp_path / "does-not-exist")

    assert inventory.list_devices() == []


def test_lists_devices_with_valid_vendor_and_product_ids(tmp_path: Path):
    sysfs_root = tmp_path / "usb"
    sysfs_root.mkdir()

    stlink_dir = sysfs_root / "1-1"
    stlink_dir.mkdir()
    (stlink_dir / "idVendor").write_text("0483\n")
    (stlink_dir / "idProduct").write_text("374b\n")
    (stlink_dir / "product").write_text("ST-Link\n")
    (stlink_dir / "manufacturer").write_text("STMicroelectronics\n")

    incomplete_dir = sysfs_root / "1-2"
    incomplete_dir.mkdir()
    (incomplete_dir / "idVendor").write_text("1234\n")

    inventory = RealUsbInventory(sysfs_root=sysfs_root)
    devices = inventory.list_devices()

    assert len(devices) == 1
    assert devices[0]["vendor_id"] == "0483"
    assert devices[0]["product_id"] == "374b"
    assert devices[0]["description"] == "STMicroelectronics ST-Link"


def test_device_without_product_or_manufacturer_has_no_description_key(tmp_path: Path):
    sysfs_root = tmp_path / "usb"
    sysfs_root.mkdir()

    bare_dir = sysfs_root / "1-3"
    bare_dir.mkdir()
    (bare_dir / "idVendor").write_text("0483")
    (bare_dir / "idProduct").write_text("5740")

    inventory = RealUsbInventory(sysfs_root=sysfs_root)
    devices = inventory.list_devices()

    assert devices == [{"vendor_id": "0483", "product_id": "5740"}]
