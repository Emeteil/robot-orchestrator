from robot_orchestrator.secrets.protocol import SecretsPayload, encode_payload
from robot_orchestrator.workers.qr_scan import process_frames


class _FakeClock:
    def __init__(self):
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _clocked_frames(items, clock: _FakeClock, step: float = 1.0):
    for item in items:
        clock.advance(step)
        yield item


def _run(frames, timeout_s: float = 600.0, preview_fps: float = 3.0):
    events = []
    result = process_frames(
        frames,
        timeout_s=timeout_s,
        preview_path=None,
        preview_fps=preview_fps,
        decode_frame=lambda frame: frame,
        emit=events.append,
        clock=lambda: 0.0,
    )
    return result, events


def test_single_part_payload_completes_immediately():
    payload = SecretsPayload(v=1, issued_at="2026-01-01T00:00:00Z", secrets={"GEMINI_API_KEY": "x"})
    part = encode_payload(payload)[0]

    result, events = _run([[], [part]])

    assert result == "complete"
    assert events[-1]["event"] == "complete"
    assert events[-1]["payload"]["secrets"] == {"GEMINI_API_KEY": "x"}


def test_unrelated_garbage_strings_are_ignored():
    payload = SecretsPayload(v=1, issued_at="2026-01-01T00:00:00Z", secrets={"GEMINI_API_KEY": "x"})
    part = encode_payload(payload)[0]

    result, events = _run([["not a real qr code"], [], [part]])

    assert result == "complete"
    assert [e for e in events if e["event"] == "part"] == []


def test_multi_part_payload_reports_progress_and_completes():
    payload = SecretsPayload(
        v=1,
        issued_at="2026-01-01T00:00:00Z",
        secrets={"GEMINI_API_KEY": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "GITHUB_PAT": "b" * 60},
    )
    parts = encode_payload(payload, max_bytes=10)
    assert len(parts) >= 3

    frames = [[]]
    for part in parts[:-1]:
        frames.append([part])
    frames.append([parts[-1]])

    result, events = _run(frames)

    assert result == "complete"
    part_events = [e for e in events if e["event"] == "part"]
    assert len(part_events) == len(parts) - 1
    for i, event in enumerate(part_events, start=1):
        assert event["received"] == i
        assert event["total"] == len(parts)
    assert events[-1]["event"] == "complete"


def test_duplicate_part_does_not_re_report_identical_progress():
    payload = SecretsPayload(
        v=1,
        issued_at="2026-01-01T00:00:00Z",
        secrets={"GEMINI_API_KEY": "a" * 60, "GITHUB_PAT": "b" * 60},
    )
    parts = encode_payload(payload, max_bytes=10)
    assert len(parts) >= 2

    frames = [[parts[0]], [parts[0]], [parts[0]], [parts[1]]]

    result, events = _run(frames)

    part_events = [e for e in events if e["event"] == "part"]
    first_part_reports = [e for e in part_events if e["received"] == 1]
    assert len(first_part_reports) == 1


def test_timeout_when_elapsed_exceeds_budget():
    clock = _FakeClock()
    frames = _clocked_frames([[] for _ in range(10)], clock, step=1.0)

    events = []
    result = process_frames(
        frames,
        timeout_s=3.5,
        preview_path=None,
        preview_fps=3.0,
        decode_frame=lambda frame: frame,
        emit=events.append,
        clock=clock,
    )

    assert result == "timeout"
    assert events[-1] == {"event": "timeout"}


def test_timeout_when_frames_exhausted_without_completion():
    events = []
    result = process_frames(
        [[], []],
        timeout_s=600.0,
        preview_path=None,
        preview_fps=3.0,
        decode_frame=lambda frame: frame,
        emit=events.append,
        clock=lambda: 0.0,
    )

    assert result == "timeout"
    assert events == [{"event": "timeout"}]
