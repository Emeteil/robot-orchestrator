import httpx

from robot_orchestrator.config import NetworkConfig
from robot_orchestrator.hal.real.network import RealNetProbe


def _probe(handler, **overrides) -> RealNetProbe:
    config = NetworkConfig(**overrides)
    client = httpx.Client(transport=httpx.MockTransport(handler))
    return RealNetProbe(config, http_client=client)


def test_all_probes_succeed_returns_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200)

    probe = _probe(handler, probes=["https://a.example.com", "https://b.example.com"], min_ok=2)
    result = probe.probe()

    assert result.ok is True


def test_some_probes_fail_but_min_ok_met_still_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        if "a.example.com" in str(request.url):
            return httpx.Response(200)
        raise httpx.ConnectError("connection refused", request=request)

    probe = _probe(handler, probes=["https://a.example.com", "https://b.example.com"], min_ok=1)
    result = probe.probe()

    assert result.ok is True


def test_not_enough_successes_returns_not_ok():
    def handler(request: httpx.Request) -> httpx.Response:
        if "a.example.com" in str(request.url):
            return httpx.Response(200)
        raise httpx.ConnectError("connection refused", request=request)

    probe = _probe(handler, probes=["https://a.example.com", "https://b.example.com"], min_ok=2)
    result = probe.probe()

    assert result.ok is False


def test_500_response_counts_as_failure():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    probe = _probe(handler, probes=["https://a.example.com"], min_ok=1)
    result = probe.probe()

    assert result.ok is False


def test_204_response_counts_as_reachable():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(204)

    probe = _probe(handler, probes=["http://connectivitycheck.gstatic.com/generate_204"], min_ok=1)
    result = probe.probe()

    assert result.ok is True
