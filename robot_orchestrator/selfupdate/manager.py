import json
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from robot_orchestrator.bootlog import logged_run
from robot_orchestrator.config import SelfUpdateConfig
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos import git
from robot_orchestrator.repos.venvs import venv_pip, venv_python
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal.atomic import ReleasePointer, fsync_dir
from robot_orchestrator.wal.journal import Journal

SELF_RELEASE_MARKER = ".orch-self-release.json"

VenvBuilderT = Callable[[Path, Path], None]
ValidatorT = Callable[[Path, Path], tuple[bool, str]]


@dataclass
class SelfUpdateStatus:
    current_sha: str | None
    staged_sha: str | None
    staged_at: float | None
    remote_sha: str | None = None
    error: str | None = None
    in_progress: bool = False


def _default_build_venv(venv_path: Path, release_dir: Path) -> None:
    venv_path.parent.mkdir(parents=True, exist_ok=True)
    logged_run([sys.executable, "-m", "venv", str(venv_path)], source="self-update", check=True)
    pip = str(venv_pip(venv_path))
    requirements = release_dir / "requirements.txt"
    if requirements.exists():
        logged_run([pip, "install", "-r", str(requirements)], source="self-update", check=True)
    logged_run([pip, "install", "-e", str(release_dir)], source="self-update", check=True)


def _default_validate(venv_path: Path, release_dir: Path) -> tuple[bool, str]:
    python = str(venv_python(venv_path))
    compile_result = subprocess.run(
        [python, "-m", "compileall", "-q", str(release_dir / "robot_orchestrator")],
        capture_output=True, text=True,
    )
    if compile_result.returncode != 0:
        return False, f"compileall failed: {compile_result.stderr or compile_result.stdout}"

    with tempfile.TemporaryDirectory(prefix="ro-self-update-validate-") as scratch:
        doctor_result = subprocess.run(
            [python, "-m", "robot_orchestrator.cli", "doctor", "--state-dir", scratch],
            capture_output=True, text=True, cwd=release_dir,
        )
    if doctor_result.returncode != 0:
        return False, f"doctor smoke test failed: {doctor_result.stderr or doctor_result.stdout}"
    return True, ""


DEFAULT_SOURCE_ROOT = Path(__file__).resolve().parent.parent.parent


def _resolve_current_sha(db: Database, source_root: Path = DEFAULT_SOURCE_ROOT) -> str | None:
    meta_sha = db.get_meta("self_update.current_sha")
    if meta_sha:
        return meta_sha
    try:
        return git.rev_parse(source_root, "HEAD")
    except git.GitError:
        return None


