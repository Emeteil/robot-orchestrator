import subprocess
import sys

import pytest

from robot_orchestrator import bootlog
from robot_orchestrator.bootlog import BootEventLog, classify_level, logged_run, redact


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code]


def test_emit_assigns_increasing_seq_and_tail_returns_after():
    log = BootEventLog()
    first = log.emit("boot", "one")
    second = log.emit("boot", "two")

    assert (first["seq"], second["seq"]) == (1, 2)
    assert [e["text"] for e in log.tail(after_seq=1)] == ["two"]


def test_emit_skips_blank_text_and_strips_ansi():
    log = BootEventLog()

    assert log.emit("boot", "   ") is None
    event = log.emit("boot", "\x1b[31mred\x1b[0m")

    assert event["text"] == "red"


def test_ring_buffer_drops_oldest_events():
    log = BootEventLog(max_events=3)
    for i in range(5):
        log.emit("boot", f"line {i}")

    assert [e["text"] for e in log.tail()] == ["line 2", "line 3", "line 4"]


def test_subscribers_receive_events_and_can_unsubscribe():
    log = BootEventLog()
    seen = []
    log.subscribe(seen.append)
    log.emit("boot", "hello")
    log.unsubscribe(seen.append)
    log.emit("boot", "ignored")

    assert [e["text"] for e in seen] == ["hello"]


def test_a_failing_subscriber_does_not_break_emit():
    log = BootEventLog()

    def boom(_event):
        raise RuntimeError("nope")

    log.subscribe(boom)

    assert log.emit("boot", "still works")["text"] == "still works"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Traceback (most recent call last):", "error"),
        ("Fatal Python error: Segmentation fault", "error"),
        ("WARNING: retrying", "warn"),
        ("all good", "info"),
    ],
)
def test_classify_level(text, expected):
    assert classify_level(text) == expected


def test_redact_hides_url_credentials_tokens_and_bearer():
    assert "hunter2" not in redact("proxy http://user:hunter2@10.0.0.1:8080/x")
    assert "abc123" not in redact("GET /api/questions?token=abc123&x=1")
    assert "s3cr3tvalue" not in redact("password: s3cr3tvalue")
    assert "eyJhbGci" not in redact("Authorization: Bearer eyJhbGci.payload.sig")


def test_redact_keeps_harmless_text_untouched():
    text = "не хватает: GEMINI_API_KEY, GITHUB_PAT"
    assert redact(text) == text


def test_known_secret_values_are_masked_everywhere():
    log = BootEventLog()
    log.set_known_secrets(["super-secret-value", "abc"])

    event = log.emit("web-core", "connecting with super-secret-value now abc")

    assert "super-secret-value" not in event["text"]
    assert "abc" in event["text"]


def test_logged_run_streams_lines_and_returns_completed_process():
    log = BootEventLog()

    result = logged_run(_py("import sys; print('out1'); print('err1', file=sys.stderr)"), source="t", log=log)

    texts = [e["text"] for e in log.tail()]
    assert result.returncode == 0
    assert result.stdout.strip() == "out1"
    assert result.stderr.strip() == "err1"
    assert "out1" in texts and "err1" in texts
    assert texts[0].startswith("$ ")


def test_logged_run_check_raises_called_process_error_with_output():
    log = BootEventLog()

    with pytest.raises(subprocess.CalledProcessError) as excinfo:
        logged_run(_py("import sys; print('boom'); sys.exit(3)"), source="t", check=True, log=log)

    assert excinfo.value.returncode == 3
    assert "boom" in excinfo.value.stdout
    assert any("кодом 3" in e["text"] for e in log.tail())


def test_logged_run_timeout_kills_process_and_raises():
    log = BootEventLog()

    with pytest.raises(subprocess.TimeoutExpired):
        logged_run(_py("import time; print('start', flush=True); time.sleep(30)"), source="t", timeout=0.5, log=log)

    assert any("start" == e["text"] for e in log.tail())
    assert any("таймаут" in e["text"] for e in log.tail())


def test_logged_run_supports_shell_and_missing_binary_behaves_like_subprocess():
    log = BootEventLog()

    assert logged_run("echo hi", source="t", shell=True, log=log).stdout.strip() == "hi"
    with pytest.raises(OSError):
        logged_run(["definitely-not-a-real-binary-xyz"], source="t", log=log)


def test_logged_run_masks_given_values_in_command_and_output():
    log = BootEventLog()

    logged_run(_py("print('psk is hunter2secret')"), source="t", mask=("hunter2secret",), log=log)

    joined = " ".join(e["text"] for e in log.tail())
    assert "hunter2secret" not in joined


def test_logged_run_collapses_immediately_repeated_lines():
    log = BootEventLog()

    logged_run(_py("[print('same') for _ in range(5)]"), source="t", announce=False, log=log)

    assert [e["text"] for e in log.tail()] == ["same"]


def test_forward_service_line_parses_logsink_format_and_classifies():
    log = BootEventLog()

    bootlog.forward_service_line("web-core", "1790000000.123 stderr ERROR: db is down", log=log)

    event = log.tail()[0]
    assert (event["source"], event["level"], event["text"]) == ("web-core", "error", "ERROR: db is down")
