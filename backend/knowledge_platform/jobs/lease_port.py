"""Application port for a database-backed, owner-fenced job lease store."""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from knowledge_contracts import LeaseCompletion, LeaseRecord


class LeaseStore(Protocol):
    """Port implemented by SQL/queue adapters; timestamps come from the adapter."""

    async def claim_next(self, *, owner: str, now: datetime, lease_seconds: int) -> LeaseRecord | None:
        """Atomically claim the oldest queued or expired job."""

    async def heartbeat(
        self, *, job_id: str, owner: str, fencing_token: str, now: datetime, lease_seconds: int
    ) -> LeaseRecord:
        """Renew only an unexpired lease held by ``owner``."""

    async def complete(self, completion: LeaseCompletion, *, now: datetime) -> LeaseRecord:
        """Fence and complete a job owned by the caller."""

    async def get(self, *, job_id: str) -> LeaseRecord:
        """Read stable lease state without changing it."""
