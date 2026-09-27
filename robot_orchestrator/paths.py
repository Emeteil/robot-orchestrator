import os
import stat
from dataclasses import dataclass
from pathlib import Path


def _mkdir(path: Path, mode: int) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(path, mode)
    return path


@dataclass(frozen=True)
class Paths:
    state_dir: Path
    log_dir: Path
    run_dir: Path

    @property
    def state_db(self) -> Path:
        return self.state_dir / "state.db"

    @property
    def bootstrap_stamp(self) -> Path:
        return self.state_dir / "bootstrap" / "system.json"

    @property
    def secrets_dir(self) -> Path:
        return self.state_dir / "secrets"

    @property
    def secrets_file(self) -> Path:
        return self.secrets_dir / "secrets.yml"

    @property
    def generated_secrets_file(self) -> Path:
        return self.secrets_dir / "generated.yml"

    @property
    def admin_file(self) -> Path:
        return self.secrets_dir / "admin.json"

    @property
    def repos_dir(self) -> Path:
        return self.state_dir / "repos"

    def repo_mirror(self, repo: str) -> Path:
        return self.repos_dir / repo / "mirror.git"

    def repo_submodule_mirror(self, repo: str, submodule: str) -> Path:
        return self.repos_dir / repo / "modules" / submodule

    def repo_releases_dir(self, repo: str) -> Path:
        return self.repos_dir / repo / "releases"

    def repo_release(self, repo: str, sha12: str) -> Path:
        return self.repo_releases_dir(repo) / sha12

    def repo_current_pointer(self, repo: str) -> Path:
        return self.repos_dir / repo / "current"

    def repo_previous_pointer(self, repo: str) -> Path:
        return self.repos_dir / repo / "previous"

    def repo_staging(self, repo: str, op_id: str) -> Path:
        return self.repo_releases_dir(repo) / f".staging-{op_id}"

    @property
    def venvs_dir(self) -> Path:
        return self.state_dir / "venvs"

    def venv_path(self, repo: str, reqhash: str) -> Path:
        return self.venvs_dir / repo / reqhash

    @property
    def platformio_venv(self) -> Path:
        return self.venvs_dir / "_platformio"

    @property
    def data_dir(self) -> Path:
        return self.state_dir / "data"

    def data_path(self, repo: str, name: str) -> Path:
        return self.data_dir / repo / name

    @property
    def firmware_dir(self) -> Path:
        return self.state_dir / "firmware"

    def firmware_release(self, sha: str) -> Path:
        return self.firmware_dir / sha

    def firmware_staging(self, op_id: str) -> Path:
        return self.firmware_dir / f".staging-{op_id}"

    @property
    def pio_core_dir(self) -> Path:
        return self.state_dir / "pio"

    @property
    def wheelhouse_dir(self) -> Path:
        return self.state_dir / "wheelhouse"

    @property
    def self_dir(self) -> Path:
        return self.state_dir / "self"

    @property
    def self_mirror(self) -> Path:
        return self.self_dir / "mirror.git"

    @property
    def self_releases_dir(self) -> Path:
        return self.self_dir / "releases"

    def self_release(self, sha12: str) -> Path:
        return self.self_releases_dir / sha12

    def self_staging(self, op_id: str) -> Path:
        return self.self_releases_dir / f".staging-{op_id}"

    @property
    def self_venvs_dir(self) -> Path:
        return self.self_dir / "venvs"

    def self_venv(self, sha12: str) -> Path:
        return self.self_venvs_dir / sha12

    @property
    def self_current_pointer(self) -> Path:
        return self.self_dir / "current"

    @property
    def self_previous_pointer(self) -> Path:
        return self.self_dir / "previous"

    @property
    def self_current_venv_pointer(self) -> Path:
        return self.self_dir / "current-venv"

    @property
    def self_previous_venv_pointer(self) -> Path:
        return self.self_dir / "previous-venv"

    def service_log(self, service: str) -> Path:
        return self.log_dir / "services" / f"{service}.log"

    @property
    def orchestrator_log(self) -> Path:
        return self.log_dir / "orchestrator.jsonl"

    @property
    def overlays_dir(self) -> Path:
        return self.run_dir / "overlays"

    def overlay_path(self, service: str) -> Path:
        return self.overlays_dir / f"{service}.yml"

    @property
    def qr_preview(self) -> Path:
        return self.run_dir / "qr_preview.jpg"

    @property
    def kiosk_profile(self) -> Path:
        return self.run_dir / "kiosk-profile"

    def ensure(self) -> None:
        _mkdir(self.state_dir, 0o750)
        _mkdir(self.state_dir / "bootstrap", 0o750)
        _mkdir(self.secrets_dir, 0o700)
        _mkdir(self.repos_dir, 0o750)
        _mkdir(self.venvs_dir, 0o750)
        _mkdir(self.data_dir, 0o750)
        _mkdir(self.firmware_dir, 0o750)
        _mkdir(self.pio_core_dir, 0o750)
        _mkdir(self.wheelhouse_dir, 0o750)
        _mkdir(self.self_dir, 0o750)
        _mkdir(self.self_releases_dir, 0o750)
        _mkdir(self.self_venvs_dir, 0o750)
        _mkdir(self.log_dir, 0o750)
        _mkdir(self.log_dir / "services", 0o750)
        _mkdir(self.run_dir, 0o750)
        _mkdir(self.overlays_dir, 0o750)

    def repo_root(self, repo: str) -> Path:
        return self.repos_dir / repo


def secure_file_mode(path: Path) -> None:
    if os.name == "nt":
        return
    os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
