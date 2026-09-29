import subprocess
from pathlib import Path

import pytest

from robot_orchestrator.config import SelfUpdateConfig
from robot_orchestrator.paths import Paths
from robot_orchestrator.selfupdate.manager import SELF_RELEASE_MARKER, SelfUpdateManager, make_self_update_committing_handler
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal import recovery
from robot_orchestrator.wal.atomic import ReleasePointer
from robot_orchestrator.wal.journal import Journal


def _git(args: list[str], cwd: Path) -> str:
    result = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _init_repo(path: Path, files: dict[str, str]) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "-b", "main"], path)
    _git(["config", "user.email", "t@t"], path)
    _git(["config", "user.name", "t"], path)
    for name, content in files.items():
        p = path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", "init"], path)
    return _git(["rev-parse", "HEAD"], path)


def _commit_all(path: Path, message: str) -> str:
    _git(["add", "-A"], path)
    _git(["commit", "-q", "-m", message], path)
    return _git(["rev-parse", "HEAD"], path)


def _fake_venv_builder(calls: list) -> callable:
    def builder(venv_path: Path, release_dir: Path) -> None:
        calls.append((venv_path, release_dir))
        venv_path.mkdir(parents=True, exist_ok=True)
        (venv_path / "marker").write_text("fake venv")
    return builder


def _fake_validator(ok: bool = True, detail: str = "fake validation failure") -> callable:
    def validator(venv_path: Path, release_dir: Path) -> tuple[bool, str]:
        return ok, detail
    return validator


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    paths.ensure()
    db = Database(paths.state_db)
    journal = Journal(db, boot_id="boot-1")
    yield paths, db, journal
    db.close()


def _manager(env, src: Path, venv_calls: list, validate_ok: bool = True, source_root: Path | None = None) -> SelfUpdateManager:
    paths, db, journal = env
    config = SelfUpdateConfig(enabled=True, url=str(src), branch="main")
    kwargs = {} if source_root is None else {"source_root": source_root}
    return SelfUpdateManager(
        paths, journal, db, config,
        venv_builder=_fake_venv_builder(venv_calls), validator=_fake_validator(validate_ok), **kwargs,
    )


