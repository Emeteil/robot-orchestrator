import re
import subprocess
from pathlib import Path


def _run(argv: list[str], timeout: float = 5.0) -> str:
    try:
        result = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return result.stdout if result.returncode == 0 else ""


def _split_terse(line: str) -> list[str]:
    parts, current, escaped = [], "", False
    for ch in line:
        if escaped:
            current += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            parts.append(current)
            current = ""
        else:
            current += ch
    parts.append(current)
    return parts


def wan_route() -> dict:
    """Interface and address the kernel uses to reach the internet."""
    out = _run(["ip", "-4", "route", "get", "1.1.1.1"])
    dev = re.search(r"\bdev (\S+)", out)
    src = re.search(r"\bsrc (\S+)", out)
    via = re.search(r"\bvia (\S+)", out)
    return {
        "interface": dev.group(1) if dev else None,
        "ip": src.group(1) if src else None,
        "gateway": via.group(1) if via else None,
    }


def interface_kind(interface: str | None) -> str | None:
    if not interface:
        return None
    if Path(f"/sys/class/net/{interface}/wireless").exists():
        return "wifi"
    return "ethernet"


def current_ssid() -> str | None:
    # IN-USE is "*" regardless of the system locale, unlike the localized yes/no of ACTIVE
    for line in _run(["nmcli", "-t", "-f", "IN-USE,SSID", "device", "wifi", "list"]).splitlines():
        fields = _split_terse(line)
        if len(fields) >= 2 and fields[0] == "*":
            return fields[1] or None
    return None


def interface_addresses() -> list[dict]:
    rows = []
    for line in _run(["ip", "-4", "-o", "addr"]).splitlines():
        match = re.match(r"\d+:\s+(\S+)\s+inet\s+(\S+)", line)
        if match and match.group(1) != "lo":
            rows.append({"interface": match.group(1), "ip": match.group(2).split("/")[0]})
    return rows


def network_info() -> dict:
    wan = wan_route()
    kind = interface_kind(wan["interface"])
    return {
        **wan,
        "kind": kind,
        "ssid": current_ssid() if kind == "wifi" else None,
        "online": wan["interface"] is not None,
        "addresses": interface_addresses(),
    }
