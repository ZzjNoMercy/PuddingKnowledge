from __future__ import annotations

import pytest

from knowledge_contracts.delegation import (
    CapabilityDescriptor,
    CapabilityInventory,
    ResearchDelegationRequest,
)
from knowledge_platform.research import DynamicCapabilityPlanner


def capability(
    capability_id: str,
    *,
    provider_id: str = "third-party-provider",
    read_only: bool = True,
    authorized: bool = True,
) -> CapabilityDescriptor:
    return CapabilityDescriptor(
        capability_id=capability_id,
        provider_id=provider_id,
        operation="read-resource",
        read_only=read_only,
        authorized=authorized,
        resource_schemes=("https",),
    )


def test_no_discovered_capability_is_explicitly_unavailable() -> None:
    plan = DynamicCapabilityPlanner().plan(
        ResearchDelegationRequest(query="research this"),
        CapabilityInventory(),
    )

    assert plan.unavailable is True
    assert plan.capabilities == ()


def test_arbitrary_authorized_provider_is_selected_without_tool_name_knowledge() -> None:
    plan = DynamicCapabilityPlanner().plan(
        ResearchDelegationRequest(query="research this"),
        CapabilityInventory((capability("provider-x/read-42"),)),
    )

    assert plan.unavailable is False
    assert [item.capability_id for item in plan.capabilities] == ["provider-x/read-42"]


def test_unauthorized_and_write_capabilities_are_not_delegated() -> None:
    inventory = CapabilityInventory(
        (
            capability("unauthorized", authorized=False),
            capability("write", read_only=False),
            capability("allowed"),
        )
    )

    plan = DynamicCapabilityPlanner().plan(ResearchDelegationRequest(query="research this"), inventory)

    assert [item.capability_id for item in plan.capabilities] == ["allowed"]


def test_capability_descriptor_metadata_is_immutable() -> None:
    item = CapabilityDescriptor("provider-x/read", "provider-x", "read", True, True, metadata={"kind": "read"})

    with pytest.raises(TypeError):
        item.metadata["kind"] = "write"


def test_capability_selection_is_bounded_by_request() -> None:
    inventory = CapabilityInventory(tuple(capability(f"cap-{index}") for index in range(3)))

    plan = DynamicCapabilityPlanner().plan(
        ResearchDelegationRequest(query="research this", max_capabilities=2),
        inventory,
    )

    assert [item.capability_id for item in plan.capabilities] == ["cap-0", "cap-1"]


def test_scope_uri_requires_a_capability_declaring_its_resource_scheme() -> None:
    inventory = CapabilityInventory(
        (
            capability("https-reader"),
            CapabilityDescriptor(
                capability_id="knowledge-reader",
                provider_id="provider-y",
                operation="read-resource",
                read_only=True,
                authorized=True,
                resource_schemes=("knowledge",),
            ),
        )
    )

    plan = DynamicCapabilityPlanner().plan(
        ResearchDelegationRequest(query="research this", scope_uri="knowledge://asset/1"),
        inventory,
    )

    assert [item.capability_id for item in plan.capabilities] == ["knowledge-reader"]


def test_scope_uri_rejects_knowledge_path_traversal() -> None:
    with pytest.raises(ValueError, match="knowledge"):
        ResearchDelegationRequest("research this", "knowledge://../etc/passwd")


@pytest.mark.parametrize("query", ["", "   "])
def test_research_request_requires_a_query(query: str) -> None:
    with pytest.raises(ValueError):
        ResearchDelegationRequest(query=query)
