from pathlib import Path

from robot_orchestrator.wal.atomic import ReleasePointer, atomic_write


def test_atomic_write_creates_file(tmp_path: Path):
    target = tmp_path / "config.yml"
    atomic_write(target, b"hello: world\n")
    assert target.read_bytes() == b"hello: world\n"


def test_atomic_write_leaves_no_temp_files_on_success(tmp_path: Path):
    target = tmp_path / "config.yml"
    atomic_write(target, b"a")
    atomic_write(target, b"b")
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []
    assert target.read_bytes() == b"b"


def test_release_pointer_roundtrip(tmp_path: Path):
    pointer = ReleasePointer(tmp_path / "current")
    assert pointer.read() is None
    pointer.set(str(tmp_path / "releases" / "abc123"))
    assert pointer.read() == str(tmp_path / "releases" / "abc123")


def test_release_pointer_overwrite_is_atomic_swap(tmp_path: Path):
    pointer = ReleasePointer(tmp_path / "current")
    pointer.set("first")
    pointer.set("second")
    assert pointer.read() == "second"
    leftovers = [p for p in tmp_path.iterdir() if p.name.startswith(".tmp-")]
    assert leftovers == []


def test_release_pointer_unset(tmp_path: Path):
    pointer = ReleasePointer(tmp_path / "current")
    pointer.set("x")
    pointer.unset()
    assert pointer.read() is None
