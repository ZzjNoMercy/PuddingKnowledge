"""Ports used by a generic Agent wiring adapter."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from knowledge_contracts import CapabilityDescriptor, CapabilityInventory, Correlation, Principal


class CapabilityDiscovery(Protocol):
    async def discover(self, *, principal: Principal) -> CapabilityInventory:
        """Discover capabilities after the interface authenticates the caller."""


class CapabilityInvoker(Protocol):
    async def invoke(
        self,
        capability: CapabilityDescriptor,
        arguments: Mapping[str, object],
        *,
        principal: Principal,
        correlation: Correlation,
    ) -> Mapping[str, object]:
        """Invoke a descriptor; the adapter owns protocol/tool translation."""
