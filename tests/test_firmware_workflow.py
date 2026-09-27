from datetime import datetime, timezone
from pathlib import Path

import pytest

from robot_orchestrator.config import FirmwareCiConfig, FirmwareFlashConfig, FirmwareTargetConfig, FirmwareVerifyConfig
from robot_orchestrator.firmware.workflow import FirmwareWorkflow
from robot_orchestrator.hal.base import ArtifactRef, FlashResult
from robot_orchestrator.hal.fake.fakes import FakeArtifactClient, FakeFirmwareBuilder, FakeFlasher, FakeMcuClient, FakeSwdProbe
from robot_orchestrator.hal.fake.scenario import Scenario, UsbScenario
from robot_orchestrator.paths import Paths
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal import recovery
from robot_orchestrator.wal.journal import Journal

_MONTH_NAMES = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]


def _format_build_fields(dt: datetime) -> tuple[str, str]:
    month = _MONTH_NAMES[dt.month - 1]
    date_str = f"{month} {dt.day:2d} {dt.year:04d}"
    time_str = f"{dt.hour:02d}:{dt.minute:02d}:{dt.second:02d}"
    return date_str, time_str


def _write_firmware_files(directory: Path, date_str: str | None = None, time_str: str | None = None) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    if date_str is not None and time_str is not None:
        date_field = date_str.encode("ascii").ljust(12, b"\x00")
        time_field = time_str.encode("ascii").ljust(9, b"\x00")
        blob = b"\xde\xad" * 4 + date_field + b"\x00" + time_field + b"\xff" * 8
    else:
        blob = b"\x00" * 64
    (directory / "firmware.bin").write_bytes(blob)
    (directory / "firmware.elf").write_bytes(b"fake-elf-content")


def _target_config(**flash_overrides) -> FirmwareTargetConfig:
    return FirmwareTargetConfig(
        name="stm32",
        repo="com-link-RT",
        pio_env="black_f407ve",
        ci=FirmwareCiConfig(owner="Emeteil", repo="com-link-RT", workflow_file="build.yml", artifact_name="firmware-black_f407ve"),
        flash=FirmwareFlashConfig(**flash_overrides),
        verify=FirmwareVerifyConfig(client_repo="web-core", client_subdir="com_link_rt", local_build_tolerance_s=120.0),
    )


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    paths.ensure()
    db = Database(paths.state_db)
    journal = Journal(db, boot_id="boot-1")
    yield paths, db, journal
    db.close()


def _swd(stlink: bool = True, target: bool = True) -> FakeSwdProbe:
    return FakeSwdProbe(Scenario(name="s", usb=UsbScenario(stlink=stlink, swd_target=target)))


def _workflow(env, target, artifact_client=None, builder=None, flasher=None, mcu_client=None, swd_probe=None) -> FirmwareWorkflow:
    paths, db, journal = env
    return FirmwareWorkflow(
        target=target,
        paths=paths,
        journal=journal,
        db=db,
        swd_probe=swd_probe or _swd(),
        artifact_client=artifact_client or FakeArtifactClient(),
        builder=builder or FakeFirmwareBuilder(),
        flasher=flasher or FakeFlasher(),
        mcu_client_factory=lambda: mcu_client or FakeMcuClient(),
    )


