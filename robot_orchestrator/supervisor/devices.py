class DeviceBusyError(RuntimeError):
    pass


class DeviceLeases:
    def __init__(self):
        self._holders: dict[str, str] = {}

    def acquire(self, device: str, holder: str) -> None:
        current = self._holders.get(device)
        if current is not None and current != holder:
            raise DeviceBusyError(f"{device!r} is held by {current!r}")
        self._holders[device] = holder

    def release(self, device: str, holder: str) -> None:
        if self._holders.get(device) == holder:
            del self._holders[device]

    def release_all(self, holder: str) -> None:
        for device in [d for d, h in self._holders.items() if h == holder]:
            del self._holders[device]

    def holder_of(self, device: str) -> str | None:
        return self._holders.get(device)

    def is_free(self, device: str) -> bool:
        return device not in self._holders
