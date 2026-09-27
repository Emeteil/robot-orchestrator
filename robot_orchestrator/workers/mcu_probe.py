import argparse
import json
import sys
import time
from typing import Callable


def probe(
    connection,
    ping_command_cls,
    version_command_cls,
    connect_timeout_s: float,
    ping_tries: int,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    start = clock()
    connected = False
    while True:
        try:
            connection.connect()
            connected = True
            break
        except Exception:
            pass
        if clock() - start >= connect_timeout_s:
            break
        sleep(0.5)

    if not connected:
        return {"ok": False, "reason": "connect_failed"}

    try:
        ping_ms = None
        for _ in range(ping_tries):
            ping_ms = ping_command_cls(connection).execute(wait_response=True, timeout=2.0)
            if ping_ms is not None:
                break
        if ping_ms is None:
            return {"ok": False, "reason": "no_ping_response"}

        version = version_command_cls(connection).execute(wait_response=True, timeout=5.0)
        if version is None:
            return {"ok": False, "reason": "no_version_response"}

        return {
            "ok": True,
            "ping_ms": ping_ms,
            "version": {"build_date": version.get("build_date"), "build_time": version.get("build_time")},
        }
    finally:
        connection.disconnect()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("port")
    parser.add_argument("--ping-tries", type=int, default=3)
    parser.add_argument("--connect-timeout", type=float, default=10.0)
    args = parser.parse_args(argv)

    import com_link_rt

    connection = com_link_rt.ComLinkConnection(args.port)
    result = probe(
        connection,
        com_link_rt.PingCommand,
        com_link_rt.VersionCommand,
        args.connect_timeout,
        args.ping_tries,
    )
    print(json.dumps(result), flush=True)
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    sys.exit(main())
