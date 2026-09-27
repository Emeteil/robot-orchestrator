from robot_orchestrator.config import RestartPolicyConfig
from robot_orchestrator.supervisor.backoff import BackoffState


def make_policy(**overrides) -> RestartPolicyConfig:
    defaults = dict(
        backoff_initial_s=1.0,
        backoff_max_s=60.0,
        stable_after_s=120.0,
        max_in_window=6,
        window_s=300.0,
        failed_cooldown_s=600.0,
    )
    defaults.update(overrides)
    return RestartPolicyConfig(**defaults)


def test_first_exit_uses_initial_delay():
    state = BackoffState(policy=make_policy())
    state.note_start(now=1000.0)
    state.on_exit(now=1001.0)
    assert state.current_delay == 1.0


def test_repeated_unstable_exits_double_delay():
    state = BackoffState(policy=make_policy())
    state.note_start(now=1000.0)
    state.on_exit(now=1000.5)
    assert state.current_delay == 1.0
    state.note_start(now=1001.5)
    state.on_exit(now=1002.0)
    assert state.current_delay == 2.0
    state.note_start(now=1002.5)
    state.on_exit(now=1003.0)
    assert state.current_delay == 4.0


def test_delay_capped_at_backoff_max():
    state = BackoffState(policy=make_policy(backoff_max_s=5.0))
    state.note_start(now=0.0)
    for i in range(10):
        state.on_exit(now=0.1 + i * 0.1)
        state.note_start(now=0.2 + i * 0.1)
    assert state.current_delay == 5.0


def test_stable_run_resets_delay():
    state = BackoffState(policy=make_policy(stable_after_s=10.0))
    state.note_start(now=0.0)
    state.on_exit(now=0.5)
    assert state.current_delay == 1.0
    state.note_start(now=1.0)
    state.on_exit(now=20.0)
    assert state.current_delay == 1.0


def test_crash_loop_limit_detected_within_window():
    state = BackoffState(policy=make_policy(max_in_window=3, window_s=10.0))
    for t in (0.0, 1.0, 2.0):
        state.note_start(now=t)
    assert state.has_exceeded_crash_loop_limit(now=2.5) is True


def test_crash_loop_limit_not_triggered_outside_window():
    state = BackoffState(policy=make_policy(max_in_window=3, window_s=10.0))
    state.note_start(now=0.0)
    state.note_start(now=1.0)
    state.note_start(now=100.0)
    assert state.has_exceeded_crash_loop_limit(now=100.5) is False


def test_enter_failed_with_positive_cooldown_expires():
    state = BackoffState(policy=make_policy(failed_cooldown_s=10.0))
    state.enter_failed(now=0.0)
    assert state.is_failed(now=5.0) is True
    assert state.is_failed(now=11.0) is False


def test_enter_failed_with_zero_cooldown_never_expires():
    state = BackoffState(policy=make_policy(failed_cooldown_s=0.0))
    state.enter_failed(now=0.0)
    assert state.is_failed(now=1_000_000.0) is True


def test_clear_failed_resets_everything():
    state = BackoffState(policy=make_policy(failed_cooldown_s=0.0))
    state.note_start(now=0.0)
    state.on_exit(now=0.1)
    state.enter_failed(now=0.1)
    state.clear_failed()
    assert state.is_failed(now=1.0) is False
    assert state.current_delay == 0.0
    assert state.recent_starts == []