class SelfUpdateManager:
    def __init__(
        self,
        paths: Paths,
        journal: Journal,
        db: Database,
        config: SelfUpdateConfig,
        venv_builder: VenvBuilderT | None = None,
        validator: ValidatorT | None = None,
        source_root: Path = DEFAULT_SOURCE_ROOT,
    ):
        self.paths = paths
        self.journal = journal
        self.db = db
        self.config = config
        self.venv_builder = venv_builder or _default_build_venv
        self.validator = validator or _default_validate
        self.source_root = source_root
        self._stage_lock = threading.Lock()

    def status(self) -> SelfUpdateStatus:
        return SelfUpdateStatus(
            current_sha=_resolve_current_sha(self.db, self.source_root),
            staged_sha=self.db.get_meta("self_update.staged_sha") or None,
            staged_at=float(v) if (v := self.db.get_meta("self_update.staged_at")) else None,
            error=self.db.get_meta("self_update.last_error") or None,
            in_progress=self._stage_lock.locked(),
        )

    def check_and_stage(self) -> SelfUpdateStatus:
        # a second concurrent run would rebuild (and, on failure, delete) the very same release/venv
        # directories the first one is still using, so a busy manager just reports its current status
        if not self._stage_lock.acquire(blocking=False):
            return self.status()
        try:
            return self._check_and_stage_locked()
        finally:
            self._stage_lock.release()

    def _check_and_stage_locked(self) -> SelfUpdateStatus:
        current_sha = _resolve_current_sha(self.db, self.source_root)
        try:
            git.ensure_mirror_fresh(self.paths.self_mirror, self.config.url, timeout=self.config.fetch_timeout_s)
            remote_sha = git.rev_parse(self.paths.self_mirror, f"refs/heads/{self.config.branch}")
        except git.GitError as e:
            self._record_error(str(e))
            return SelfUpdateStatus(current_sha=current_sha, staged_sha=None, staged_at=None, error=str(e))

        already_staged = self.db.get_meta("self_update.staged_sha") or None
        if remote_sha == current_sha or remote_sha == already_staged:
            self.db.set_meta("self_update.last_error", "")
            return self.status()

        return self._stage(remote_sha, current_sha, superseded_sha=already_staged)

    def _stage(self, target_sha: str, current_sha: str | None, superseded_sha: str | None = None) -> SelfUpdateStatus:
        if superseded_sha:
            git.rmtree_safe(self.paths.self_release(superseded_sha[:12]))
            git.rmtree_safe(self.paths.self_venv(superseded_sha[:12]))

        op_id = uuid.uuid4().hex
        staging = self.paths.self_staging(op_id)
        release_dir = self.paths.self_release(target_sha[:12])
        venv_path = self.paths.self_venv(target_sha[:12])
        # leftovers of an attempt that crashed half-way would make the final rename fail
        git.rmtree_safe(release_dir)
        git.rmtree_safe(venv_path)
        try:
            git.clone_no_checkout(self.paths.self_mirror, staging)
            git.checkout_detach(staging, target_sha)
            staging.rename(release_dir)
            fsync_dir(release_dir.parent)

            self.venv_builder(venv_path, release_dir)
            ok, detail = self.validator(venv_path, release_dir)
            if not ok:
                git.rmtree_safe(release_dir)
                git.rmtree_safe(venv_path)
                self._record_error(detail)
                return SelfUpdateStatus(current_sha=current_sha, staged_sha=None, staged_at=None, error=detail)

            (release_dir / SELF_RELEASE_MARKER).write_text(
                json.dumps({"sha": target_sha, "created_at": time.time()}), encoding="utf-8"
            )
        except Exception as e:
            git.rmtree_safe(staging)
            git.rmtree_safe(release_dir)
            git.rmtree_safe(venv_path)
            self._record_error(str(e))
            return SelfUpdateStatus(current_sha=current_sha, staged_sha=None, staged_at=None, error=str(e))

        staged_at = time.time()
        self.db.set_meta("self_update.staged_sha", target_sha)
        self.db.set_meta("self_update.staged_at", str(staged_at))
        self.db.set_meta("self_update.staged_venv", str(self.paths.self_venv(target_sha[:12])))
        self.db.set_meta("self_update.last_error", "")
        return SelfUpdateStatus(current_sha=current_sha, staged_sha=target_sha, staged_at=staged_at)

    def _record_error(self, error: str) -> None:
        self.db.set_meta("self_update.last_error", error)

    def apply(self) -> bool:
        staged_sha = self.db.get_meta("self_update.staged_sha")
        staged_venv = self.db.get_meta("self_update.staged_venv")
        if not staged_sha:
            return False

        release_dir = self.paths.self_release(staged_sha[:12])
        marker = release_dir / SELF_RELEASE_MARKER
        if not marker.exists() or not staged_venv or not Path(staged_venv).exists():
            return False

        op_id = uuid.uuid4().hex
        self.journal.begin(
            "self_update_swap", "self",
            {"target_sha": staged_sha, "release": str(release_dir), "venv": staged_venv}, op_id=op_id,
        )
        self.journal.prepared(op_id)
        self.journal.committing(op_id)

        release_pointer = ReleasePointer(self.paths.self_current_pointer)
        venv_pointer = ReleasePointer(self.paths.self_current_venv_pointer)
        previous_release = release_pointer.read()
        previous_venv = venv_pointer.read()
        release_pointer.set(str(release_dir))
        venv_pointer.set(staged_venv)

        self.journal.committed(op_id, state_update=lambda conn: self._record_applied(conn, staged_sha))

        if previous_release:
            ReleasePointer(self.paths.self_previous_pointer).set(previous_release)
        if previous_venv:
            ReleasePointer(self.paths.self_previous_venv_pointer).set(previous_venv)
        return True

    def _record_applied(self, conn, staged_sha: str) -> None:
        self.db.set_meta("self_update.current_sha", staged_sha, conn=conn)
        self.db.set_meta("self_update.staged_sha", "", conn=conn)
        self.db.set_meta("self_update.staged_at", "", conn=conn)
        self.db.set_meta("self_update.staged_venv", "", conn=conn)

    def gc(self) -> list[Path]:
        keep_shas = {s[:12] for s in (self.db.get_meta("self_update.current_sha"), self.db.get_meta("self_update.staged_sha")) if s}
        for pointer_path in (self.paths.self_current_pointer, self.paths.self_previous_pointer):
            target = ReleasePointer(pointer_path).read()
            if target:
                keep_shas.add(Path(target).name)

        removed = []
        for base_dir in (self.paths.self_releases_dir, self.paths.self_venvs_dir):
            if not base_dir.exists():
                continue
            for entry in base_dir.iterdir():
                if entry.name.startswith(".staging-") or entry.name in keep_shas:
                    continue
                git.rmtree_safe(entry)
                removed.append(entry)
        return removed


def make_self_update_committing_handler(paths: Paths):
    def handler(db: Database, row, journal: Journal) -> str:
        target_sha = row.payload["target_sha"]
        release_dir = Path(row.payload["release"])
        venv_path = row.payload["venv"]
        marker = release_dir / SELF_RELEASE_MARKER

        if not marker.exists():
            journal.rolled_back(row.op_id, error="crashed before self-update release was staged")
            return "rolled_back"

        release_pointer = ReleasePointer(paths.self_current_pointer)
        venv_pointer = ReleasePointer(paths.self_current_venv_pointer)
        previous_release = release_pointer.read()
        previous_venv = venv_pointer.read()
        already_flipped = previous_release == str(release_dir)
        if not already_flipped:
            release_pointer.set(str(release_dir))
            venv_pointer.set(venv_path)

        def update(conn):
            db.set_meta("self_update.current_sha", target_sha, conn=conn)
            db.set_meta("self_update.staged_sha", "", conn=conn)
            db.set_meta("self_update.staged_at", "", conn=conn)
            db.set_meta("self_update.staged_venv", "", conn=conn)

        journal.committed(row.op_id, state_update=update)
        if not already_flipped:
            if previous_release:
                ReleasePointer(paths.self_previous_pointer).set(previous_release)
            if previous_venv:
                ReleasePointer(paths.self_previous_venv_pointer).set(previous_venv)
        return "committed"

    return handler
