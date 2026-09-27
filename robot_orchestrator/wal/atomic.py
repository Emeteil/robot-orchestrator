import os
import uuid
from pathlib import Path


def fsync_dir(path: Path) -> None:
    if os.name == "nt":
        return
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    directory = path.parent
    tmp = directory / f".tmp-{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    fsync_dir(directory)


class ReleasePointer:
    def __init__(self, path: Path):
        self.path = path

    def read(self) -> str | None:
        if not self.path.exists() and not self.path.is_symlink():
            return None
        if self.path.is_symlink():
            return os.readlink(self.path)
        return self.path.read_text(encoding="utf-8").strip()

    def set(self, target: str) -> None:
        directory = self.path.parent
        tmp = directory / f".tmp-{uuid.uuid4().hex}"
        if os.name == "nt":
            tmp.write_text(target, encoding="utf-8")
        else:
            os.symlink(target, tmp)
        os.replace(tmp, self.path)
        fsync_dir(directory)

    def unset(self) -> None:
        self.path.unlink(missing_ok=True)
        fsync_dir(self.path.parent)
