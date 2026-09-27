import os
import socket


class SdNotifier:
    def __init__(self):
        addr = os.environ.get("NOTIFY_SOCKET")
        self.sock: socket.socket | None = None
        if addr:
            self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            self.addr = addr if not addr.startswith("@") else "\0" + addr[1:]

    def notify(self, state: str) -> None:
        if self.sock is None:
            return
        try:
            self.sock.sendto(state.encode("utf-8"), self.addr)
        except OSError:
            pass

    def ready(self) -> None:
        self.notify("READY=1")

    def watchdog(self) -> None:
        self.notify("WATCHDOG=1")

    def status(self, text: str) -> None:
        self.notify(f"STATUS={text}")

    def stopping(self) -> None:
        self.notify("STOPPING=1")
