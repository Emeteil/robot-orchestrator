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
    reason = "connect_failed"
    while True:
        try:
            connection.connect()
        except Exception:
            pass
        else:
            try:
                return _query(connection, ping_command_cls, version_command_cls, ping_tries)
            except Exception:
                # right after a flash reset the old CDC node can still be open while USB re-enumerates
                reason = "io_error"
            finally:
                _disconnect(connection)
        if clock() - start >= connect_timeout_s:
            return {"ok": False, "reason": reason}
        sleep(0.5)


def _query(connection, ping_command_cls, version_command_cls, ping_tries: int) -> dict:
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


def _disconnect(connection) -> None:
    try:
        connection.disconnect()
    except Exception:
        pass


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
