import subprocess
from typing import Callable

from robot_orchestrator.bootlog import logged_run
from robot_orchestrator.network.info import _split_terse
from robot_orchestrator.secrets.protocol import WifiCredential

RunnerT = Callable[[list[str], float], subprocess.CompletedProcess]


def _default_runner(argv: list[str], timeout: float) -> subprocess.CompletedProcess:
    passwords = tuple(argv[i + 1] for i, arg in enumerate(argv[:-1]) if arg == "password")
    return logged_run(argv, source="wifi", timeout=timeout, mask=passwords)


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

    def list_networks(self) -> list[dict]:
        try:
            result = self.runner(
                ["nmcli", "-t", "-f", "IN-USE,SSID,SIGNAL,SECURITY", "device", "wifi", "list"], self.timeout_s
            )
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        networks: dict[str, dict] = {}
        for line in result.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) < 4 or not fields[1]:
                continue
            item = {
                "ssid": fields[1], "signal": int(fields[2]) if fields[2].isdigit() else 0,
                "secured": fields[3] not in ("", "--"), "in_use": fields[0] == "*",
            }
            known = networks.get(item["ssid"])
            if known is None or (item["in_use"], item["signal"]) > (known["in_use"], known["signal"]):
                networks[item["ssid"]] = item
        return sorted(networks.values(), key=lambda n: (not n["in_use"], -n["signal"]))

    def saved_profiles(self) -> list[str]:
        try:
            result = self.runner(["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"], self.timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            return []
        if result.returncode != 0:
            return []
        names = []
        for line in result.stdout.splitlines():
            fields = _split_terse(line)
            if len(fields) >= 2 and fields[1] == "802-11-wireless":
                names.append(fields[0])
        return names

    def rescan(self) -> None:
        try:
            self.runner(["nmcli", "device", "wifi", "rescan"], self.timeout_s)
        except (OSError, subprocess.TimeoutExpired):
            pass

    def connect_saved(self, ssid: str) -> bool:
        try:
            result = self.runner(["nmcli", "connection", "up", "id", ssid], self.timeout_s * 2)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return result.returncode == 0

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
