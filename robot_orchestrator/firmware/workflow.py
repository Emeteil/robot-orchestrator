import hashlib
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robot_orchestrator.config import FirmwareTargetConfig
from robot_orchestrator.firmware import image_meta
from robot_orchestrator.hal.base import ArtifactClient, FirmwareBuilder, Flasher, McuClient, SwdProbe
from robot_orchestrator.paths import Paths
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal.journal import Journal


@dataclass
class FlashOutcome:
    state: str
    reason: str | None = None


@dataclass
class _AcquiredFirmware:
    image_dir: Path
    method: str
    expected_build_date: str | None
    expected_build_time: str | None
    build_window_start_utc: float | None = None
    build_window_end_utc: float | None = None
    ci_run_id: str | None = None


@dataclass
class _FlashAttemptResult:
    ok: bool
    started_at: float
    finished_at: float


@dataclass
class _VerifyOutcome:
    ok: bool
    reason: str | None
    reported_build_date: str | None = None
    reported_build_time: str | None = None
    reported_ts_utc: float | None = None


class FirmwareWorkflow:
    def __init__(
        self,
        target: FirmwareTargetConfig,
        paths: Paths,
        journal: Journal,
        db: Database,
        swd_probe: SwdProbe,
        artifact_client: ArtifactClient,
        builder: FirmwareBuilder,
        flasher: Flasher,
        mcu_client_factory: Callable[[], McuClient],
    ):
        self.target = target
        self.paths = paths
        self.journal = journal
        self.db = db
        self.swd_probe = swd_probe
        self.artifact_client = artifact_client
        self.builder = builder
        self.flasher = flasher
        self.mcu_client_factory = mcu_client_factory

    def run(self, target_sha: str, release_dir: Path) -> FlashOutcome:
        detect_outcome = self._detect()
        if detect_outcome is not None:
            return detect_outcome

        prior_flash_state = store.get_flash_state(self.db.conn, self.target.name)
        if not self._decide(prior_flash_state, target_sha):
            return FlashOutcome(state="up_to_date")

        return self._acquire_flash_verify_record(target_sha, release_dir, prior_flash_state)

    def force_reflash(self, target_sha: str, release_dir: Path) -> FlashOutcome:
        detect_outcome = self._detect()
        if detect_outcome is not None:
            return detect_outcome

        prior_flash_state = store.get_flash_state(self.db.conn, self.target.name)
        return self._acquire_flash_verify_record(target_sha, release_dir, prior_flash_state)

    def _acquire_flash_verify_record(
        self, target_sha: str, release_dir: Path, prior_flash_state: dict | None
    ) -> FlashOutcome:
        acquired = self._acquire(target_sha, release_dir)
        if acquired is None:
            return FlashOutcome(state="failed", reason="firmware_unavailable")

        op_id = uuid.uuid4().hex
        image_path = acquired.image_dir / self.target.flash.image
        flash_attempt = self._flash(op_id, image_path)
        if not flash_attempt.ok:
            self._abandon(op_id, "flash failed after all configured attempts")
            return FlashOutcome(state="failed", reason="mcu_flash_failed")

        verify = self._verify(prior_flash_state, acquired)
        if not verify.ok:
            self._abandon(op_id, f"verify failed: {verify.reason}")
            return FlashOutcome(state="failed", reason=verify.reason)

        self._record(op_id, target_sha, image_path, acquired, flash_attempt, verify)
        return FlashOutcome(state="flashed")

    def verify_only(self) -> FlashOutcome:
        detect_outcome = self._detect()
        if detect_outcome is not None:
            return detect_outcome

        prior_flash_state = store.get_flash_state(self.db.conn, self.target.name)
        if prior_flash_state is None or not prior_flash_state.get("flashed_sha"):
            return FlashOutcome(state="failed", reason="mcu_verify_failed")

        client = self.mcu_client_factory()
        rtt = client.ping()
        version = client.version()
        if rtt is None or version is None:
            return FlashOutcome(state="failed", reason="mcu_verify_failed")

        with self.db.transaction() as conn:
            store.upsert_flash_state(conn, self.target.name, verified=1, dirty=0)
        return FlashOutcome(state="verified")

    def _detect(self) -> FlashOutcome | None:
        if not self.swd_probe.stlink_present():
            return FlashOutcome(state="skipped", reason="stlink_missing")
        if not self.swd_probe.target_present():
            return FlashOutcome(state="skipped", reason="mcu_missing")
        return None

    def _decide(self, flash_state: dict | None, target_sha: str) -> bool:
        if flash_state is None:
            return True
        if flash_state.get("dirty"):
            return True
        return flash_state.get("flashed_sha") != target_sha

    def _acquire(self, target_sha: str, release_dir: Path) -> _AcquiredFirmware | None:
        acquired = self._acquire_via_artifact(target_sha)
        if acquired is not None:
            return acquired
        return self._acquire_via_local_build(release_dir)

    def _acquire_via_artifact(self, target_sha: str) -> _AcquiredFirmware | None:
        try:
            ref = self.artifact_client.find_artifact(target_sha)
            if ref is None:
                return None
            staging_dir = self.paths.firmware_staging(uuid.uuid4().hex)
            image_dir = self.artifact_client.download(ref, staging_dir)
        except Exception:
            return None
        acquired = self._describe_acquired(image_dir, method="ci_artifact")
        acquired.ci_run_id = ref.run_id
        return acquired

    def _acquire_via_local_build(self, release_dir: Path) -> _AcquiredFirmware | None:
        try:
            build_dir = self.paths.firmware_staging(uuid.uuid4().hex)
            build_result = self.builder.build(release_dir, self.target.pio_env, build_dir)
        except Exception:
            return None
        acquired = self._describe_acquired(build_result.output_dir, method="local_build")
        acquired.build_window_start_utc = build_result.started_at
        acquired.build_window_end_utc = build_result.finished_at
        return acquired

    def _describe_acquired(self, image_dir: Path, method: str) -> _AcquiredFirmware:
        extracted = image_meta.extract_build_timestamp(image_dir / "firmware.bin")
        expected_date, expected_time = extracted if extracted else (None, None)
        return _AcquiredFirmware(
            image_dir=image_dir,
            method=method,
            expected_build_date=expected_date,
            expected_build_time=expected_time,
        )

    def _flash(self, op_id: str, image_path: Path) -> _FlashAttemptResult:
        self.journal.begin(
            "firmware_flash",
            self.target.name,
            {"target": self.target.name, "image": str(image_path)},
            op_id=op_id,
        )
        self.journal.prepared(op_id)

        self.journal.committing(op_id)
        with self.db.transaction() as conn:
            store.upsert_flash_state(conn, self.target.name, dirty=1, flashed_sha=None, verified=0)

        started_at = time.time()
        ok = False
        for attempt in range(1, self.target.flash.attempts + 1):
            result = self.flasher.flash(image_path, attempt=attempt)
            if result.ok:
                ok = True
                break
        finished_at = time.time()
        return _FlashAttemptResult(ok=ok, started_at=started_at, finished_at=finished_at)

    def _abandon(self, op_id: str, error: str) -> None:
        def mark_dirty(conn):
            store.upsert_flash_state(conn, self.target.name, dirty=1, flashed_sha=None, verified=0)

        self.journal.abandoned(op_id, error=error, state_update=mark_dirty)

    def _verify(self, prior_flash_state: dict | None, acquired: _AcquiredFirmware) -> _VerifyOutcome:
        client = self.mcu_client_factory()
        rtt = client.ping()
        version = client.version()
        if rtt is None or version is None:
            return _VerifyOutcome(ok=False, reason="mcu_verify_failed")

        reported_date = version.get("build_date")
        reported_time = version.get("build_time")

        reported_ts: float | None = None
        if reported_date and reported_time:
            try:
                reported_ts = image_meta.parse_build_timestamp_to_utc(reported_date, reported_time)
            except ValueError:
                reported_ts = None

        if acquired.expected_build_date is not None and acquired.expected_build_time is not None:
            ok = (
                reported_date == acquired.expected_build_date
                and reported_time == acquired.expected_build_time
            )
        elif acquired.method == "ci_artifact":
            prior_ts = (prior_flash_state or {}).get("reported_ts_utc")
            ok = reported_ts is not None and (prior_ts is None or reported_ts > prior_ts)
        else:
            tolerance = self.target.verify.local_build_tolerance_s
            start = acquired.build_window_start_utc
            end = acquired.build_window_end_utc
            ok = (
                reported_ts is not None
                and start is not None
                and end is not None
                and (start - tolerance) <= reported_ts <= (end + tolerance)
            )

        if not ok:
            return _VerifyOutcome(ok=False, reason="mcu_client_incompatible")

        return _VerifyOutcome(
            ok=True,
            reason=None,
            reported_build_date=reported_date,
            reported_build_time=reported_time,
            reported_ts_utc=reported_ts,
        )

    def _record(
        self,
        op_id: str,
        target_sha: str,
        image_path: Path,
        acquired: _AcquiredFirmware,
        flash_attempt: _FlashAttemptResult,
        verify: _VerifyOutcome,
    ) -> None:
        recorded_at = time.time()
        image_sha256 = hashlib.sha256(image_path.read_bytes()).hexdigest()

        def update(conn):
            store.upsert_flash_state(
                conn,
                self.target.name,
                flashed_sha=target_sha,
                method=acquired.method,
                ci_run_id=acquired.ci_run_id,
                image_sha256=image_sha256,
                expected_build_date=acquired.expected_build_date,
                expected_build_time=acquired.expected_build_time,
                reported_build_date=verify.reported_build_date,
                reported_build_time=verify.reported_build_time,
                reported_ts_utc=verify.reported_ts_utc,
                build_window_start_utc=acquired.build_window_start_utc,
                build_window_end_utc=acquired.build_window_end_utc,
                flash_started_at=flash_attempt.started_at,
                flash_finished_at=flash_attempt.finished_at,
                verified=1,
                dirty=0,
                client_sha=None,
            )
            store.insert_flash_history(
                conn,
                self.target.name,
                recorded_at,
                method=acquired.method,
                ci_run_id=acquired.ci_run_id,
                image_sha256=image_sha256,
                reported_build_date=verify.reported_build_date,
                reported_build_time=verify.reported_build_time,
                verified=1,
                dirty=0,
                error=None,
            )

        self.journal.committed(op_id, state_update=update)
