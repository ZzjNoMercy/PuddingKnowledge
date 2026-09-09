"""Deterministic reference implementation for the Platform lease port.

This adapter is for contract/concurrency tests, not production persistence.
Every operation receives ``now`` explicitly so tests and database adapters
cannot silently depend on a worker host clock.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta

from knowledge_contracts import LeaseCompletion, LeaseLostError, LeaseRecord, LeaseStatus, new_fencing_token


class InMemoryLeaseStore:
    def __init__(self) -> None:
        self._jobs: dict[str, LeaseRecord] = {}
        self._lock = asyncio.Lock()

    async def add(self, job_id: str, *, created: bool = True) -> None:
        del created  # Keeps the fixture API explicit without adding a second state machine.
        async with self._lock:
            if job_id in self._jobs:
                raise ValueError(f"duplicate job: {job_id}")
            self._jobs[job_id] = LeaseRecord(job_id, LeaseStatus.QUEUED, None, None, 0)

    async def claim_next(self, *, owner: str, now: datetime, lease_seconds: int) -> LeaseRecord | None:
        if not owner.strip() or lease_seconds <= 0:
            raise ValueError("owner and positive lease_seconds are required")
        async with self._lock:
            candidates = [
                job
                for job in self._jobs.values()
                if job.status is LeaseStatus.QUEUED
                or (job.status is LeaseStatus.RUNNING and job.expires_at is not None and _parse(job.expires_at) <= now)
            ]
            if not candidates:
                return None
            selected = min(candidates, key=lambda job: (job.attempt, job.job_id))
            claimed = LeaseRecord(
                selected.job_id,
                LeaseStatus.RUNNING,
                owner,
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                selected.attempt + 1,
                new_fencing_token(),
            )
            self._jobs[selected.job_id] = claimed
            return claimed

    async def heartbeat(
        self, *, job_id: str, owner: str, fencing_token: str, now: datetime, lease_seconds: int
    ) -> LeaseRecord:
        async with self._lock:
            current = self._require_live(job_id, owner, fencing_token, now)
            renewed = LeaseRecord(
                current.job_id,
                LeaseStatus.RUNNING,
                owner,
                (now + timedelta(seconds=lease_seconds)).isoformat(),
                current.attempt,
                current.fencing_token,
            )
            self._jobs[job_id] = renewed
            return renewed

    async def complete(self, completion: LeaseCompletion, *, now: datetime) -> LeaseRecord:
        async with self._lock:
            current = self._require_live(completion.job_id, completion.owner, completion.fencing_token, now)
            completed = LeaseRecord(completion.job_id, completion.status, None, None, current.attempt, None)
            self._jobs[completion.job_id] = completed
            return completed

    async def get(self, *, job_id: str) -> LeaseRecord:
        async with self._lock:
            try:
                return self._jobs[job_id]
            except KeyError as exc:
                raise KeyError(f"unknown job: {job_id}") from exc

    def _require_live(self, job_id: str, owner: str, fencing_token: str | None, now: datetime) -> LeaseRecord:
        current = self._jobs.get(job_id)
        if (
            current is None
            or current.status is not LeaseStatus.RUNNING
            or current.owner != owner
            or current.fencing_token != fencing_token
            or current.expires_at is None
            or _parse(current.expires_at) <= now
        ):
            raise LeaseLostError(f"lease lost for job {job_id} (owner {owner})")
        return current


def _parse(value: str) -> datetime:
    return datetime.fromisoformat(value)
