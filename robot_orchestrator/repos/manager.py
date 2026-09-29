import json
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from robot_orchestrator.bootlog import BOOT_LOG, logged_run
from robot_orchestrator.config import RepoConfig
from robot_orchestrator.paths import Paths
from robot_orchestrator.repos import git
from robot_orchestrator.repos.git import ensure_mirror_fresh as _ensure_mirror_fresh
from robot_orchestrator.repos.git import rmtree_safe as _rmtree
from robot_orchestrator.repos.venvs import NullVenvBuilder, VenvBuildResult, venv_python
from robot_orchestrator.state import store
from robot_orchestrator.state.store import Database
from robot_orchestrator.wal.atomic import ReleasePointer, fsync_dir
from robot_orchestrator.wal.journal import Journal

RELEASE_MARKER = ".orch-release.json"


@dataclass
class SyncResult:
    changed: bool
    sha: str | None
    error: str | None = None


@dataclass
class CheckResult:
    pending: bool
    current_sha: str | None
    remote_sha: str | None
    error: str | None = None


def make_repo_swap_committing_handler(paths: Paths):
    def handler(db: Database, row, journal: Journal) -> str:
        target_sha = row.payload["target_sha"]
        release_dir = paths.repo_release(row.subject, target_sha[:12])
        marker = release_dir / RELEASE_MARKER

        if not marker.exists():
            for p in row.payload.get("staging_paths", []):
                _rmtree(Path(p))
            _rmtree(release_dir)
            journal.rolled_back(row.op_id, error="crashed before release rename completed")
            return "rolled_back"

        prior_state = store.get_repo_state(db.conn, row.subject) or {}
        pointer = ReleasePointer(paths.repo_current_pointer(row.subject))
        previous_target = pointer.read()
        already_flipped = previous_target == str(release_dir)
        if not already_flipped:
            pointer.set(str(release_dir))

        def update(conn):
            store.upsert_repo_state(
                conn,
                row.subject,
                current_release=str(release_dir),
                current_sha=target_sha,
                previous_release=(prior_state.get("current_release") if not already_flipped else prior_state.get("previous_release")),
                previous_sha=(prior_state.get("current_sha") if not already_flipped else prior_state.get("previous_sha")),
                last_fetch_ok=1,
                last_error=None,
                updated_at=time.time(),
            )

        journal.committed(row.op_id, state_update=update)
        if not already_flipped and previous_target:
            ReleasePointer(paths.repo_previous_pointer(row.subject)).set(previous_target)
        return "committed"

    return handler


