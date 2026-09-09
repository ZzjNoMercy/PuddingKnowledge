"""Versioned Harness boundary protocol without Analytics-owned fields."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum

from ._immutables import freeze_mapping
from .query import Correlation, Principal


class HarnessProtocolVersion(StrEnum):
    LEGACY_1 = "protocol-1.0"
    V2 = "harness-agent-2.0"


_REMOVED_LEGACY_FIELDS = frozenset({"analytics_model_id"})


def _find_removed_fields(value: object, path: str = "") -> tuple[str, ...]:
    found: list[str] = []
    if isinstance(value, Mapping):
        for key, child in value.items():
            current = f"{path}.{key}" if path else str(key)
            if str(key) in _REMOVED_LEGACY_FIELDS:
                found.append(current)
            found.extend(_find_removed_fields(child, current))
    elif isinstance(value, (list, tuple)):
        for index, child in enumerate(value):
            found.extend(_find_removed_fields(child, f"{path}[{index}]"))
    return tuple(found)


def project_legacy_payload(payload: Mapping[str, object]) -> tuple[dict[str, object], tuple[str, ...]]:
    """Read legacy payloads while dropping removed fields before v2 emission."""

    dropped: list[str] = []

    def project(value: object, path: str = "") -> object:
        if isinstance(value, Mapping):
            result: dict[str, object] = {}
            for raw_key, child in value.items():
                key = str(raw_key)
                current = f"{path}.{key}" if path else key
                if key in _REMOVED_LEGACY_FIELDS:
                    dropped.append(current)
                    continue
                result[key] = project(child, current)
            return result
        if isinstance(value, list):
            return [project(child, f"{path}[{index}]") for index, child in enumerate(value)]
        if isinstance(value, tuple):
            return tuple(project(child, f"{path}[{index}]") for index, child in enumerate(value))
        return value

    return project(payload), tuple(dropped)


@dataclass(frozen=True, slots=True)
class AgentProtocolEnvelope:
    protocol_version: HarnessProtocolVersion
    principal: Principal
    correlation: Correlation
    payload: Mapping[str, object]

    def __post_init__(self) -> None:
        if self.protocol_version == HarnessProtocolVersion.V2:
            removed = _find_removed_fields(self.payload)
            if removed:
                raise ValueError("v2 Harness payload contains removed fields: " + ", ".join(removed))
        object.__setattr__(self, "payload", freeze_mapping(self.payload))

    @classmethod
    def from_legacy(
        cls,
        *,
        principal: Principal,
        correlation: Correlation,
        payload: Mapping[str, object],
    ) -> tuple[AgentProtocolEnvelope, tuple[str, ...]]:
        projected, dropped = project_legacy_payload(payload)
        return cls(HarnessProtocolVersion.V2, principal, correlation, projected), dropped
