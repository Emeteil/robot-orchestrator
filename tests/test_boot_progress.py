from robot_orchestrator.boot.progress import DEFAULT_STEPS, FAILED, OK, PENDING, RUNNING, SKIPPED, WARN, BootProgress
from robot_orchestrator.bootlog import BootEventLog


def _progress():
    log = BootEventLog()
    return BootProgress(log=log), log


def test_all_default_steps_start_pending():
    progress, _ = _progress()

    assert [s.id for s in progress.steps] == [step_id for step_id, _title in DEFAULT_STEPS]
    assert all(s.status == PENDING for s in progress.steps)


def test_begin_marks_running_and_tracks_current_step():
    progress, log = _progress()

    progress.begin("camera", "ищу камеру")

    step = next(s for s in progress.steps if s.id == "camera")
    assert (step.status, step.detail) == (RUNNING, "ищу камеру")
    assert progress.step == "camera"
    assert log.tail()[-1]["level"] == "step"


def test_finish_records_status_detail_and_duration():
    progress, log = _progress()
    progress.begin("microphone")

    progress.finish("microphone", FAILED, "no matching input device")

    step = next(s for s in progress.steps if s.id == "microphone")
    assert step.status == FAILED
    assert step.detail == "no matching input device"
    assert step.finished_at >= step.started_at
    assert log.tail()[-1]["level"] == "error"


def test_skip_and_warn_use_matching_log_levels():
    progress, log = _progress()

    progress.skip("qr", "секреты уже введены")
    progress.finish("gpio", WARN, "перемычка замкнута")

    levels = [e["level"] for e in log.tail()]
    assert levels == ["dim", "warn"]
    assert next(s for s in progress.steps if s.id == "qr").status == SKIPPED


def test_snapshot_reports_percent_and_never_hits_100_until_complete():
    progress, _ = _progress()
    for step_id, _title in DEFAULT_STEPS:
        progress.finish(step_id, OK)

    assert progress.snapshot()["percent"] == 99

    progress.complete()
    snap = progress.snapshot()
    assert snap["percent"] == 100
    assert snap["step"] == "done"


def test_snapshot_counts_a_running_step_as_half_done():
    progress, _ = _progress()
    total = len(DEFAULT_STEPS)
    progress.begin("init")

    assert progress.snapshot()["percent"] == round(100 * 0.5 / total)


def test_snapshot_is_json_serialisable():
    import json

    progress, _ = _progress()
    progress.begin("init")

    json.dumps(progress.snapshot())
