from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


@dataclass
class ProbeResult:
    ok: bool
    detail: str = ""


@dataclass
class ArtifactRef:
    run_id: str
    download_url: str
    expired: bool = False


@dataclass
class FlashResult:
    ok: bool
    detail: str = ""


class Clock(Protocol):
    def now(self) -> float: ...
    def sleep(self, seconds: float) -> None: ...


class CameraProbe(Protocol):
    def probe(self, device: str) -> ProbeResult: ...


class MicProbe(Protocol):
    def probe(self) -> ProbeResult: ...


class NetProbe(Protocol):
    def probe(self) -> ProbeResult: ...


class UsbInventory(Protocol):
    def list_devices(self) -> list[dict]: ...


class SwdProbe(Protocol):
    def stlink_present(self) -> bool: ...
    def target_present(self) -> bool: ...


class GpioReader(Protocol):
    def read(
        self,
        chip: str,
        line: int,
        active_low: bool,
        bias: str,
        samples: int,
        interval_ms: int,
    ) -> bool: ...


class ArtifactClient(Protocol):
    def find_artifact(self, sha: str) -> ArtifactRef | None: ...
    def download(self, ref: ArtifactRef, dest_dir: Path) -> Path: ...


class FirmwareBuilder(Protocol):
    def build(self, release_dir: Path, pio_env: str, build_dir: Path) -> Path: ...


class Flasher(Protocol):
    def flash(self, image_path: Path, attempt: int) -> FlashResult: ...


class McuClient(Protocol):
    def ping(self) -> float | None: ...
    def version(self) -> dict | None: ...
