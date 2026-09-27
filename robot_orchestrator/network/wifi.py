import subprocess
from typing import Callable

from robot_orchestrator.secrets.protocol import WifiCredential

RunnerT = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(argv, capture_output=True, text=True, timeout=timeout)


class WifiManager:
    def __init__(self, runner: RunnerT | None = None, timeout_s: float = 20.0):
        self.runner = runner or _default_runner
        self.timeout_s = timeout_s

    def list_visible_ssids(self) -> list[str]:
        try:
            result = self.runner(["nmcli", "-t", "-f", "SSID", "device", "wifi", "list"], self.timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        ssids: list[str] = []
        for line in result.stdout.splitlines():
            ssid = line.strip()
            if ssid and ssid not in ssids:
                ssids.append(ssid)
        return ssids

    def connect(self, ssid: str, psk: str) -> bool:
        try:
            result = self.runner(["nmcli", "device", "wifi", "connect", ssid, "password", psk], self.timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

    def try_known_networks(self, known: list[WifiCredential]) -> str | None:
        if not known:
            return None
        visible = set(self.list_visible_ssids())
        for credential in known:
            if credential.ssid in visible and self.connect(credential.ssid, credential.psk):
                return credential.ssid
        return None