def test_check_and_stage_stages_new_commit_when_current_is_known(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": "fastapi\n"})
    db.set_meta("self_update.current_sha", sha1)

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    venv_calls = []
    manager = _manager(env, src, venv_calls)

    status = manager.check_and_stage()

    assert status.staged_sha == sha2
    assert status.error is None
    assert venv_calls == [(paths.self_venv(sha2[:12]), paths.self_release(sha2[:12]))]
    assert (paths.self_release(sha2[:12]) / SELF_RELEASE_MARKER).exists()
    assert db.get_meta("self_update.staged_sha") == sha2


def test_check_and_stage_is_noop_when_remote_matches_current(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    db.set_meta("self_update.current_sha", sha1)

    manager = _manager(env, src, [])
    status = manager.check_and_stage()

    assert status.staged_sha is None
    assert status.in_progress is False


def test_check_and_stage_is_noop_when_remote_matches_already_staged(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    _commit_all(src, "second")

    venv_calls = []
    manager = _manager(env, src, venv_calls)
    manager.check_and_stage()
    assert len(venv_calls) == 1

    manager.check_and_stage()
    assert len(venv_calls) == 1


def test_check_and_stage_supersedes_a_previous_unapplied_stage(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")
    manager = _manager(env, src, [])
    manager.check_and_stage()
    assert paths.self_release(sha2[:12]).exists()

    (src / "main.py").write_text("print(3)\n", encoding="utf-8")
    sha3 = _commit_all(src, "third")
    manager.check_and_stage()

    assert not paths.self_release(sha2[:12]).exists()
    assert paths.self_release(sha3[:12]).exists()
    assert db.get_meta("self_update.staged_sha") == sha3


def test_check_and_stage_records_error_and_does_not_crash_on_validation_failure(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    manager = _manager(env, src, [], validate_ok=False)
    status = manager.check_and_stage()

    assert status.staged_sha is None
    assert status.error
    assert not paths.self_release(sha2[:12]).exists()
    assert not db.get_meta("self_update.staged_sha")


def test_check_and_stage_records_error_on_unreachable_remote_without_raising(env, tmp_path):
    paths, db, journal = env
    manager = _manager(env, tmp_path / "does-not-exist", [])

    status = manager.check_and_stage()

    assert status.staged_sha is None
    assert status.error


def test_apply_returns_false_when_nothing_staged(env, tmp_path):
    manager = _manager(env, tmp_path / "src", [])
    assert manager.apply() is False


def test_apply_flips_pointers_and_records_current_sha(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    manager = _manager(env, src, [])
    manager.check_and_stage()

    applied = manager.apply()

    assert applied is True
    assert ReleasePointer(paths.self_current_pointer).read() == str(paths.self_release(sha2[:12]))
    assert ReleasePointer(paths.self_current_venv_pointer).read() == str(paths.self_venv(sha2[:12]))
    assert db.get_meta("self_update.current_sha") == sha2
    assert db.get_meta("self_update.staged_sha") == ""


def test_apply_sets_previous_pointers_on_second_apply(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    manager = _manager(env, src, [])
    manager.check_and_stage()
    manager.apply()

    (src / "main.py").write_text("print(3)\n", encoding="utf-8")
    sha3 = _commit_all(src, "third")
    manager.check_and_stage()
    manager.apply()

    assert ReleasePointer(paths.self_current_pointer).read() == str(paths.self_release(sha3[:12]))
    assert ReleasePointer(paths.self_previous_pointer).read() == str(paths.self_release(sha2[:12]))
    assert ReleasePointer(paths.self_current_venv_pointer).read() == str(paths.self_venv(sha3[:12]))
    assert ReleasePointer(paths.self_previous_venv_pointer).read() == str(paths.self_venv(sha2[:12]))


def test_status_falls_back_to_git_head_when_never_self_updated(env, tmp_path):
    paths, db, journal = env
    running_source = tmp_path / "running-source"
    running_head = _init_repo(running_source, {"cli.py": "print('hi')\n"})

    manager = _manager(env, tmp_path / "src", [], source_root=running_source)
    status = manager.status()

    assert status.current_sha == running_head
    assert status.staged_sha is None


def test_status_current_sha_is_none_when_running_source_is_not_a_git_tree(env, tmp_path):
    running_source = tmp_path / "not-a-repo"
    running_source.mkdir()

    manager = _manager(env, tmp_path / "src", [], source_root=running_source)
    status = manager.status()

    assert status.current_sha is None


def test_recovery_of_committing_phase_rolls_forward_when_release_already_flipped(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})

    from robot_orchestrator.repos import git as gitmod

    mirror = paths.self_mirror
    gitmod.clone_mirror(str(src), mirror)
    op_id = "fixedselfop123"
    staging = paths.self_staging(op_id)
    gitmod.clone_no_checkout(mirror, staging)
    gitmod.checkout_detach(staging, sha1)
    release_dir = paths.self_release(sha1[:12])
    release_dir.parent.mkdir(parents=True, exist_ok=True)
    (staging / SELF_RELEASE_MARKER).write_text('{"sha": "%s"}' % sha1, encoding="utf-8")
    staging.rename(release_dir)
    venv_path = paths.self_venv(sha1[:12])
    venv_path.mkdir(parents=True)

    journal.begin(
        "self_update_swap", "self",
        {"target_sha": sha1, "release": str(release_dir), "venv": str(venv_path)}, op_id=op_id,
    )
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal, committing_handlers={"self_update_swap": make_self_update_committing_handler(paths)})

    assert report.rolled_forward == [op_id]
    assert ReleasePointer(paths.self_current_pointer).read() == str(release_dir)
    assert db.get_meta("self_update.current_sha") == sha1


def test_recovery_of_committing_phase_rolls_back_when_marker_missing(env, tmp_path):
    paths, db, journal = env
    op_id = "fixedselfop456"
    staging = paths.self_staging(op_id)
    staging.mkdir(parents=True)

    journal.begin(
        "self_update_swap", "self",
        {"target_sha": "deadbeef", "release": str(staging), "venv": str(tmp_path / "venv")}, op_id=op_id,
    )
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal, committing_handlers={"self_update_swap": make_self_update_committing_handler(paths)})

    assert report.rolled_back == [op_id]
    assert ReleasePointer(paths.self_current_pointer).read() is None


@pytest.mark.skipif(__import__("os").name == "nt", reason="requirements.txt includes gpiod, which is Linux-only")
def test_default_build_venv_and_validate_work_against_a_real_checkout(tmp_path):
    from robot_orchestrator.selfupdate.manager import _default_build_venv, _default_validate

    repo_root = Path(__file__).resolve().parents[1]
    venv_path = tmp_path / "venv"

    _default_build_venv(venv_path, repo_root)
    ok, detail = _default_validate(venv_path, repo_root)

    assert ok, detail


def test_gc_keeps_current_and_staged_but_removes_superseded_release(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n", "requirements.txt": ""})
    db.set_meta("self_update.current_sha", sha1)

    manager = _manager(env, src, [])

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")
    manager.check_and_stage()
    manager.apply()

    (src / "main.py").write_text("print(3)\n", encoding="utf-8")
    sha3 = _commit_all(src, "third")
    manager.check_and_stage()

    stray = paths.self_release("strayjunk12")
    stray.mkdir(parents=True)

    removed = manager.gc()

    assert not stray.exists()
    assert paths.self_release(sha2[:12]).exists()
    assert paths.self_release(sha3[:12]).exists()
    assert any(r.name == "strayjunk12" for r in removed)


def test_concurrent_check_is_rejected_while_a_build_is_running(env, tmp_path):
    import threading

    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    db.set_meta("self_update.current_sha", sha1)
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    build_started = threading.Event()
    release_build = threading.Event()
    calls = []

    def slow_builder(venv_path: Path, release_dir: Path) -> None:
        calls.append(venv_path)
        venv_path.mkdir(parents=True, exist_ok=True)
        build_started.set()
        assert release_build.wait(timeout=10)

    config = SelfUpdateConfig(enabled=True, url=str(src), branch="main")
    manager = SelfUpdateManager(paths, journal, db, config, venv_builder=slow_builder, validator=_fake_validator())

    results = []
    first = threading.Thread(target=lambda: results.append(manager.check_and_stage()))
    first.start()
    assert build_started.wait(timeout=10)

    second = manager.check_and_stage()

    assert second.in_progress is True
    assert second.staged_sha is None
    assert manager.status().in_progress is True
    assert len(calls) == 1

    release_build.set()
    first.join(timeout=10)

    assert results[0].staged_sha == sha2
    assert results[0].error is None
    assert manager.status().in_progress is False
    assert len(calls) == 1


def test_stale_leftovers_from_a_crashed_attempt_do_not_block_staging(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    db.set_meta("self_update.current_sha", sha1)
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    stale_release = paths.self_release(sha2[:12])
    stale_release.mkdir(parents=True)
    (stale_release / "half-written-file").write_text("junk", encoding="utf-8")
    stale_venv = paths.self_venv(sha2[:12])
    stale_venv.mkdir(parents=True)
    (stale_venv / "junk").write_text("junk", encoding="utf-8")

    manager = _manager(env, src, [])
    status = manager.check_and_stage()

    assert status.error is None
    assert status.staged_sha == sha2
    assert not (paths.self_release(sha2[:12]) / "half-written-file").exists()