def test_stlink_absent_is_skipped_without_touching_journal_or_state(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    workflow = _workflow(env, target, swd_probe=_swd(stlink=False))

    outcome = workflow.run("sha1", release_dir=tmp_path / "release")

    assert outcome.state == "skipped"
    assert outcome.reason == "stlink_missing"
    assert journal.active_ops() == []
    assert db.conn.execute("SELECT COUNT(*) FROM journal").fetchone()[0] == 0
    assert store.get_flash_state(db.conn, target.name) is None


def test_target_absent_is_skipped_with_mcu_missing(env, tmp_path):
    target = _target_config()
    workflow = _workflow(env, target, swd_probe=_swd(stlink=True, target=False))

    outcome = workflow.run("sha1", release_dir=tmp_path / "release")

    assert outcome.state == "skipped"
    assert outcome.reason == "mcu_missing"


def test_already_up_to_date_never_calls_flasher(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, flashed_sha="sha1", dirty=0, verified=1)

    flasher = FakeFlasher()
    workflow = _workflow(env, target, flasher=flasher)

    outcome = workflow.run("sha1", release_dir=tmp_path / "release")

    assert outcome.state == "up_to_date"
    assert flasher.calls == []


def test_artifact_flash_and_exact_match_verify_records_flashed_state(env, tmp_path):
    paths, db, journal = env
    target = _target_config()

    build_date, build_time = "Sep 27 2026", "18:45:03"
    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir, build_date, build_time)

    artifact_client = FakeArtifactClient(
        ref=ArtifactRef(run_id="42", download_url="https://x/download"), image_dir=image_dir
    )
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=4.0, version={"build_date": build_date, "build_time": build_time})

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher, mcu_client=mcu_client)

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "flashed"
    assert outcome.reason is None
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["method"] == "ci_artifact"
    assert flash_state["flashed_sha"] == "target-sha"
    assert flash_state["verified"] == 1
    assert flash_state["dirty"] == 0
    assert flash_state["expected_build_date"] == build_date
    assert flash_state["reported_build_date"] == build_date


def test_local_build_flash_and_tolerance_window_verify_records_flashed_state(env, tmp_path):
    paths, db, journal = env
    target = _target_config()

    build_dir_output = tmp_path / "build_output"
    _write_firmware_files(build_dir_output)

    now = datetime.now(timezone.utc)
    reported_date, reported_time = _format_build_fields(now)
    builder = FakeFirmwareBuilder(output_dir=build_dir_output)
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=4.0, version={"build_date": reported_date, "build_time": reported_time})

    workflow = _workflow(
        env, target, artifact_client=FakeArtifactClient(ref=None), builder=builder, flasher=flasher, mcu_client=mcu_client
    )

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "flashed"
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["method"] == "local_build"
    assert flash_state["expected_build_date"] is None
    assert flash_state["flashed_sha"] == "target-sha"
    assert flash_state["dirty"] == 0
    assert flash_state["verified"] == 1


def test_neither_artifact_nor_local_build_available_leaves_state_untouched(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, flashed_sha="old-sha", dirty=0, verified=1)

    workflow = _workflow(
        env, target, artifact_client=FakeArtifactClient(ref=None), builder=FakeFirmwareBuilder(should_fail=True)
    )

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "failed"
    assert outcome.reason == "firmware_unavailable"
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["flashed_sha"] == "old-sha"
    assert flash_state["dirty"] == 0


def test_flash_fails_all_attempts_leaves_dirty_flag_set(env, tmp_path):
    paths, db, journal = env
    target = _target_config(attempts=2)

    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir)
    artifact_client = FakeArtifactClient(ref=ArtifactRef(run_id="1", download_url="x"), image_dir=image_dir)
    flasher = FakeFlasher(results=[FlashResult(ok=False, detail="fail1"), FlashResult(ok=False, detail="fail2")])

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher)

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "failed"
    assert outcome.reason == "mcu_flash_failed"
    assert len(flasher.calls) == 2
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["dirty"] == 1
    assert flash_state["flashed_sha"] is None


def test_flash_succeeds_but_exact_match_verify_fails_is_client_incompatible(env, tmp_path):
    paths, db, journal = env
    target = _target_config()

    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir, "Sep 27 2026", "18:45:03")
    artifact_client = FakeArtifactClient(ref=ArtifactRef(run_id="1", download_url="x"), image_dir=image_dir)
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=4.0, version={"build_date": "Sep 26 2026", "build_time": "18:45:03"})

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher, mcu_client=mcu_client)

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "failed"
    assert outcome.reason == "mcu_client_incompatible"
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["dirty"] == 1


def test_flash_succeeds_but_device_never_responds_is_verify_failed(env, tmp_path):
    paths, db, journal = env
    target = _target_config()

    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir)
    artifact_client = FakeArtifactClient(ref=ArtifactRef(run_id="1", download_url="x"), image_dir=image_dir)
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=None, version=None)

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher, mcu_client=mcu_client)

    outcome = workflow.run("target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "failed"
    assert outcome.reason == "mcu_verify_failed"
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["dirty"] == 1


def test_force_reflash_ignores_decide_and_reflashes_even_when_up_to_date(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, flashed_sha="sha1", dirty=0, verified=1)

    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir)
    artifact_client = FakeArtifactClient(ref=ArtifactRef(run_id="1", download_url="x"), image_dir=image_dir)
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=4.0, version={"build_date": "Jan  1 2026", "build_time": "00:00:00"})

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher, mcu_client=mcu_client)

    outcome = workflow.force_reflash("sha1", release_dir=tmp_path / "release")

    assert outcome.state == "flashed"
    assert len(flasher.calls) == 1


