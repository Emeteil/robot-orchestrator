from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field


def deep_merge(base: dict, override: dict) -> dict:
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class PathsConfig(BaseModel):
    state_dir: str = "/var/lib/robot-orchestrator"
    log_dir: str = "/var/log/robot-orchestrator"
    run_dir: str = "/run/robot-orchestrator"


class WebConfig(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    admin_user: str = "admin"
    jwt_ttl_s: int = 600


class GpioConfig(BaseModel):
    enabled: bool = False
    chip: str | None = None
    line: int | None = None
    active_low: bool = True
    bias: Literal["pull-up", "pull-down", "disabled"] = "pull-up"
    samples: int = 7
    sample_interval_ms: int = 5
    on_error: Literal["nonprod", "ignore"] = "nonprod"

    def is_configured(self) -> bool:
        return self.enabled and self.chip is not None and self.line is not None


class CameraConfig(BaseModel):
    device: str = "auto"
    probe_frames: int = 5
    probe_timeout_s: float = 8.0
    exclude_substrings: list[str] = Field(
        default_factory=lambda: ["rkisp", "hdmirx", "rkvdec", "rkcodec"]
    )


class MicrophoneConfig(BaseModel):
    alsa_card_match: str = "USB"
    probe_seconds: float = 1.0
    min_rms: float = 2.0


class StlinkConfig(BaseModel):
    usb_ids: list[str] = Field(
        default_factory=lambda: ["0483:3748", "0483:374b", "0483:374e", "0483:374f", "0483:3753", "0483:3754"]
    )


class McuConfig(BaseModel):
    usb_ids: list[str] = Field(default_factory=lambda: ["0483:5740"])
    serial_glob: str = "/dev/serial/by-id/usb-STMicroelectronics*"
    reenumerate_timeout_s: float = 15.0


class HardwareConfig(BaseModel):
    camera: CameraConfig = Field(default_factory=CameraConfig)
    microphone: MicrophoneConfig = Field(default_factory=MicrophoneConfig)
    stlink: StlinkConfig = Field(default_factory=StlinkConfig)
    mcu: McuConfig = Field(default_factory=McuConfig)


class NetworkConfig(BaseModel):
    probes: list[str] = Field(
        default_factory=lambda: [
            "https://api.github.com/zen",
            "http://connectivitycheck.gstatic.com/generate_204",
        ]
    )
    timeout_s: float = 5.0
    min_ok: int = 1


class SecretsConfig(BaseModel):
    required: list[str] = Field(default_factory=lambda: ["GEMINI_API_KEY", "GITHUB_PAT"])
    optional: list[str] = Field(
        default_factory=lambda: ["GEMINI_PROXY", "WEBCORE_ADMIN_PASSWORD", "ORCH_ADMIN_PASSWORD"]
    )


class QrConfig(BaseModel):
    scan_timeout_s: float = 600.0
    preview_fps: float = 3.0
    max_parts: int = 16


class PythonRequirementsConfig(BaseModel):
    requirements: list[str] = Field(default_factory=lambda: ["requirements.txt"])
    pip_no_deps: list[str] = Field(default_factory=list)


class RepoConfig(BaseModel):
    name: str
    url: str
    branch: str = "main"
    python: PythonRequirementsConfig | None = None
    persistent: dict[str, str] = Field(default_factory=dict)
    validate_cmds: list[str] = Field(default_factory=list)
    restart_on_change: list[str] = Field(default_factory=list)
    triggers_on_change: list[str] = Field(default_factory=list)


class ReadyProbeConfig(BaseModel):
    type: Literal["http", "http_json", "tcp", "none"] = "none"
    url: str | None = None
    path: str | None = None
    timeout_s: float = 60.0


class StopConfig(BaseModel):
    signal: str = "SIGINT"
    timeout_s: float = 10.0


class RestartPolicyConfig(BaseModel):
    backoff_initial_s: float = 1.0
    backoff_max_s: float = 60.0
    stable_after_s: float = 120.0
    max_in_window: int = 6
    window_s: float = 300.0
    failed_cooldown_s: float = 600.0


class DependsOnConfig(BaseModel):
    service: str
    condition: Literal["ready", "started"] = "ready"


class ServiceConfig(BaseModel):
    name: str
    repo: str | None = None
    cmd: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)
    secret_env: list[str] = Field(default_factory=list)
    depends_on: list[DependsOnConfig] = Field(default_factory=list)
    holds_devices: list[str] = Field(default_factory=list)
    ready: ReadyProbeConfig = Field(default_factory=ReadyProbeConfig)
    stop: StopConfig = Field(default_factory=StopConfig)
    restart: RestartPolicyConfig = Field(default_factory=RestartPolicyConfig)
    enabled_when: str | None = None
    phase: Literal["early", "main"] = "main"
    requires: list[str] = Field(default_factory=list)
    overlay: dict[str, Any] = Field(default_factory=dict)


class FirmwareCiConfig(BaseModel):
    owner: str
    repo: str
    workflow_file: str
    artifact_name: str
    wait_for_running_s: float = 300.0


class FirmwareFlashConfig(BaseModel):
    interface_cfg: str = "interface/stlink.cfg"
    target_cfg: str = "target/stm32f4x.cfg"
    image: str = "firmware.elf"
    attempts: int = 2
    timeout_s: float = 90.0


class FirmwareVerifyConfig(BaseModel):
    client_repo: str
    client_subdir: str
    local_build_tolerance_s: float = 120.0
    ping_tries: int = 3


class FirmwareTargetConfig(BaseModel):
    name: str
    repo: str
    pio_env: str
    ci: FirmwareCiConfig
    flash: FirmwareFlashConfig = Field(default_factory=FirmwareFlashConfig)
    verify: FirmwareVerifyConfig
    requires_stopped: list[str] = Field(default_factory=list)


class KioskConfig(BaseModel):
    operator_url: str = "http://127.0.0.1:80/?token={secret.MASTER_TOKEN}"


class UpdatesConfig(BaseModel):
    fetch_timeout_s: float = 120.0
    rollback_window_s: float = 120.0


class SelfUpdateConfig(BaseModel):
    enabled: bool = False
    url: str = "https://github.com/Emeteil/robot-orchestrator.git"
    branch: str = "main"
    fetch_timeout_s: float = 120.0


class Settings(BaseModel):
    paths: PathsConfig = Field(default_factory=PathsConfig)
    web: WebConfig = Field(default_factory=WebConfig)
    gpio: GpioConfig = Field(default_factory=GpioConfig)
    hardware: HardwareConfig = Field(default_factory=HardwareConfig)
    network: NetworkConfig = Field(default_factory=NetworkConfig)
    secrets: SecretsConfig = Field(default_factory=SecretsConfig)
    qr: QrConfig = Field(default_factory=QrConfig)
    updates: UpdatesConfig = Field(default_factory=UpdatesConfig)
    self_update: SelfUpdateConfig = Field(default_factory=SelfUpdateConfig)
    kiosk: KioskConfig = Field(default_factory=KioskConfig)
    repos: list[RepoConfig] = Field(default_factory=list)
    services: list[ServiceConfig] = Field(default_factory=list)
    firmware_targets: list[FirmwareTargetConfig] = Field(default_factory=list)


def load_yaml(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_settings(default_path: Path, local_path: Path | None = None) -> Settings:
    merged = load_yaml(default_path)
    if local_path is not None:
        merged = deep_merge(merged, load_yaml(local_path))
    return Settings.model_validate(merged)
