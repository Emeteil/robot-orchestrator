import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from robot_orchestrator.secrets.protocol import PartialAssembly
from robot_orchestrator.wal.atomic import atomic_write


def _pyzbar_decode(frame: Any) -> list[str]:
    from pyzbar import pyzbar

    texts = []
    for symbol in pyzbar.decode(frame):
        try:
            texts.append(symbol.data.decode("utf-8"))
        except UnicodeDecodeError:
            continue
    return texts


def _emit(event: dict) -> None:
    print(json.dumps(event), flush=True)


def process_frames(
    frames: Iterable[Any],
    timeout_s: float,
    preview_path: Path | None,
    preview_fps: float,
    decode_frame: Callable[[Any], list[str]] = _pyzbar_decode,
    emit: Callable[[dict], None] = _emit,
    clock: Callable[[], float] = time.monotonic,
) -> str:
    assembly = PartialAssembly()
    reported: dict[str, tuple[int, int]] = {}
    preview_interval = 1.0 / preview_fps if preview_fps > 0 else None
    start = clock()
    last_preview = start

    for frame in frames:
        now = clock()
        if now - start >= timeout_s:
            emit({"event": "timeout"})
            return "timeout"

        for text in decode_frame(frame):
            payload = assembly.add_part(text)
            if payload is not None:
                emit({"event": "complete", "payload": payload.model_dump()})
                return "complete"

        for digest in assembly.digests():
            progress = assembly.progress(digest)
            if reported.get(digest) != progress:
                reported[digest] = progress
                emit({"event": "part", "digest": digest, "received": progress[0], "total": progress[1]})

        if preview_path is not None and preview_interval is not None and now - last_preview >= preview_interval:
            import cv2

            ok, buf = cv2.imencode(".jpg", frame)
            if ok:
                atomic_write(preview_path, buf.tobytes())
            last_preview = now

    emit({"event": "timeout"})
    return "timeout"


def _open_capture(device: str):
    import cv2

    if device.lstrip("-").isdigit():
        return cv2.VideoCapture(int(device))
    return cv2.VideoCapture(device, cv2.CAP_V4L2)


def _iter_camera_frames(cap) -> Iterable[Any]:
    while True:
        ret, frame = cap.read()
        if not ret:
            continue
        yield frame


def run(device: str, timeout_s: float, preview_path: Path | None, preview_fps: float) -> int:
    cap = _open_capture(device)
    if not cap.isOpened():
        _emit({"event": "camera_error", "detail": f"could not open camera device {device}"})
        cap.release()
        return 1

    try:
        outcome = process_frames(_iter_camera_frames(cap), timeout_s, preview_path, preview_fps)
    except Exception as exc:
        _emit({"event": "camera_error", "detail": str(exc)})
        return 1
    finally:
        cap.release()

    return 0 if outcome == "complete" else 2


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("device")
    parser.add_argument("--timeout", type=float, default=600.0)
    parser.add_argument("--preview-path", type=Path, default=None)
    parser.add_argument("--preview-fps", type=float, default=3.0)
    args = parser.parse_args(argv)
    return run(args.device, args.timeout, args.preview_path, args.preview_fps)


if __name__ == "__main__":
    sys.exit(main())
