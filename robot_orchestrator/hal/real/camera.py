from robot_orchestrator.boot.hardware import resolve_camera_device
from robot_orchestrator.hal.base import ProbeResult

__all__ = ["RealCameraProbe", "resolve_camera_device"]


def _parse_int(device: str) -> int | None:
    try:
        return int(device)
    except ValueError:
        return None


class RealCameraProbe:
    def probe(self, device: str) -> ProbeResult:
        import cv2

        if device.startswith("/"):
            cap = cv2.VideoCapture(device, cv2.CAP_V4L2)
        else:
            index = _parse_int(device)
            cap = cv2.VideoCapture(index) if index is not None else cv2.VideoCapture(device)

        try:
            if not cap.isOpened():
                return ProbeResult(ok=False, detail=f"camera {device} did not open")
            ok, frame = cap.read()
            if not ok or frame is None:
                return ProbeResult(ok=False, detail=f"camera {device} opened but returned no frame")
            height, width = frame.shape[0], frame.shape[1]
            return ProbeResult(ok=True, detail=f"camera {device} opened, frame {width}x{height}")
        finally:
            cap.release()
