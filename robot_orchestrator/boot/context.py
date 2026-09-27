from dataclasses import dataclass, field


@dataclass
class BootFacts:
    camera_ok: bool = False
    mic_ok: bool = False
    internet_ok: bool = False
    stlink_present: bool = False
    mcu_present: bool = False
    missing_required_secrets: list[str] = field(default_factory=list)
    repo_update_failures: list[str] = field(default_factory=list)
    mcu_flash_failed: bool = False
    mcu_verify_failed: bool = False
    mcu_client_incompatible: bool = False
    firmware_unavailable: bool = False
    gpio_forced: bool | None = None

    def reasons(self) -> list[str]:
        reasons: list[str] = []
        if self.gpio_forced is True:
            reasons.append("gpio_forced")
        if not self.camera_ok:
            reasons.append("camera_unavailable")
        if not self.mic_ok:
            reasons.append("mic_unavailable")
        if not self.internet_ok:
            reasons.append("internet_down")
        if not self.stlink_present:
            reasons.append("stlink_missing")
        elif not self.mcu_present:
            reasons.append("mcu_missing")
        for repo in self.repo_update_failures:
            reasons.append(f"repo_update_failed:{repo}")
        for key in self.missing_required_secrets:
            reasons.append(f"secrets_missing:{key}")
        if self.firmware_unavailable:
            reasons.append("firmware_unavailable")
        if self.mcu_flash_failed:
            reasons.append("mcu_flash_failed")
        if self.mcu_verify_failed:
            reasons.append("mcu_verify_failed")
        if self.mcu_client_incompatible:
            reasons.append("mcu_client_incompatible")
        return reasons
