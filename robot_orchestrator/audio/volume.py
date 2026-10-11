import os
import re
import subprocess
from pathlib import Path

DEFAULT_PULSE_SOCKET = "/tmp/pulse-socket"
_PERCENT = re.compile(r"(\d+)%")


def _env() -> dict:
    env = dict(os.environ)
    if "PULSE_SERVER" not in env and Path(DEFAULT_PULSE_SOCKET).exists():
        env["PULSE_SERVER"] = f"unix:{DEFAULT_PULSE_SOCKET}"
    return env


def _pactl(*args: str, timeout: float = 5.0) -> subprocess.CompletedProcess:
    return subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=timeout, env=_env())


def get_volume() -> dict | None:
    try:
        volume = _pactl("get-sink-volume", "@DEFAULT_SINK@")
        mute = _pactl("get-sink-mute", "@DEFAULT_SINK@")
    except (OSError, subprocess.TimeoutExpired):
        return None
    if volume.returncode != 0:
        return None
    levels = [int(m) for m in _PERCENT.findall(volume.stdout)]
    if not levels:
        return None
    return {"percent": max(levels), "muted": mute.returncode == 0 and "yes" in mute.stdout.lower()}


def set_volume(percent: int | None = None, muted: bool | None = None) -> dict | None:
    try:
        if percent is not None:
            _pactl("set-sink-volume", "@DEFAULT_SINK@", f"{max(0, min(100, int(percent)))}%")
        if muted is not None:
            _pactl("set-sink-mute", "@DEFAULT_SINK@", "1" if muted else "0")
    except (OSError, subprocess.TimeoutExpired):
        return None
    return get_volume()
