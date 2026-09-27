import subprocess

from robot_orchestrator.network.wifi import WifiManager
from robot_orchestrator.secrets.protocol import WifiCredential


class FakeRunner:
    def __init__(self, list_output: str = "", connect_ok: bool = True):
        self.list_output = list_output
        self.connect_ok = connect_ok
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str], timeout: float) -> subprocess.CompletedProcess:
        self.calls.append(argv)
        if argv[:4] == ["nmcli", "device", "wifi", "connect"]:
            return subprocess.CompletedProcess(argv, 0 if self.connect_ok else 1)
        return subprocess.CompletedProcess(argv, 0, stdout=self.list_output)


def test_list_visible_ssids_parses_and_dedupes():
    runner = FakeRunner(list_output="RobotWifi\nOtherNet\nRobotWifi\n\n")
    manager = WifiManager(runner=runner)

    assert manager.list_visible_ssids() == ["RobotWifi", "OtherNet"]


def test_list_visible_ssids_returns_empty_on_nonzero_exit():
    def runner(argv, timeout):
        return subprocess.CompletedProcess(argv, 1, stdout="ignored\n")

    manager = WifiManager(runner=runner)

    assert manager.list_visible_ssids() == []


def test_list_visible_ssids_returns_empty_when_nmcli_missing():
    def runner(argv, timeout):
        raise OSError("nmcli not found")

    manager = WifiManager(runner=runner)

    assert manager.list_visible_ssids() == []


def test_connect_returns_false_on_timeout():
    def runner(argv, timeout):
        raise subprocess.TimeoutExpired(argv, timeout)

    manager = WifiManager(runner=runner)

    assert manager.connect("ssid", "psk") is False


def test_try_known_networks_connects_to_first_visible_match():
    runner = FakeRunner(list_output="OtherNet\nRobotWifi\n")
    manager = WifiManager(runner=runner)
    known = [WifiCredential(ssid="NotThere", psk="x"), WifiCredential(ssid="RobotWifi", psk="secret")]

    connected = manager.try_known_networks(known)

    assert connected == "RobotWifi"
    assert runner.calls[-1] == ["nmcli", "device", "wifi", "connect", "RobotWifi", "password", "secret"]


def test_try_known_networks_returns_none_when_nothing_visible():
    runner = FakeRunner(list_output="SomeoneElse\n")
    manager = WifiManager(runner=runner)
    known = [WifiCredential(ssid="RobotWifi", psk="secret")]

    assert manager.try_known_networks(known) is None


def test_try_known_networks_returns_none_when_no_known_credentials():
    manager = WifiManager(runner=FakeRunner(list_output="RobotWifi\n"))

    assert manager.try_known_networks([]) is None


def test_try_known_networks_skips_visible_match_that_fails_to_connect():
    runner = FakeRunner(list_output="RobotWifi\n", connect_ok=False)
    manager = WifiManager(runner=runner)
    known = [WifiCredential(ssid="RobotWifi", psk="wrong")]

    assert manager.try_known_networks(known) is None
