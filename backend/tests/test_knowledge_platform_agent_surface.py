from __future__ import annotations

import asyncio

import pytest

from knowledge_contracts import CapabilityDescriptor, CapabilityInventory, Correlation, Principal
from knowledge_platform.agent import AgentCapabilitySurfaceBuilder


class FakeDiscovery:
    def __init__(self, inventory: CapabilityInventory) -> None:
        self.inventory = inventory

    async def discover(self, *, principal: Principal) -> CapabilityInventory:
        return self.inventory


class FakeInvoker:
    def __init__(self) -> None:
        self.received = None

    async def invoke(self, capability, arguments, *, principal, correlation):
        self.received = (capability, arguments, principal, correlation)
        return {"status": "ok"}


def descriptor(capability_id: str, authorized: bool = True) -> CapabilityDescriptor:
    return CapabilityDescriptor(capability_id, "provider-x", "read", True, authorized, ("knowledge",))


def test_surface_uses_discovered_authorized_descriptors_without_name_registry() -> None:
    inventory = CapabilityInventory(
        (descriptor("provider-x/read"), descriptor("denied", False)),
        issuer_id="auth-service",
    )
    surface = asyncio.run(
        AgentCapabilitySurfaceBuilder(FakeDiscovery(inventory), trusted_issuer_ids=frozenset({"auth-service"})).build(
            principal=Principal("user-1"), correlation=Correlation("trace-1")
        )
    )
    invoker = FakeInvoker()

    result = asyncio.run(surface.invoke("provider-x/read", {"resource_uri": "knowledge://asset/1"}, invoker=invoker))

    assert result == {"status": "ok"}
    assert [item.capability_id for item in surface.capabilities] == ["provider-x/read"]
    assert invoker.received[2:] == (Principal("user-1"), Correlation("trace-1"))


def test_surface_rejects_implicit_harness_context_fields() -> None:
    inventory = CapabilityInventory((descriptor("provider-x/read"),), issuer_id="auth-service")
    surface = asyncio.run(
        AgentCapabilitySurfaceBuilder(FakeDiscovery(inventory), trusted_issuer_ids=frozenset({"auth-service"})).build(
            principal=Principal("user-1"), correlation=Correlation("trace-1")
        )
    )

    with pytest.raises(ValueError, match="implicit Agent context"):
        asyncio.run(surface.invoke("provider-x/read", {"session_id": "legacy"}, invoker=FakeInvoker()))


def test_surface_rejects_capability_not_authorized_by_discovery() -> None:
    inventory = CapabilityInventory((descriptor("denied", False),), issuer_id="auth-service")
    surface = asyncio.run(
        AgentCapabilitySurfaceBuilder(FakeDiscovery(inventory), trusted_issuer_ids=frozenset({"auth-service"})).build(
            principal=Principal("user-1"), correlation=Correlation("trace-1")
        )
    )

    with pytest.raises(PermissionError):
        asyncio.run(surface.invoke("denied", {}, invoker=FakeInvoker()))


def test_capability_inventory_requires_a_trusted_issuer() -> None:
    inventory = CapabilityInventory((descriptor("provider-x/read"),), issuer_id="untrusted")
    with pytest.raises(PermissionError, match="issuer"):
        asyncio.run(
            AgentCapabilitySurfaceBuilder(
                FakeDiscovery(inventory), trusted_issuer_ids=frozenset({"auth-service"})
            ).build(principal=Principal("user-1"), correlation=Correlation("trace-1"))
        )


def test_surface_cannot_be_constructed_without_discovery_issuer() -> None:
    from knowledge_platform.agent.surface import AgentCapabilitySurface

    with pytest.raises(TypeError, match="created by discovery"):
        AgentCapabilitySurface(Principal("user-1"), Correlation("trace-1"), (), _issuer=object())


@pytest.mark.parametrize(
    "arguments",
    [
        {"goal_id": "legacy"},
        {"context": {"session_id": "legacy"}},
        {"virtual_mount": "/knowledge"},
        {"resource_uri": "file:///etc/passwd"},
        {"resourceUri": "file:///etc/passwd"},
        {"sourceUri": "knowledge://../etc/passwd"},
    ],
)
def test_surface_rejects_nested_implicit_fields_and_undeclared_resource_schemes(arguments) -> None:
    inventory = CapabilityInventory(
        (CapabilityDescriptor("provider-x/read", "provider-x", "read", True, True, ("knowledge",)),),
        issuer_id="auth-service",
    )
    surface = asyncio.run(
        AgentCapabilitySurfaceBuilder(FakeDiscovery(inventory), trusted_issuer_ids=frozenset({"auth-service"})).build(
            principal=Principal("user-1"), correlation=Correlation("trace-1")
        )
    )

    with pytest.raises(ValueError):
        asyncio.run(surface.invoke("provider-x/read", arguments, invoker=FakeInvoker()))
