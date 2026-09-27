import subprocess
from pathlib import Path

import pytest

from robot_orchestrator.config import RepoConfig
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos.manager import RELEASE_MARKER, RepoManager, make_repo_swap_committing_handler
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


@pytest.fixture
def env(tmp_path: Path):
    paths = Paths(tmp_path / "state", tmp_path / "logs", tmp_path / "run")
    paths.ensure()
    db = Database(paths.state_db)
    journal = Journal(db, boot_id="boot-1")
    yield paths, db, journal
    db.close()


def test_first_sync_creates_release_and_pointers(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)

    result = mgr.sync()

    assert result.changed is True
    assert result.sha == sha
    current = ReleasePointer(paths.repo_current_pointer("demo")).read()
    assert Path(current) == paths.repo_release("demo", sha[:12])
    assert (Path(current) / "main.py").read_text() == "print(1)\n"
    assert (Path(current) / RELEASE_MARKER).exists()


def test_second_sync_with_no_new_commits_is_a_noop(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)

    first = mgr.sync()
    second = mgr.sync()

    assert first.changed is True
    assert second.changed is False
    assert second.sha == first.sha


def test_new_commit_produces_new_release_and_sets_previous_pointer(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    result = mgr.sync()

    assert result.changed is True
    assert result.sha == sha2
    current = ReleasePointer(paths.repo_current_pointer("demo")).read()
    previous = ReleasePointer(paths.repo_previous_pointer("demo")).read()
    assert Path(current) == paths.repo_release("demo", sha2[:12])
    assert Path(previous) == paths.repo_release("demo", sha1[:12])
    assert (Path(current) / "main.py").read_text() == "print(2)\n"
    assert (Path(previous) / "main.py").read_text() == "print(1)\n"


def test_persistent_paths_are_linked_and_survive_across_releases(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main", persistent={"database": "database"})
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    current = Path(ReleasePointer(paths.repo_current_pointer("demo")).read())
    data_dir_1 = Path(ReleasePointer(current / "database").read())
    assert data_dir_1 == paths.data_path("demo", "database")
    (data_dir_1 / "users.json").write_text("[]", encoding="utf-8")

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    _commit_all(src, "second")
    mgr.sync()

    current2 = Path(ReleasePointer(paths.repo_current_pointer("demo")).read())
    data_dir_2 = Path(ReleasePointer(current2 / "database").read())
    assert data_dir_2 == data_dir_1
    assert (data_dir_2 / "users.json").read_text() == "[]"
    assert current2 != current


def test_validate_failure_rolls_back_and_records_error(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main", validate_cmds=["exit 1"])
    mgr = RepoManager(paths, journal, db, config)

    result = mgr.sync()

    assert result.changed is False
    assert result.error is not None
    assert ReleasePointer(paths.repo_current_pointer("demo")).read() is None
    releases_dir = paths.repo_releases_dir("demo")
    remaining = list(releases_dir.iterdir()) if releases_dir.exists() else []
    assert remaining == []
    assert journal.active_ops() == []


def test_bad_sha_is_skipped_without_restaging(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    with db.transaction() as conn:
        from robot_orchestrator.state import store
        store.upsert_repo_state(conn, "demo", bad_sha=sha)

    result = mgr.sync()
    assert result.changed is False
    assert "bad" in (result.error or "")


def test_submodule_is_checked_out_at_pinned_commit(env, tmp_path):
    paths, db, journal = env
    sub_src = tmp_path / "sub-src"
    sub_sha = _init_repo(sub_src, {"lib.py": "VERSION = 1\n"})

    main_src = tmp_path / "main-src"
    main_src.mkdir()
    _git(["init", "-q", "-b", "main"], main_src)
    _git(["config", "user.email", "t@t"], main_src)
    _git(["config", "user.name", "t"], main_src)
    (main_src / "app.py").write_text("import lib\n", encoding="utf-8")
    _git(["add", "app.py"], main_src)
    _git(["-c", "protocol.file.allow=always", "submodule", "add", str(sub_src), "lib"], main_src)
    _commit_all(main_src, "init with submodule")

    config = RepoConfig(name="demo", url=str(main_src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    result = mgr.sync()

    assert result.changed is True
    current = Path(ReleasePointer(paths.repo_current_pointer("demo")).read())
    assert (current / "lib" / "lib.py").read_text() == "VERSION = 1\n"
    submodule_head = _git(["rev-parse", "HEAD"], current / "lib")
    assert submodule_head == sub_sha


def test_recovery_rolls_forward_repo_swap_when_rename_completed_before_crash(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})

    from robot_orchestrator.repos import git as gitmod

    mirror = paths.repo_mirror("demo")
    gitmod.clone_mirror(str(src), mirror)
    op_id = "fixedop123"
    staging = paths.repo_staging("demo", op_id)
    gitmod.clone_no_checkout(mirror, staging)
    gitmod.checkout_detach(staging, sha)
    release_dir = paths.repo_release("demo", sha[:12])
    release_dir.parent.mkdir(parents=True, exist_ok=True)
    marker_content = '{"repo": "demo", "sha": "%s"}' % sha
    (staging / RELEASE_MARKER).write_text(marker_content, encoding="utf-8")
    staging.rename(release_dir)

    journal.begin("repo_swap", "demo", {"target_sha": sha, "staging_paths": [str(staging)]}, op_id=op_id)
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal, committing_handlers={"repo_swap": make_repo_swap_committing_handler(paths)})

    assert report.rolled_forward == [op_id]
    current = ReleasePointer(paths.repo_current_pointer("demo")).read()
    assert Path(current) == release_dir
    from robot_orchestrator.state import store

    state = store.get_repo_state(db.conn, "demo")
    assert state["current_sha"] == sha


def test_recovery_rolls_back_repo_swap_when_crash_before_rename(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})

    op_id = "fixedop456"
    staging = paths.repo_staging("demo", op_id)
    staging.mkdir(parents=True)
    (staging / "partial.txt").write_text("half", encoding="utf-8")

    journal.begin("repo_swap", "demo", {"target_sha": sha, "staging_paths": [str(staging)]}, op_id=op_id)
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal, committing_handlers={"repo_swap": make_repo_swap_committing_handler(paths)})

    assert report.rolled_back == [op_id]
    assert not staging.exists()
    assert ReleasePointer(paths.repo_current_pointer("demo")).read() is None


def test_check_reports_no_pending_update_right_after_sync(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    result = mgr.check()

    assert result.pending is False
    assert result.current_sha == sha
    assert result.remote_sha == sha
    assert result.error is None


def test_check_reports_pending_update_without_staging_anything(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")

    result = mgr.check()

    assert result.pending is True
    assert result.current_sha == sha1
    assert result.remote_sha == sha2
    assert ReleasePointer(paths.repo_current_pointer("demo")).read() != str(paths.repo_release("demo", sha2[:12]))


def test_check_does_not_flag_pending_for_a_known_bad_sha(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    from robot_orchestrator.state import store
    with db.transaction() as conn:
        store.upsert_repo_state(conn, "demo", bad_sha=sha)

    result = mgr.check()

    assert result.pending is False


def test_rollback_swaps_current_and_previous_and_marks_bad_sha(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    sha2 = _commit_all(src, "second")
    mgr.sync()

    ok = mgr.rollback()

    assert ok is True
    current = ReleasePointer(paths.repo_current_pointer("demo")).read()
    previous = ReleasePointer(paths.repo_previous_pointer("demo")).read()
    assert Path(current) == paths.repo_release("demo", sha1[:12])
    assert Path(previous) == paths.repo_release("demo", sha2[:12])

    from robot_orchestrator.state import store
    state = store.get_repo_state(db.conn, "demo")
    assert state["current_sha"] == sha1
    assert state["previous_sha"] == sha2
    assert state["bad_sha"] == sha2


def test_rollback_is_a_noop_without_a_previous_release(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    assert mgr.rollback() is False


def test_recovery_of_repo_rollback_committing_phase_uses_same_handler_as_repo_swap(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    sha1 = _init_repo(src, {"main.py": "print(1)\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()
    (src / "main.py").write_text("print(2)\n", encoding="utf-8")
    _commit_all(src, "second")
    mgr.sync()

    op_id = "rollbackop789"
    journal.begin("repo_rollback", "demo", {"target_sha": sha1}, op_id=op_id)
    journal.prepared(op_id)
    journal.committing(op_id)

    report = recovery.run(db, journal, committing_handlers={"repo_rollback": make_repo_swap_committing_handler(paths)})

    assert report.rolled_forward == [op_id]
    current = ReleasePointer(paths.repo_current_pointer("demo")).read()
    assert Path(current) == paths.repo_release("demo", sha1[:12])


def test_gc_removes_old_releases_but_keeps_current_and_previous(env, tmp_path):
    paths, db, journal = env
    src = tmp_path / "src"
    _init_repo(src, {"main.py": "v1\n"})
    config = RepoConfig(name="demo", url=str(src), branch="main")
    mgr = RepoManager(paths, journal, db, config)
    mgr.sync()

    for i in range(2, 5):
        (src / "main.py").write_text(f"v{i}\n", encoding="utf-8")
        _commit_all(src, f"v{i}")
        mgr.sync()

    releases_before = list(paths.repo_releases_dir("demo").iterdir())
    assert len(releases_before) == 4

    removed = mgr.gc()

    releases_after = list(paths.repo_releases_dir("demo").iterdir())
    assert len(releases_after) == 2
    assert len(removed) == 2
