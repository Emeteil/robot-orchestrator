import io
import zipfile
from pathlib import Path

import httpx
import pytest

from robot_orchestrator.config import FirmwareCiConfig
from robot_orchestrator.firmware.github_artifacts import ArtifactDownloadError, GithubArtifactClient

BASE = "https://api.github.com"
BLOB = "https://blob-host.example.com"


def _config(**overrides) -> FirmwareCiConfig:
    defaults = dict(
        owner="Emeteil",
        repo="com-link-RT",
        workflow_file="build.yml",
        artifact_name="firmware-black_f407ve",
        wait_for_running_s=1.0,
    )
    defaults.update(overrides)
    return FirmwareCiConfig(**defaults)


def _zip_bytes(names: list[str]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name in names:
            zf.writestr(name, f"content of {name}")
    return buf.getvalue()


def _run_json(run_id: int, status: str, conclusion: str | None, head_sha: str = "abc123") -> dict:
    return {
        "id": run_id,
        "status": status,
        "conclusion": conclusion,
        "path": ".github/workflows/build.yml",
        "head_sha": head_sha,
        "created_at": "2026-09-27T00:00:00Z",
    }


def test_find_artifact_and_download_with_redirect_strips_auth_header(tmp_path: Path):
    run = _run_json(1, "completed", "success")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{BASE}/repos/Emeteil/com-link-RT/actions/runs?"):
            return httpx.Response(200, json={"workflow_runs": [run]})
        if url == f"{BASE}/repos/Emeteil/com-link-RT/actions/runs/1/artifacts":
            return httpx.Response(
                200,
                json={
                    "artifacts": [
                        {"name": "firmware-black_f407ve", "expired": False, "archive_download_url": f"{BASE}/download/1"}
                    ]
                },
            )
        if url == f"{BASE}/download/1":
            assert request.headers.get("authorization") == "Bearer secret-token"
            return httpx.Response(302, headers={"location": f"{BLOB}/artifact.zip"})
        if url == f"{BLOB}/artifact.zip":
            assert "authorization" not in request.headers
            return httpx.Response(200, content=_zip_bytes(["firmware.bin", "firmware.elf"]))
        raise AssertionError(f"unexpected request: {url}")

    client = GithubArtifactClient(
        _config(),
        token="secret-token",
        base_url=BASE,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_s=0.01,
    )

    ref = client.find_artifact("abc123")
    assert ref is not None
    assert ref.run_id == "1"

    dest = tmp_path / "artifact_download"
    out = client.download(ref, dest)
    assert (out / "firmware.bin").exists()
    assert (out / "firmware.elf").exists()


def test_find_artifact_polls_in_progress_run_until_completed():
    state = {"calls": 0}
    run_list = _run_json(2, "in_progress", None)

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{BASE}/repos/Emeteil/com-link-RT/actions/runs?"):
            return httpx.Response(200, json={"workflow_runs": [run_list]})
        if url == f"{BASE}/repos/Emeteil/com-link-RT/actions/runs/2":
            state["calls"] += 1
            if state["calls"] < 2:
                return httpx.Response(200, json=_run_json(2, "in_progress", None))
            return httpx.Response(200, json=_run_json(2, "completed", "success"))
        if url == f"{BASE}/repos/Emeteil/com-link-RT/actions/runs/2/artifacts":
            return httpx.Response(
                200,
                json={"artifacts": [{"name": "firmware-black_f407ve", "expired": False, "archive_download_url": "x"}]},
            )
        raise AssertionError(f"unexpected request: {url}")

    client = GithubArtifactClient(
        _config(wait_for_running_s=5.0),
        token="t",
        base_url=BASE,
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        poll_interval_s=0.01,
    )

    ref = client.find_artifact("abc123")
    assert ref is not None
    assert state["calls"] >= 2


def test_find_artifact_returns_none_when_conclusion_is_not_success():
    run = _run_json(3, "completed", "failure")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": [run]})

    client = GithubArtifactClient(_config(), token="t", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.find_artifact("abc123") is None


def test_find_artifact_returns_none_when_no_run_matches_head_sha():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workflow_runs": []})

    client = GithubArtifactClient(_config(), token="t", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.find_artifact("nonexistent-sha") is None


def test_find_artifact_ignores_expired_artifact():
    run = _run_json(4, "completed", "success")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.startswith(f"{BASE}/repos/Emeteil/com-link-RT/actions/runs?"):
            return httpx.Response(200, json={"workflow_runs": [run]})
        if url == f"{BASE}/repos/Emeteil/com-link-RT/actions/runs/4/artifacts":
            return httpx.Response(
                200,
                json={"artifacts": [{"name": "firmware-black_f407ve", "expired": True, "archive_download_url": "x"}]},
            )
        raise AssertionError(f"unexpected request: {url}")

    client = GithubArtifactClient(_config(), token="t", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))

    assert client.find_artifact("abc123") is None


def test_download_raises_clear_error_when_elf_missing_from_archive(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=_zip_bytes(["firmware.bin"]))

    from robot_orchestrator.hal.base import ArtifactRef

    client = GithubArtifactClient(_config(), token="t", base_url=BASE, http_client=httpx.Client(transport=httpx.MockTransport(handler)))
    ref = ArtifactRef(run_id="9", download_url=f"{BASE}/download/9")

    with pytest.raises(ArtifactDownloadError, match="firmware.elf"):
        client.download(ref, tmp_path / "missing_elf")
