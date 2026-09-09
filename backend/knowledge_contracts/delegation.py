"""Framework-neutral contracts for dynamic read-capability delegation."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field

from ._immutables import freeze_mapping
from .query import is_valid_knowledge_uri


@dataclass(frozen=True, slots=True)
class CapabilityDescriptor:
    """A boundary-issued capability; the identifier need not be a tool name."""

    capability_id: str
    provider_id: str
    operation: str
    read_only: bool
    authorized: bool
    resource_schemes: tuple[str, ...] = ()
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for field_name in ("capability_id", "provider_id", "operation"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"CapabilityDescriptor.{field_name} must not be empty")
        if any(not scheme.strip() for scheme in self.resource_schemes):
            raise ValueError("CapabilityDescriptor.resource_schemes cannot contain empty values")
        if type(self.read_only) is not bool or type(self.authorized) is not bool:
            raise TypeError("CapabilityDescriptor.read_only and authorized must be bools")
        if any(not isinstance(value, str) for value in self.metadata.values()):
            raise ValueError("CapabilityDescriptor.metadata values must be strings")
        object.__setattr__(self, "resource_schemes", tuple(self.resource_schemes))
        object.__setattr__(self, "metadata", freeze_mapping(self.metadata))


@dataclass(frozen=True, slots=True)
class CapabilityInventory:
    """Capabilities discovered for this request after boundary authorization."""

    capabilities: tuple[CapabilityDescriptor, ...] = ()
    issuer_id: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "capabilities", tuple(self.capabilities))

    def read_capabilities(self) -> tuple[CapabilityDescriptor, ...]:
        return tuple(capability for capability in self.capabilities if capability.authorized and capability.read_only)


@dataclass(frozen=True, slots=True)
class ResearchDelegationRequest:
    query: str
    scope_uri: str | None = None
    max_capabilities: int = 8

    def __post_init__(self) -> None:
        if not self.query.strip():
            raise ValueError("ResearchDelegationRequest.query must not be empty")
        if self.scope_uri is not None and not is_valid_knowledge_uri(self.scope_uri):
            raise ValueError("ResearchDelegationRequest.scope_uri must use knowledge://")
        if self.max_capabilities <= 0:
            raise ValueError("ResearchDelegationRequest.max_capabilities must be positive")


@dataclass(frozen=True, slots=True)
class ResearchDelegationPlan:
    query: str
    capabilities: tuple[CapabilityDescriptor, ...]
    unavailable: bool
