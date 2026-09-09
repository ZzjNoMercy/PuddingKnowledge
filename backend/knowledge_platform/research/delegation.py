"""Application adapter for the dynamic research delegation contract."""

from urllib.parse import urlsplit

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_contracts.delegation import (
    CapabilityInventory,
    ResearchDelegationPlan,
    ResearchDelegationRequest,
)


class DynamicCapabilityPlanner:
    """Build a read-only research plan from discovered capabilities only."""

    def plan(
        self,
        request: ResearchDelegationRequest,
        inventory: CapabilityInventory,
    ) -> ResearchDelegationPlan:
        candidates = inventory.read_capabilities()
        if request.scope_uri is not None:
            if not is_valid_knowledge_uri(request.scope_uri):
                raise ValueError("research scope URI is not a valid knowledge resource")
            scheme = urlsplit(request.scope_uri).scheme
            candidates = tuple(
                capability for capability in candidates if scheme in capability.resource_schemes
            )
        selected = candidates[: request.max_capabilities]
        return ResearchDelegationPlan(
            query=request.query,
            capabilities=selected,
            unavailable=not selected,
        )

__all__ = ["DynamicCapabilityPlanner"]
