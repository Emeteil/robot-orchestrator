import configparser
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

GIT_ENV = {**os.environ, "GIT_TERMINAL_PROMPT": "0"}


class GitError(RuntimeError):
    pass


def clear_readonly(path: Path) -> None:
    for root, _dirs, files in os.walk(path):
        for name in files:
            try:
                (Path(root) / name).chmod(stat.S_IWRITE | stat.S_IREAD)
            except OSError:
                pass


def rmtree_safe(path: Path, attempts: int = 3, delay: float = 0.1) -> None:
    for attempt in range(attempts):
        if path.exists():
            clear_readonly(path)
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return
        time.sleep(delay * (attempt + 1))


def ensure_mirror_fresh(mirror: Path, url: str, timeout: float | None = None) -> None:
    if not mirror.exists():
        clone_mirror(url, mirror, timeout=timeout)
        return
    try:
        fetch_all_refs(mirror, timeout=timeout)
    except GitError:
        if fsck(mirror):
            raise
        rmtree_safe(mirror)
        clone_mirror(url, mirror, timeout=timeout)


def run(args: list[str], cwd: Path | None = None, timeout: float | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=str(cwd) if cwd else None,
        env=GIT_ENV,
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


def clone_mirror(url: str, dest: Path, timeout: float | None = None) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["clone", "--mirror", url, str(dest)], timeout=timeout)


def fetch_all_refs(mirror: Path, timeout: float | None = None) -> None:
    run(["-C", str(mirror), "fetch", "--prune", "origin", "+refs/heads/*:refs/heads/*", "+refs/tags/*:refs/tags/*"], timeout=timeout)


def fsck(mirror: Path) -> bool:
    try:
        run(["-C", str(mirror), "fsck", "--connectivity-only"])
        return True
    except GitError:
        return False


def rev_parse(mirror: Path, ref: str) -> str:
    return run(["-C", str(mirror), "rev-parse", ref])


def has_object(mirror: Path, sha: str) -> bool:
    try:
        run(["-C", str(mirror), "cat-file", "-e", sha])
        return True
    except GitError:
        return False


def clone_no_checkout(mirror: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    run(["clone", "--no-checkout", str(mirror), str(dest)])


def checkout_detach(worktree: Path, sha: str) -> None:
    run(["-C", str(worktree), "checkout", "--detach", sha])


def read_gitmodules(worktree: Path, sha: str) -> str | None:
    try:
        return run(["-C", str(worktree), "show", f"{sha}:.gitmodules"])
    except GitError:
        return None


def ls_tree_path(worktree: Path, sha: str, path: str) -> str | None:
    out = run(["-C", str(worktree), "ls-tree", sha, path])
    if not out:
        return None
    return out.split()[2]


def set_submodule_url(worktree: Path, path: str, url: str) -> None:
    run(["-C", str(worktree), "config", f"submodule.{path}.url", url])


def submodule_update_init_recursive(worktree: Path) -> None:
    run(["-c", "protocol.file.allow=always", "-C", str(worktree), "submodule", "update", "--init", "--recursive"])


def parse_gitmodules(content: str) -> list[dict]:
    parser = configparser.ConfigParser(strict=False)
    parser.read_string(content)
    submodules = []
    for section in parser.sections():
        if not section.startswith("submodule "):
            continue
        name = section.split('"')[1] if '"' in section else section.split(" ", 1)[1]
        submodules.append({
            "name": name,
            "path": parser.get(section, "path"),
            "url": parser.get(section, "url"),
        })
    return submodules
