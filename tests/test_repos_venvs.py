import subprocess
from types import SimpleNamespace

import pytest

from robot_orchestrator.config import PythonRequirementsConfig
from robot_orchestrator.repos.venvs import VenvBuilder, compute_reqhash


def test_nested_lock_changes_environment_hash(tmp_path):
    (tmp_path / "requirements.txt").write_text("-r lock.txt\n")
    lock = tmp_path / "lock.txt"
    lock.write_text("package==1\n")
    before = compute_reqhash(tmp_path, ["requirements.txt"])
    lock.write_text("package==2\n")
    assert compute_reqhash(tmp_path, ["requirements.txt"]) != before


def test_interpreter_and_install_options_change_environment(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("package==1\n")
    monkeypatch.setattr(subprocess, "check_output", lambda cmd, **kw: cmd[0])
    builder = VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")
    monkeypatch.setattr(builder, "_build", lambda *a, **kw: None)
    config = PythonRequirementsConfig(executable="python312")
    first = builder.ensure("voice", tmp_path, config)
    assert builder.ensure("voice", tmp_path, config).reused
    config.executable = "python310"
    assert builder.ensure("voice", tmp_path, config).reqhash != first.reqhash
    config.executable = "python312"
    config.extra_no_deps = ["openwakeword==0.6.0"]
    assert builder.ensure("voice", tmp_path, config).reqhash != first.reqhash


def test_build_dependencies_are_cached_before_offline_install(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("docopt==0.6.2\n")
    commands = []
    monkeypatch.setattr("robot_orchestrator.repos.venvs.logged_run",
                        lambda cmd, **kw: commands.append(cmd))
    builder = VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")
    config = PythonRequirementsConfig(build_requirements=["setuptools==80.9.0", "wheel==0.45.1"])
    builder._build(tmp_path / "env", tmp_path, config)
    build_index = next(i for i, cmd in enumerate(commands) if "setuptools==80.9.0" in cmd)
    install_index = next(i for i, cmd in enumerate(commands) if "--no-index" in cmd)
    assert build_index < install_index
    assert "--only-binary=:all:" in commands[build_index]
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "python312")
    monkeypatch.setattr(builder, "_build", lambda *a, **kw: None)
    first = builder.ensure("voice", tmp_path, config)
    config.build_requirements.append("other-build-package==1")
    assert builder.ensure("voice", tmp_path, config).reqhash != first.reqhash


def test_build_uses_selected_python_and_preserves_lock_arguments(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("-r lock.txt\n")
    commands = []
    monkeypatch.setattr("robot_orchestrator.repos.venvs.logged_run",
                        lambda cmd, **kw: commands.append(cmd))
    builder = VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")
    config = PythonRequirementsConfig(extra_no_deps=["openwakeword==0.6.0"])
    builder._build(tmp_path / "env", tmp_path, config, executable="python312")
    assert commands[0][0] == "python312"
    assert "--clear" in commands[0]
    assert any("--no-deps" in cmd and "openwakeword==0.6.0" in cmd for cmd in commands)
    download = next(cmd for cmd in commands if "download" in cmd)
    assert download[-2:] == ["-r", str(tmp_path / "requirements.txt")]


def test_missing_interpreter_is_installed_before_environment(tmp_path, monkeypatch):
    calls = []
    results = iter([SimpleNamespace(returncode=1, stdout=""),
                    SimpleNamespace(returncode=0, stdout="/managed/python3.12\n")])
    monkeypatch.setattr(subprocess, "run", lambda *a, **kw: next(results))
    monkeypatch.setattr("robot_orchestrator.repos.venvs.logged_run",
                        lambda cmd, **kw: calls.append((cmd, kw)))
    builder = VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")
    assert builder._python(PythonRequirementsConfig(version="3.12.12")) == "/managed/python3.12"
    assert calls[0][0][-2:] == ["install", "3.12.12"]
    assert calls[0][1]["env"]["UV_PYTHON_INSTALL_DIR"] == str(tmp_path / "venvs" / "_python")


def test_install_failure_does_not_mark_environment_ready(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("package==1\n")
    builder = VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")
    monkeypatch.setattr(subprocess, "check_output", lambda *a, **kw: "python312")

    def fail(*args, **kwargs):
        raise RuntimeError("install failed")

    monkeypatch.setattr(builder, "_build", fail)
    with pytest.raises(RuntimeError, match="install failed"):
        builder.ensure("voice", tmp_path, PythonRequirementsConfig())
    assert not list((tmp_path / "venvs").rglob(".complete"))


def test_wheelhouse_does_not_duplicate_pip_http_cache(tmp_path, monkeypatch):
    (tmp_path / "requirements.txt").write_text("package==1\n")
    calls = []
    monkeypatch.setattr("robot_orchestrator.repos.venvs.logged_run",
                        lambda cmd, **kw: calls.append((cmd, kw)))
    config = PythonRequirementsConfig(extra_no_deps=["other==1"], build_requirements=["setuptools==80.9.0"])
    VenvBuilder(tmp_path / "venvs", tmp_path / "wheels")._build(tmp_path / "env", tmp_path, config)
    pip_calls = [(cmd, kw) for cmd, kw in calls if "install" in cmd or "download" in cmd]
    assert len(pip_calls) == 4
    assert all(kw["env"]["PIP_NO_CACHE_DIR"] == "1" for cmd, kw in pip_calls)
