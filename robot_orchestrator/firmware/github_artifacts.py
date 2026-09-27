import time
import zipfile
from pathlib import Path

import httpx

from robot_orchestrator.config import FirmwareCiConfig
from robot_orchestrator.hal.base import ArtifactRef

_REDIRECT_STATUS_CODES = (301, 302, 303, 307, 308)
_RUNNING_STATUSES = ("queued", "in_progress")


class ArtifactDownloadError(Exception):
    pass


class GithubArtifactClient:
    def __init__(
        self,
        config: FirmwareCiConfig,
        token: str,
        base_url: str = "https://api.github.com",
        http_client: httpx.Client | None = None,
        poll_interval_s: float = 5.0,
    ):
        self.config = config
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http_client = http_client or httpx.Client()
        self.poll_interval_s = poll_interval_s

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def _get_json(self, url: str) -> dict:
        response = self.http_client.get(url, headers=self._auth_headers())
        response.raise_for_status()
        return response.json()

    def find_artifact(self, sha: str) -> ArtifactRef | None:
        workflow_path = f".github/workflows/{self.config.workflow_file}"
        runs_url = (
            f"{self.base_url}/repos/{self.config.owner}/{self.config.repo}"
            f"/actions/runs?head_sha={sha}&per_page=20"
        )
        data = self._get_json(runs_url)
        candidates = [r for r in data.get("workflow_runs", []) if r.get("path") == workflow_path]
        if not candidates:
            return None

        run = candidates[0]
        run_id = run["id"]
        status = run.get("status")

        deadline = time.monotonic() + self.config.wait_for_running_s
        while status in _RUNNING_STATUSES and time.monotonic() < deadline:
            time.sleep(self.poll_interval_s)
            run = self._get_json(
                f"{self.base_url}/repos/{self.config.owner}/{self.config.repo}/actions/runs/{run_id}"
            )
            status = run.get("status")

        if status != "completed" or run.get("conclusion") != "success":
            return None

        artifacts_data = self._get_json(
            f"{self.base_url}/repos/{self.config.owner}/{self.config.repo}/actions/runs/{run_id}/artifacts"
        )
        for artifact in artifacts_data.get("artifacts", []):
            if artifact.get("name") == self.config.artifact_name and not artifact.get("expired", False):
                return ArtifactRef(run_id=str(run_id), download_url=artifact["archive_download_url"])
        return None

    def download(self, ref: ArtifactRef, dest_dir: Path) -> Path:
        dest_dir.mkdir(parents=True, exist_ok=True)

        response = self.http_client.get(
            ref.download_url, headers=self._auth_headers(), follow_redirects=False
        )
        if response.status_code in _REDIRECT_STATUS_CODES and "location" in response.headers:
            redirect_url = response.headers["location"]
            response = self.http_client.get(redirect_url)
        response.raise_for_status()

        zip_path = dest_dir / "artifact.zip"
        zip_path.write_bytes(response.content)

        with zipfile.ZipFile(zip_path) as zf:
            names = set(zf.namelist())
            missing = [name for name in ("firmware.bin", "firmware.elf") if name not in names]
            if missing:
                raise ArtifactDownloadError(
                    f"artifact archive for run {ref.run_id} is missing expected file(s): {', '.join(missing)}"
                )
            zf.extract("firmware.bin", dest_dir)
            zf.extract("firmware.elf", dest_dir)

        return dest_dir
