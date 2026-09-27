import time
from dataclasses import dataclass, field

from robot_orchestrator.config import RestartPolicyConfig


@dataclass
class BackoffState:
    policy: RestartPolicyConfig
    current_delay: float = 0.0
    last_start_at: float | None = None
    recent_starts: list[float] = field(default_factory=list)
    failed_until: float | None = None
    failed_forever: bool = False

    def note_start(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        self.last_start_at = now
        self.recent_starts.append(now)
        cutoff = now - self.policy.window_s
        self.recent_starts = [t for t in self.recent_starts if t >= cutoff]

    def is_stable(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if self.last_start_at is None:
            return False
        return (now - self.last_start_at) >= self.policy.stable_after_s

    def on_exit(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if self.is_stable(now):
            self.current_delay = self.policy.backoff_initial_s
        else:
            self.current_delay = min(
                self.policy.backoff_max_s,
                max(self.policy.backoff_initial_s, self.current_delay * 2),
            )

    def has_exceeded_crash_loop_limit(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        cutoff = now - self.policy.window_s
        recent = [t for t in self.recent_starts if t >= cutoff]
        return len(recent) >= self.policy.max_in_window

    def enter_failed(self, now: float | None = None) -> None:
        now = now if now is not None else time.time()
        if self.policy.failed_cooldown_s <= 0:
            self.failed_forever = True
            self.failed_until = None
        else:
            self.failed_until = now + self.policy.failed_cooldown_s

    def is_failed(self, now: float | None = None) -> bool:
        now = now if now is not None else time.time()
        if self.failed_forever:
            return True
        if self.failed_until is None:
            return False
        return now < self.failed_until

    def clear_failed(self) -> None:
        self.failed_until = None
        self.failed_forever = False
        self.recent_starts = []
        self.current_delay = 0.0
