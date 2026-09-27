from pathlib import Path

import yaml
from pydantic import BaseModel, Field


class GpioScenario(BaseModel):
    forced: bool | None = None
    error: bool = False


class CameraScenario(BaseModel):
    present: bool = True
    open_delay_s: float = 0.0


class MicrophoneScenario(BaseModel):
    present: bool = True
    usable: bool = True


class InternetScenario(BaseModel):
    up: bool = True


class UsbScenario(BaseModel):
    stlink: bool = True
    mcu_cdc: bool = True
    swd_target: bool = True


class FirmwareArtifactScenario(BaseModel):
    result: str = "success"


class FirmwareLocalBuildScenario(BaseModel):
    result: str = "success"
    duration_s: float = 0.0


class FirmwareTargetScenario(BaseModel):
    artifact: FirmwareArtifactScenario = Field(default_factory=FirmwareArtifactScenario)
    local_build: FirmwareLocalBuildScenario = Field(default_factory=FirmwareLocalBuildScenario)
    flash_results: list[str] = Field(default_factory=lambda: ["success"])
    version_after_flash: dict | None = None
    ping_ms: float | None = 5.0


class Scenario(BaseModel):
    name: str
    gpio: GpioScenario = Field(default_factory=GpioScenario)
    camera: CameraScenario = Field(default_factory=CameraScenario)
    microphone: MicrophoneScenario = Field(default_factory=MicrophoneScenario)
    internet: InternetScenario = Field(default_factory=InternetScenario)
    usb: UsbScenario = Field(default_factory=UsbScenario)
    firmware: dict[str, FirmwareTargetScenario] = Field(default_factory=dict)


def load_scenario(path: Path) -> Scenario:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    return Scenario.model_validate(data)