class RepoManager:
    def __init__(
        self,
        paths: Paths,
        journal: Journal,
        db: Database,
        config: RepoConfig,
        venv_builder=None,
        fetch_timeout: float = 120.0,
    ):
        self.paths = paths
        self.journal = journal
        self.db = db
        self.config = config
        self.venv_builder = venv_builder or NullVenvBuilder()
        self.fetch_timeout = fetch_timeout

    def sync(self) -> SyncResult:
        mirror = self.paths.repo_mirror(self.config.name)
        try:
            _ensure_mirror_fresh(mirror, self.config.url, timeout=self.fetch_timeout)
            target_sha = git.rev_parse(mirror, f"refs/heads/{self.config.branch}")
        except git.GitError as e:
            self._record_failure(str(e))
            return SyncResult(changed=False, sha=None, error=str(e))

        state = store.get_repo_state(self.db.conn, self.config.name) or {}

        if state.get("bad_sha") == target_sha:
            return SyncResult(
                changed=False,
                sha=state.get("current_sha"),
                error=f"skipping known-bad {target_sha[:12]}",
            )

        if state.get("current_sha") == target_sha and self._release_is_valid(state.get("current_release")):
            with self.db.transaction() as conn:
                store.upsert_repo_state(conn, self.config.name, last_fetch_at=time.time(), last_fetch_ok=1, last_error=None)
            return SyncResult(changed=False, sha=target_sha)

        return self._stage_and_commit(mirror, target_sha, state)

    def check(self) -> CheckResult:
        mirror = self.paths.repo_mirror(self.config.name)
        try:
            _ensure_mirror_fresh(mirror, self.config.url, timeout=self.fetch_timeout)
            remote_sha = git.rev_parse(mirror, f"refs/heads/{self.config.branch}")
        except git.GitError as e:
            self._record_failure(str(e))
            return CheckResult(pending=False, current_sha=None, remote_sha=None, error=str(e))

        state = store.get_repo_state(self.db.conn, self.config.name) or {}
        with self.db.transaction() as conn:
            store.upsert_repo_state(conn, self.config.name, last_fetch_at=time.time(), last_fetch_ok=1, last_error=None)

        current_sha = state.get("current_sha")
        pending = current_sha != remote_sha and remote_sha != state.get("bad_sha")
        return CheckResult(pending=pending, current_sha=current_sha, remote_sha=remote_sha)

    def rollback(self) -> bool:
        state = store.get_repo_state(self.db.conn, self.config.name) or {}
        previous_release = state.get("previous_release")
        previous_sha = state.get("previous_sha")
        if not previous_release or not (Path(previous_release) / RELEASE_MARKER).exists():
            return False

        op_id = uuid.uuid4().hex
        self.journal.begin("repo_rollback", self.config.name, {"target_sha": previous_sha}, op_id=op_id)
        self.journal.prepared(op_id)
        self.journal.committing(op_id)

        bad_release = state.get("current_release")
        bad_sha = state.get("current_sha")
        current_pointer = ReleasePointer(self.paths.repo_current_pointer(self.config.name))
        current_pointer.set(previous_release)
        if bad_release:
            ReleasePointer(self.paths.repo_previous_pointer(self.config.name)).set(bad_release)

        def update(conn):
            store.upsert_repo_state(
                conn,
                self.config.name,
                current_release=previous_release,
                current_sha=previous_sha,
                previous_release=bad_release,
                previous_sha=bad_sha,
                bad_sha=bad_sha,
                updated_at=time.time(),
            )

        self.journal.committed(op_id, state_update=update)
        return True

    def _release_is_valid(self, release_path: str | None) -> bool:
        if not release_path:
            return False
        return (Path(release_path) / RELEASE_MARKER).exists()

    def _record_failure(self, error: str) -> None:
        with self.db.transaction() as conn:
            store.upsert_repo_state(conn, self.config.name, last_fetch_at=time.time(), last_fetch_ok=0, last_error=error)

    def _stage_and_commit(self, mirror: Path, target_sha: str, prior_state: dict) -> SyncResult:
        op_id = uuid.uuid4().hex
        staging = self.paths.repo_staging(self.config.name, op_id)
        BOOT_LOG.emit(self.config.name, f"готовлю релиз {target_sha[:12]}: клонирование, подмодули, окружение", "info")
        self.journal.begin(
            "repo_swap",
            self.config.name,
            {"target_sha": target_sha, "staging_paths": [str(staging)]},
            op_id=op_id,
        )
        try:
            git.clone_no_checkout(mirror, staging)
            git.checkout_detach(staging, target_sha)
            self._sync_submodules(staging)
            self._link_persistent(staging)
            venv_result = self.venv_builder.ensure(self.config.name, staging, self.config.python)
            self._run_validate(staging, venv_result)
            self._write_release_marker(staging, target_sha, venv_result)
        except Exception as e:
            _rmtree(staging)
            error_message = str(e)

            def record(conn):
                store.upsert_repo_state(
                    conn, self.config.name, last_fetch_at=time.time(), last_fetch_ok=0, last_error=error_message
                )

            self.journal.rolled_back(op_id, error=error_message, state_update=record)
            return SyncResult(changed=False, sha=None, error=error_message)

        self.journal.prepared(op_id)
        release_dir = self.paths.repo_release(self.config.name, target_sha[:12])
        release_dir.parent.mkdir(parents=True, exist_ok=True)
        self.journal.committing(op_id)

        try:
            self._commit_release(op_id, release_dir, staging, target_sha, prior_state)
        except Exception as e:
            _rmtree(staging)
            _rmtree(release_dir)
            self.journal.abandoned(op_id, error=str(e))
            return SyncResult(changed=False, sha=None, error=str(e))

        return SyncResult(changed=True, sha=target_sha)

    def _commit_release(
        self,
        op_id: str,
        release_dir: Path,
        staging: Path,
        target_sha: str,
        prior_state: dict,
    ) -> None:
        staging.rename(release_dir)
        fsync_dir(release_dir.parent)

        current_pointer = ReleasePointer(self.paths.repo_current_pointer(self.config.name))
        previous_target = current_pointer.read()
        current_pointer.set(str(release_dir))

        def update_state(conn):
            store.upsert_repo_state(
                conn,
                self.config.name,
                current_release=str(release_dir),
                current_sha=target_sha,
                previous_release=previous_target or prior_state.get("current_release"),
                previous_sha=prior_state.get("current_sha"),
                last_fetch_at=time.time(),
                last_fetch_ok=1,
                last_error=None,
                updated_at=time.time(),
            )

        self.journal.committed(op_id, state_update=update_state)

        if previous_target:
            ReleasePointer(self.paths.repo_previous_pointer(self.config.name)).set(previous_target)

    def _sync_submodules(self, staging: Path) -> None:
        gitmodules = staging / ".gitmodules"
        if not gitmodules.exists():
            return
        submodules = git.parse_gitmodules(gitmodules.read_text(encoding="utf-8"))
        for sub in submodules:
            mirror_path = self.paths.repo_submodule_mirror(self.config.name, sub["name"])
            _ensure_mirror_fresh(mirror_path, sub["url"], timeout=self.fetch_timeout)
            git.set_submodule_url(staging, sub["path"], str(mirror_path.resolve()))
        git.submodule_update_init_recursive(staging)

    def _link_persistent(self, staging: Path) -> None:
        for name, rel in self.config.persistent.items():
            target_dir = self.paths.data_path(self.config.name, name)
            target_dir.mkdir(parents=True, exist_ok=True)
            link_path = staging / rel
            if link_path.is_symlink():
                link_path.unlink()
            elif link_path.exists():
                if link_path.is_dir():
                    if any(link_path.iterdir()):
                        raise RuntimeError(
                            f"persistent path {rel!r} is tracked/non-empty for repo {self.config.name}"
                        )
                    link_path.rmdir()
                else:
                    raise RuntimeError(f"persistent path {rel!r} exists as a file, expected a directory")
            else:
                link_path.parent.mkdir(parents=True, exist_ok=True)
            ReleasePointer(link_path).set(str(target_dir))

    def _run_validate(self, staging: Path, venv_result: VenvBuildResult | None) -> None:
        for template in self.config.validate_cmds:
            if "{venv}" in template and venv_result is None:
                continue
            if "{venv_python}" in template and venv_result is None:
                continue
            cmd = template.replace("{release}", str(staging))
            if venv_result is not None:
                cmd = cmd.replace("{venv}", str(venv_result.path)).replace(
                    "{venv_python}", str(venv_python(venv_result.path))
                )
            logged_run(cmd, source=f"check:{self.config.name}", shell=True, cwd=staging, check=True)

    def _write_release_marker(self, staging: Path, sha: str, venv_result: VenvBuildResult | None) -> None:
        marker = {
            "repo": self.config.name,
            "sha": sha,
            "venv_hash": venv_result.reqhash if venv_result else None,
            "created_at": time.time(),
        }
        (staging / RELEASE_MARKER).write_text(json.dumps(marker), encoding="utf-8")

    def committing_handler(self):
        return make_repo_swap_committing_handler(self.paths)

    def gc(self, keep_extra: set[Path] | None = None) -> list[Path]:
        keep_extra = keep_extra or set()
        state = store.get_repo_state(self.db.conn, self.config.name) or {}
        keep = {str(Path(p).resolve()) for p in (state.get("current_release"), state.get("previous_release")) if p}
        keep |= {str(p.resolve()) for p in keep_extra}
        removed = []
        releases_dir = self.paths.repo_releases_dir(self.config.name)
        if not releases_dir.exists():
            return removed
        for entry in releases_dir.iterdir():
            if entry.name.startswith(".staging-"):
                continue
            if str(entry.resolve()) in keep:
                continue
            _rmtree(entry)
            removed.append(entry)
        return removed
