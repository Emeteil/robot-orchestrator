import httpx

from robot_orchestrator.config import NetworkConfig
from robot_orchestrator.hal.base import ProbeResult


class RealNetProbe:
    def __init__(self, config: NetworkConfig, http_client: httpx.Client | None = None):
        self.config = config
        self.http_client = http_client or httpx.Client()

    def probe(self) -> ProbeResult:
        successes = 0
        failures: list[str] = []
        for url in self.config.probes:
            try:
                response = self.http_client.get(url, timeout=self.config.timeout_s)
                if response.status_code < 500:
                    successes += 1
                else:
                    failures.append(f"{url}: status {response.status_code}")
            except httpx.HTTPError as e:
                failures.append(f"{url}: {e}")

        ok = successes >= self.config.min_ok
        detail = f"{successes}/{len(self.config.probes)} probes reachable"
        if failures:
            detail += "; " + "; ".join(failures)
        return ProbeResult(ok=ok, detail=detail)
