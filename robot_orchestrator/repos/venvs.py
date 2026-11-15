import hashlib
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from robot_orchestrator.bootlog import BOOT_LOG, logged_run
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
    visited = set()

    def include(path):
        path = path.resolve()
        if path in visited:
            return
        visited.add(path)
        payload = path.read_bytes()
        h.update(payload)
        for line in payload.decode("utf-8").splitlines():
            parts = line.strip().split(maxsplit=1)
            if len(parts) == 2 and parts[0] in ("-r", "--requirement", "-c", "--constraint"):
                include(path.parent / parts[1])

    for name in requirement_files:
        include(release_dir / name)
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
        executable = self._python(python_config)
        identity = subprocess.check_output(
            [executable, "-c", "import sys; print(getattr(sys, '_base_executable', sys.executable)); print(sys.version)"],
            text=True, timeout=30,
        ).strip()
        payload = json.dumps([compute_reqhash(release_dir, python_config.requirements), identity,
                              python_config.pip_no_deps, python_config.extra_no_deps, python_config.build_requirements])
        reqhash = hashlib.sha256(payload.encode()).hexdigest()[:16]
        venv_path = self.venvs_root / repo / reqhash
        marker = venv_path / ".complete"
        if marker.exists():
            return VenvBuildResult(venv_path, reqhash, reused=True)
        BOOT_LOG.emit(f"pip:{repo}", f"собираю окружение {repo} (хэш зависимостей {reqhash})", "info")
        base_python = str(Path(identity.splitlines()[0]).resolve())
        self._build(venv_path, release_dir, python_config, source=f"pip:{repo}", executable=base_python)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text("")
        BOOT_LOG.emit(f"pip:{repo}", f"окружение {repo} готово", "ok")
        return VenvBuildResult(venv_path, reqhash, reused=False)

    def _python(self, config: PythonRequirementsConfig) -> str:
        if config.executable:
            return config.executable
        if not config.version:
            return sys.executable
        env = dict(os.environ, UV_PYTHON_INSTALL_DIR=str(self.venvs_root / "_python"))
        command = [sys.executable, "-m", "uv", "python"]
        find = command + ["find", "--managed-python", config.version]
        result = subprocess.run(find, env=env, capture_output=True, text=True, timeout=30)
        if result.returncode:
            logged_run(command + ["install", config.version], env=env, source="python", check=True, timeout=600)
            result = subprocess.run(find, env=env, capture_output=True, text=True, check=True, timeout=30)
        return result.stdout.strip()

    def _build(
        self,
        venv_path: Path,
        release_dir: Path,
        python_config: PythonRequirementsConfig,
        source: str = "pip",
        executable: str | None = None,
    ) -> None:
        venv_path.parent.mkdir(parents=True, exist_ok=True)
        logged_run([executable or python_config.executable or sys.executable, "-m", "venv", "--clear", str(venv_path)],
                   source=source, check=True)
        pip = str(venv_pip(venv_path))
        env = dict(os.environ, PIP_NO_CACHE_DIR="1")

        lines = _read_requirement_lines(release_dir, python_config.requirements)
        no_deps_names = {n.lower() for n in python_config.pip_no_deps}
        no_deps_lines = [line for line in lines if package_name(line).lower() in no_deps_names]
        no_deps_lines += python_config.extra_no_deps
        rest_lines = [line for line in lines if package_name(line).lower() not in no_deps_names]
        if not no_deps_names:
            rest_lines = [arg for name in python_config.requirements for arg in ("-r", str(release_dir / name))]

        for line in no_deps_lines:
            logged_run([pip, "install", "--no-deps", line], env=env, source=source, check=True)

        if rest_lines:
            self.wheelhouse.mkdir(parents=True, exist_ok=True)
            if python_config.build_requirements:
                logged_run(
                    [pip, "download", "--only-binary=:all:", "-d", str(self.wheelhouse),
                     *python_config.build_requirements],
                    cwd=release_dir, env=env, source=source, check=True,
                )
            logged_run([pip, "download", "-d", str(self.wheelhouse), *rest_lines],
                       cwd=release_dir, env=env, source=source, check=True)
            logged_run(
                [pip, "install", "--no-index", "--find-links", str(self.wheelhouse), *rest_lines],
                cwd=release_dir, env=env, source=source, check=True,
            )

        for line in no_deps_lines:
            if package_name(line).lower() == "openwakeword":
                python = str(venv_python(venv_path))
                logged_run(
                    [python, "-c", "from openwakeword.utils import download_models; "
                     "download_models(model_names=['alexa'])"],
                    source=source, check=True,
                )


class NullVenvBuilder:
    def ensure(
        self,
        repo: str,
        release_dir: Path,
        python_config: PythonRequirementsConfig | None,
    ) -> VenvBuildResult | None:
        return None