def test_force_reflash_still_reports_skipped_when_mcu_missing(env, tmp_path):
    target = _target_config()
    workflow = _workflow(env, target, swd_probe=_swd(stlink=True, target=False))

    outcome = workflow.force_reflash("sha1", release_dir=tmp_path / "release")

    assert outcome.state == "skipped"
    assert outcome.reason == "mcu_missing"


def test_verify_only_succeeds_and_clears_dirty_flag_without_touching_flasher(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, flashed_sha="sha1", dirty=1, verified=0)

    flasher = FakeFlasher()
    mcu_client = FakeMcuClient(ping_ms=3.0, version={"build_date": "Jan  1 2026", "build_time": "00:00:00"})
    workflow = _workflow(env, target, flasher=flasher, mcu_client=mcu_client)

    outcome = workflow.verify_only()

    assert outcome.state == "verified"
    assert flasher.calls == []
    flash_state = store.get_flash_state(db.conn, target.name)
    assert flash_state["verified"] == 1
    assert flash_state["dirty"] == 0


def test_verify_only_fails_when_device_does_not_respond(env, tmp_path):
    paths, db, journal = env
    target = _target_config()
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, flashed_sha="sha1", dirty=0, verified=1)

    mcu_client = FakeMcuClient(ping_ms=None, version=None)
    workflow = _workflow(env, target, mcu_client=mcu_client)

    outcome = workflow.verify_only()

    assert outcome.state == "failed"
    assert outcome.reason == "mcu_verify_failed"


def test_verify_only_fails_when_nothing_has_ever_been_flashed(env, tmp_path):
    target = _target_config()
    workflow = _workflow(env, target)

    outcome = workflow.verify_only()

    assert outcome.state == "failed"
    assert outcome.reason == "mcu_verify_failed"


def test_verify_only_still_reports_skipped_when_stlink_missing(env, tmp_path):
    target = _target_config()
    workflow = _workflow(env, target, swd_probe=_swd(stlink=False))

    outcome = workflow.verify_only()

    assert outcome.state == "skipped"
    assert outcome.reason == "stlink_missing"


def test_crash_during_committing_is_recovered_and_forces_a_clean_reflash(env, tmp_path):
    paths, db, journal = env
    target = _target_config()

    op_id = journal.begin("firmware_flash", target.name, {"target": target.name})
    journal.prepared(op_id)
    journal.committing(op_id)
    with db.transaction() as conn:
        store.upsert_flash_state(conn, target.name, dirty=1, flashed_sha=None, verified=0)

    report = recovery.run(db, journal)

    assert report.abandoned == [op_id]
    recovered_state = store.get_flash_state(db.conn, target.name)
    assert recovered_state["dirty"] == 1
    assert recovered_state["flashed_sha"] is None
    assert journal.get(op_id).phase == "abandoned"

    image_dir = tmp_path / "artifact_image"
    _write_firmware_files(image_dir)
    artifact_client = FakeArtifactClient(ref=ArtifactRef(run_id="7", download_url="x"), image_dir=image_dir)
    flasher = FakeFlasher(results=[FlashResult(ok=True)])
    mcu_client = FakeMcuClient(ping_ms=2.0, version={"build_date": "Jan  1 2026", "build_time": "00:00:00"})

    workflow = _workflow(env, target, artifact_client=artifact_client, flasher=flasher, mcu_client=mcu_client)
    outcome = workflow.run("new-target-sha", release_dir=tmp_path / "release")

    assert outcome.state == "flashed"
    assert len(flasher.calls) == 1
    final_state = store.get_flash_state(db.conn, target.name)
    assert final_state["dirty"] == 0
    assert final_state["flashed_sha"] == "new-target-sha"
