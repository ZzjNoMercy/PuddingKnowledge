"""Capability-driven Agent wiring with no business-field injection."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

from knowledge_contracts import CapabilityDescriptor, Correlation, Principal, is_valid_knowledge_uri

from .ports import CapabilityDiscovery, CapabilityInvoker

_FORBIDDEN_IMPLICIT_FIELDS = frozenset(
    {
        "session_id",
        "query_id",
        "run_id",
        "goal_id",
        "attachment",
        "attachments",
        "current_message",
        "session_documents",
        "virtual_path",
        "knowledge_mount",
        "semantic_assets_mount",
        "sql_guardrails_mount",
        "analytics_models_mount",
        "virtual_mount",
        "virtual_paths",
    }
)
_SURFACE_ISSUER = object()


def _forbidden_fields(value: object, path: str = "") -> list[str]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, item in value.items():
            normalized = str(key).casefold().replace("-", "_")
            current = f"{path}.{normalized}" if path else normalized
            if normalized in _FORBIDDEN_IMPLICIT_FIELDS:
                found.append(current)
            found.extend(_forbidden_fields(item, current))
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            found.extend(_forbidden_fields(item, f"{path}[{index}]"))
    return found


def _resource_uris(value: object, key: str = "") -> list[str]:
    normalized = key.casefold().replace("-", "_")
    compact = normalized.replace("_", "")
    if normalized in {"resource_uri", "source_uri", "uri", "blob_uri"} or compact.endswith(("uri", "url")):
        if not isinstance(value, str):
            raise ValueError(f"{normalized} must be a URI string")
        return [value]
    if isinstance(value, Mapping):
        result: list[str] = []
        for child_key, child_value in value.items():
            result.extend(_resource_uris(child_value, str(child_key)))
        return result
    if isinstance(value, (list, tuple)):
        result = []
        for child in value:
            result.extend(_resource_uris(child, key))
        return result
    return []


@dataclass(frozen=True, slots=True, init=False)
class AgentCapabilitySurface:
    principal: Principal
    correlation: Correlation
    capabilities: tuple[CapabilityDescriptor, ...]

    def __init__(
        self,
        principal: Principal,
        correlation: Correlation,
        capabilities: tuple[CapabilityDescriptor, ...],
        *,
        _issuer: object,
    ) -> None:
        if _issuer is not _SURFACE_ISSUER:
            raise TypeError("AgentCapabilitySurface must be created by discovery")
        object.__setattr__(self, "principal", principal)
        object.__setattr__(self, "correlation", correlation)
        object.__setattr__(self, "capabilities", tuple(capabilities))

    @classmethod
    def _from_discovery(
        cls,
        principal: Principal,
        correlation: Correlation,
        capabilities: tuple[CapabilityDescriptor, ...],
    ) -> AgentCapabilitySurface:
        return cls(principal, correlation, capabilities, _issuer=_SURFACE_ISSUER)

    def _resolve(self, capability_id: str) -> CapabilityDescriptor:
        for capability in self.capabilities:
            if capability.capability_id == capability_id and capability.authorized:
                return capability
        raise PermissionError("capability is not present in the authorized surface")

    async def invoke(
        self,
        capability_id: str,
        arguments: Mapping[str, object],
        *,
        invoker: CapabilityInvoker,
    ) -> Mapping[str, object]:
        forbidden = sorted(_forbidden_fields(arguments))
        if forbidden:
            raise ValueError("implicit Agent context fields are not accepted: " + ", ".join(forbidden))
        capability = self._resolve(capability_id)
        for resource_uri in _resource_uris(arguments):
            parsed = urlsplit(resource_uri)
            if not parsed.scheme or parsed.scheme not in capability.resource_schemes:
                raise ValueError("resource URI scheme is not declared by the capability")
            if parsed.scheme == "knowledge" and not is_valid_knowledge_uri(resource_uri):
                raise ValueError("resource URI is not a valid knowledge resource")
        return await invoker.invoke(
            capability,
            arguments,
            principal=self.principal,
            correlation=self.correlation,
        )


class AgentCapabilitySurfaceBuilder:
    """Build a surface from discovery; never infer capabilities from tool names."""

    def __init__(self, discovery: CapabilityDiscovery, *, trusted_issuer_ids: frozenset[str]) -> None:
        self._discovery = discovery
        if not trusted_issuer_ids:
            raise ValueError("trusted_issuer_ids must not be empty")
        self._trusted_issuer_ids = frozenset(trusted_issuer_ids)

    async def build(self, *, principal: Principal, correlation: Correlation) -> AgentCapabilitySurface:
        inventory = await self._discovery.discover(principal=principal)
        if inventory.issuer_id not in self._trusted_issuer_ids:
            raise PermissionError("capability inventory issuer is not trusted")
        authorized = tuple(capability for capability in inventory.capabilities if capability.authorized)
        return AgentCapabilitySurface._from_discovery(principal, correlation, authorized)
