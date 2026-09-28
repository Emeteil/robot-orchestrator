import hashlib
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from robot_orchestrator.config import PythonRequirementsConfig

_NAME_SPLIT_RE = re.compile(r"[=<>!;\[\s]")


def package_name(requirement_line: str) -> str:
    return _NAME_SPLIT_RE.split(requirement_line, maxsplit=1)[0].strip()


def _read_requirement_lines(release_dir: Path, requirement_files: list[str]) -> list[str]:
    lines: list[str] = []
    for rel in requirement_files:
        path = release_dir / rel
        if not path.exists():
            continue
        for raw in path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if line and not line.startswith("#"):
                lines.append(line)
    return lines


def compute_reqhash(release_dir: Path, requirement_files: list[str]) -> str:
    h = hashlib.sha256()
    h.update(f"{sys.version_info.major}.{sys.version_info.minor}".encode())
    for line in _read_requirement_lines(release_dir, requirement_files):
        h.update(line.encode())
        h.update(b"\n")
    return h.hexdigest()[:16]


def venv_python(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "python.exe"
    return venv_path / "bin" / "python"


def venv_pip(venv_path: Path) -> Path:
    if os.name == "nt":
        return venv_path / "Scripts" / "pip.exe"
    return venv_path / "bin" / "pip"


@dataclass
class VenvBuildResult:
    path: Path
    reqhash: str
    reused: bool


class VenvBuilder:
    def __init__(self, venvs_root: Path, wheelhouse: Path):
        self.venvs_root = venvs_root
        self.wheelhouse = wheelhouse

    def ensure(
        self,
        repo: str,
        release_dir: Path,
        python_config: PythonRequirementsConfig | None,
    ) -> VenvBuildResult | None:
        if python_config is None:
            return None
        reqhash = compute_reqhash(release_dir, python_config.requirements)
        venv_path = self.venvs_root / repo / reqhash
        marker = venv_path / ".complete"
        if marker.exists():
            return VenvBuildResult(venv_path, reqhash, reused=True)
        self._build(venv_path, release_dir, python_config)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
        return VenvBuildResult(venv_path, reqhash, reused=False)

    def _build(self, venv_path: Path, release_dir: Path, python_config: PythonRequirementsConfig) -> None:
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run([sys.executable, "-m", "venv", str(venv_path)], check=True, capture_output=True, text=True)
        pip = str(venv_pip(venv_path))

        lines = _read_requirement_lines(release_dir, python_config.requirements)
        no_deps_names = {n.lower() for n in python_config.pip_no_deps}
        no_deps_lines = [line for line in lines if package_name(line).lower() in no_deps_names]
        rest_lines = [line for line in lines if package_name(line).lower() not in no_deps_names]

        for line in no_deps_lines:
            subprocess.run([pip, "install", "--no-deps", line], check=True, capture_output=True, text=True)
            if package_name(line).lower() == "openwakeword":
                python = str(venv_python(venv_path))
                subprocess.run(
                    [python, "-c", "from openwakeword.utils import download_models; download_models()"],
                    check=True, capture_output=True, text=True,
                )

        if rest_lines:
            self.wheelhouse.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [pip, "download", "-d", str(self.wheelhouse), *rest_lines],
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                [pip, "install", "--no-index", "--find-links", str(self.wheelhouse), *rest_lines],
                check=True,
                capture_output=True,
                text=True,
            )


class NullVenvBuilder:
    def ensure(
        self,
        repo: str,
        release_dir: Path,
        python_config: PythonRequirementsConfig | None,
    ) -> VenvBuildResult | None:
        return None
