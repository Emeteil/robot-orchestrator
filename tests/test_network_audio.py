import subprocess

from robot_orchestrator.audio import volume
from robot_orchestrator.network import info
from robot_orchestrator.network.wifi import WifiManager


def _result(stdout="", code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


def test_split_terse_honours_escaped_colons():
    assert info._split_terse(r"*:My\:Net:77:WPA2") == ["*", "My:Net", "77", "WPA2"]


def test_wan_route_parses_interface_source_and_gateway(monkeypatch):
    out = "1.1.1.1 via 192.168.0.1 dev wlan0 src 192.168.0.33 uid 1000\n"
    monkeypatch.setattr(info, "_run", lambda argv, timeout=5.0: out)

    assert info.wan_route() == {"interface": "wlan0", "ip": "192.168.0.33", "gateway": "192.168.0.1"}


def test_wan_route_is_empty_without_a_route(monkeypatch):
    monkeypatch.setattr(info, "_run", lambda argv, timeout=5.0: "")

    assert info.wan_route() == {"interface": None, "ip": None, "gateway": None}


def test_list_networks_dedupes_ssids_and_puts_active_first():
    output = "\n".join([" :Cafe:40:--", "*:Home:80:WPA2", " :Home:55:WPA2", " :Guest:62:WPA2", " ::20:WPA2"])
    manager = WifiManager(runner=lambda argv, timeout: _result(output))

    networks = manager.list_networks()

    assert [n["ssid"] for n in networks] == ["Home", "Guest", "Cafe"]
    assert networks[0]["in_use"] and networks[0]["signal"] == 80
    assert networks[2]["secured"] is False


def test_saved_profiles_only_lists_wifi_connections():
    output = "Home:802-11-wireless\nWired connection 1:802-3-ethernet\nCafe:802-11-wireless"
    manager = WifiManager(runner=lambda argv, timeout: _result(output))

    assert manager.saved_profiles() == ["Home", "Cafe"]


def test_connect_saved_brings_the_profile_up():
    seen = []
    manager = WifiManager(runner=lambda argv, timeout: seen.append(argv) or _result())

    assert manager.connect_saved("Home") is True
    assert seen == [["nmcli", "connection", "up", "id", "Home"]]


def test_get_volume_reads_percent_and_mute(monkeypatch):
    def fake(*args, timeout=5.0):
        if args[0] == "get-sink-volume":
            return _result("Volume: front-left: 41943 /  64% / -11.00 dB,   front-right: 41943 /  64% / -11.00 dB")
        return _result("Mute: yes")

    monkeypatch.setattr(volume, "_pactl", fake)

    assert volume.get_volume() == {"percent": 64, "muted": True}


def test_get_volume_is_none_without_a_sound_server(monkeypatch):
    monkeypatch.setattr(volume, "_pactl", lambda *a, timeout=5.0: _result("", 1))

    assert volume.get_volume() is None


def test_set_volume_clamps_to_valid_range(monkeypatch):
    calls = []

    def fake(*args, timeout=5.0):
        calls.append(args)
        return _result("Volume: 100%" if args[0] == "get-sink-volume" else "Mute: no")

    monkeypatch.setattr(volume, "_pactl", fake)
    volume.set_volume(percent=250)

    assert calls[0] == ("set-sink-volume", "@DEFAULT_SINK@", "100%")
