import sys
import types

from robot_orchestrator.hal.real.camera import RealCameraProbe, resolve_camera_device


class _FakeFrame:
    def __init__(self, width: int, height: int):
        self.shape = (height, width, 3)


class _FakeCapture:
    def __init__(self, opened: bool, frame: "_FakeFrame | None" = None):
        self._opened = opened
        self._frame = frame
        self.released = False

    def isOpened(self) -> bool:
        return self._opened

    def read(self):
        if self._frame is None:
            return False, None
        return True, self._frame

    def release(self) -> None:
        self.released = True


def _install_fake_cv2(monkeypatch, capture_factory, calls: list):
    fake_cv2 = types.SimpleNamespace(CAP_V4L2=200)

    def video_capture(*args):
        calls.append(args)
        return capture_factory(*args)

    fake_cv2.VideoCapture = video_capture
    monkeypatch.setitem(sys.modules, "cv2", fake_cv2)


def test_probe_path_device_uses_cap_v4l2_flag(monkeypatch):
    calls: list = []
    _install_fake_cv2(monkeypatch, lambda *a: _FakeCapture(True, _FakeFrame(640, 480)), calls)

    result = RealCameraProbe().probe("/dev/video3")

    assert result.ok is True
    assert "640x480" in result.detail
    assert calls[0] == ("/dev/video3", 200)


def test_probe_numeric_string_device_uses_int_index(monkeypatch):
    calls: list = []
    _install_fake_cv2(monkeypatch, lambda *a: _FakeCapture(True, _FakeFrame(320, 240)), calls)

    RealCameraProbe().probe("0")

    assert calls[0] == (0,)


def test_probe_non_numeric_non_path_device_passed_through_as_is(monkeypatch):
    calls: list = []
    _install_fake_cv2(monkeypatch, lambda *a: _FakeCapture(True, _FakeFrame(320, 240)), calls)

    RealCameraProbe().probe("some-device-name")

    assert calls[0] == ("some-device-name",)


def test_probe_device_that_never_opens_returns_not_ok(monkeypatch):
    calls: list = []
    _install_fake_cv2(monkeypatch, lambda *a: _FakeCapture(False), calls)

    result = RealCameraProbe().probe("/dev/video9")

    assert result.ok is False


def test_probe_releases_capture_even_when_no_frame_is_read(monkeypatch):
    calls: list = []
    captures: list = []

    def factory(*a):
        cap = _FakeCapture(True, frame=None)
        captures.append(cap)
        return cap

    _install_fake_cv2(monkeypatch, factory, calls)

    result = RealCameraProbe().probe("/dev/video9")

    assert result.ok is False
    assert captures[0].released is True


def test_probe_releases_capture_on_success_too(monkeypatch):
    calls: list = []
    captures: list = []

    def factory(*a):
        cap = _FakeCapture(True, _FakeFrame(640, 480))
        captures.append(cap)
        return cap

    _install_fake_cv2(monkeypatch, factory, calls)

    RealCameraProbe().probe("/dev/video0")

    assert captures[0].released is True


def test_resolve_camera_device_is_reexported_from_boot_hardware():
    from robot_orchestrator.boot.hardware import resolve_camera_device as boot_resolve

    assert resolve_camera_device is boot_resolve
