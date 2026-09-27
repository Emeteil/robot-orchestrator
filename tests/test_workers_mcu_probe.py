from robot_orchestrator.workers.mcu_probe import probe


class FakeConnection:
    def __init__(self, fail_count: int = 0, always_fail: bool = False):
        self.fail_count = fail_count
        self.always_fail = always_fail
        self.connect_calls = 0
        self.disconnect_calls = 0

    def connect(self) -> None:
        self.connect_calls += 1
        if self.always_fail or self.connect_calls <= self.fail_count:
            raise RuntimeError("simulated connect failure")

    def disconnect(self) -> None:
        self.disconnect_calls += 1


def _make_ping_cls(results: list):
    queue = list(results)

    class FakePingCommand:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, wait_response: bool = True, timeout: float = 2.0):
            return queue.pop(0)

    return FakePingCommand


def _make_version_cls(value):
    class FakeVersionCommand:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, wait_response: bool = True, timeout: float = 5.0):
            return value

    return FakeVersionCommand


def _clock_sleep(step: float = 1.0):
    state = {"t": 0.0}

    def clock() -> float:
        return state["t"]

    def sleep(seconds: float) -> None:
        state["t"] += step

    return clock, sleep


def test_connect_fails_every_retry_within_timeout_returns_connect_failed():
    connection = FakeConnection(always_fail=True)
    clock, sleep = _clock_sleep(step=5.0)

    result = probe(connection, _make_ping_cls([]), _make_version_cls(None), connect_timeout_s=12.0, ping_tries=3, sleep=sleep, clock=clock)

    assert result == {"ok": False, "reason": "connect_failed"}
    assert connection.disconnect_calls == 0


def test_connect_retries_then_succeeds_before_pinging():
    connection = FakeConnection(fail_count=2)
    clock, sleep = _clock_sleep(step=1.0)
    ping_cls = _make_ping_cls([9.0])
    version_cls = _make_version_cls({"build_date": "Sep 27 2026", "build_time": "18:45:03"})

    result = probe(connection, ping_cls, version_cls, connect_timeout_s=10.0, ping_tries=3, sleep=sleep, clock=clock)

    assert connection.connect_calls == 3
    assert result["ok"] is True
    assert connection.disconnect_calls == 1


def test_ping_always_none_returns_no_ping_response_and_disconnects_once():
    connection = FakeConnection()
    clock, sleep = _clock_sleep()
    ping_cls = _make_ping_cls([None, None, None])
    version_cls = _make_version_cls({"build_date": "x", "build_time": "y"})

    result = probe(connection, ping_cls, version_cls, connect_timeout_s=10.0, ping_tries=3, sleep=sleep, clock=clock)

    assert result == {"ok": False, "reason": "no_ping_response"}
    assert connection.disconnect_calls == 1


def test_version_none_after_successful_ping_returns_no_version_response():
    connection = FakeConnection()
    clock, sleep = _clock_sleep()
    ping_cls = _make_ping_cls([12.5])
    version_cls = _make_version_cls(None)

    result = probe(connection, ping_cls, version_cls, connect_timeout_s=10.0, ping_tries=3, sleep=sleep, clock=clock)

    assert result == {"ok": False, "reason": "no_version_response"}
    assert connection.disconnect_calls == 1


def test_ping_and_version_succeed_returns_ok_payload_with_real_ping_value():
    connection = FakeConnection()
    clock, sleep = _clock_sleep()
    ping_cls = _make_ping_cls([None, 8.25])
    version_cls = _make_version_cls({"build_date": "Sep 27 2026", "build_time": "18:45:03"})

    result = probe(connection, ping_cls, version_cls, connect_timeout_s=10.0, ping_tries=3, sleep=sleep, clock=clock)

    assert result == {
        "ok": True,
        "ping_ms": 8.25,
        "version": {"build_date": "Sep 27 2026", "build_time": "18:45:03"},
    }
    assert connection.disconnect_calls == 1
