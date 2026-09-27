import time
from dataclasses import dataclass
from pathlib import Path

from robot_orchestrator.hal.base import ArtifactRef, FlashResult, ProbeResult
from robot_orchestrator.hal.fake.scenario import Scenario


class FakeClock:
    def __init__(self, time_scale: float = 0.0):
        self.time_scale = time_scale
        self._t = 0.0

    def now(self) -> float:
        return self._t

    def sleep(self, seconds: float) -> None:
        if self.time_scale == 0.0:
            return
        self._t += seconds * self.time_scale


class FakeCameraProbe:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def probe(self, device: str) -> ProbeResult:
        if self.scenario.camera.present:
            return ProbeResult(ok=True, detail=f"camera {device} opened")
        return ProbeResult(ok=False, detail=f"camera {device} not present")


class FakeMicProbe:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def probe(self) -> ProbeResult:
        mic = self.scenario.microphone
        if mic.present and mic.usable:
            return ProbeResult(ok=True, detail="microphone usable")
        return ProbeResult(ok=False, detail="microphone unavailable")


class FakeNetProbe:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def probe(self) -> ProbeResult:
        if self.scenario.internet.up:
            return ProbeResult(ok=True, detail="internet reachable")
        return ProbeResult(ok=False, detail="internet unreachable")


class FakeUsbInventory:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def list_devices(self) -> list[dict]:
        devices = []
        usb = self.scenario.usb
        if usb.stlink:
            devices.append({"vendor_id": "0483", "product_id": "374b", "description": "ST-Link"})
        if usb.mcu_cdc:
            devices.append({"vendor_id": "0483", "product_id": "5740", "description": "MCU CDC"})
        return devices


class FakeSwdProbe:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def stlink_present(self) -> bool:
        return self.scenario.usb.stlink

    def target_present(self) -> bool:
        return self.scenario.usb.swd_target


class FakeGpioReader:
    def __init__(self, scenario: Scenario):
        self.scenario = scenario

    def read(
        self,
        chip: str,
        line: int,
        active_low: bool,
        bias: str,
        samples: int,
        interval_ms: int,
    ) -> bool:
        if self.scenario.gpio.error:
            raise RuntimeError("simulated gpio read error")
        return bool(self.scenario.gpio.forced)


@dataclass
class FakeBuildResult:
    output_dir: Path
    started_at: float
    finished_at: float


class FakeArtifactClient:
    def __init__(self, ref: ArtifactRef | None = None, image_dir: Path | None = None):
        self.ref = ref
        self.image_dir = image_dir
        self.find_calls: list[str] = []
        self.download_calls: list[ArtifactRef] = []

    def find_artifact(self, sha: str) -> ArtifactRef | None:
        self.find_calls.append(sha)
        return self.ref

    def download(self, ref: ArtifactRef, dest_dir: Path) -> Path:
        self.download_calls.append(ref)
        if self.image_dir is None:
            raise RuntimeError("fake artifact client has no image configured")
        return self.image_dir


class FakeFirmwareBuilder:
    def __init__(
        self,
        output_dir: Path | None = None,
        should_fail: bool = False,
        started_at: float | None = None,
        finished_at: float | None = None,
    ):
        self.output_dir = output_dir
        self.should_fail = should_fail
        self.started_at = started_at
        self.finished_at = finished_at
        self.build_calls: list[tuple[Path, str, Path]] = []

    def build(self, release_dir: Path, pio_env: str, build_dir: Path) -> FakeBuildResult:
        started_at = self.started_at if self.started_at is not None else time.time()
        self.build_calls.append((release_dir, pio_env, build_dir))
        if self.should_fail or self.output_dir is None:
            raise RuntimeError("fake firmware builder: build failed")
        finished_at = self.finished_at if self.finished_at is not None else time.time()
        return FakeBuildResult(output_dir=self.output_dir, started_at=started_at, finished_at=finished_at)


class FakeFlasher:
    def __init__(self, results: list[FlashResult] | None = None):
        self.results = results if results is not None else [FlashResult(ok=True)]
        self.calls: list[tuple[Path, int]] = []

    def flash(self, image_path: Path, attempt: int) -> FlashResult:
        self.calls.append((image_path, attempt))
        index = min(attempt - 1, len(self.results) - 1)
        return self.results[index]


class FakeMcuClient:
    def __init__(self, ping_ms: float | None = 5.0, version: dict | None = None):
        self.ping_ms = ping_ms
        self.version_info = version

    def ping(self) -> float | None:
        return self.ping_ms

    def version(self) -> dict | None:
        return self.version_info
