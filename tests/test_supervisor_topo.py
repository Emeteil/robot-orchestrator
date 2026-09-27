import pytest

from robot_orchestrator.config import DependsOnConfig, ServiceConfig
from robot_orchestrator.supervisor.supervisor import DependencyCycleError, topological_order


def svc(name: str, depends_on: list[str] = ()) -> ServiceConfig:
    return ServiceConfig(name=name, depends_on=[DependsOnConfig(service=d) for d in depends_on])


def test_independent_services_any_order():
    order = topological_order([svc("a"), svc("b"), svc("c")])
    assert set(order) == {"a", "b", "c"}
    assert len(order) == 3


def test_direct_dependency_ordered_correctly():
    order = topological_order([svc("web-core"), svc("voice-interface", ["web-core"])])
    assert order.index("web-core") < order.index("voice-interface")


def test_chain_of_three_ordered_correctly():
    order = topological_order([svc("c", ["b"]), svc("a"), svc("b", ["a"])])
    assert order.index("a") < order.index("b") < order.index("c")


def test_diamond_dependency_ordered_correctly():
    order = topological_order([
        svc("base"),
        svc("left", ["base"]),
        svc("right", ["base"]),
        svc("top", ["left", "right"]),
    ])
    assert order.index("base") < order.index("left")
    assert order.index("base") < order.index("right")
    assert order.index("left") < order.index("top")
    assert order.index("right") < order.index("top")


def test_cycle_raises():
    with pytest.raises(DependencyCycleError):
        topological_order([svc("x", ["y"]), svc("y", ["x"])])


def test_self_cycle_raises():
    with pytest.raises(DependencyCycleError):
        topological_order([svc("a", ["a"])])
