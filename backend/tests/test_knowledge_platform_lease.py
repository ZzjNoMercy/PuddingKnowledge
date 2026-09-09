from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest


def test_lease_contract_rejects_unfenced_running_state() -> None:
    from knowledge_contracts import LeaseRecord, LeaseStatus

    with pytest.raises(ValueError, match="owner, expires_at"):
        LeaseRecord("job_1", LeaseStatus.RUNNING, None, None, 1)


def test_in_memory_lease_store_claims_atomically_and_fences_reclaimed_owner() -> None:
    from knowledge_contracts import LeaseCompletion, LeaseLostError, LeaseStatus
    from knowledge_platform.jobs import InMemoryLeaseStore

    async def run() -> None:
        store = InMemoryLeaseStore()
        await store.add("job_1")
        await store.add("job_2")
        start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        claims = await asyncio.gather(
            store.claim_next(owner="worker_a", now=start, lease_seconds=10),
            store.claim_next(owner="worker_b", now=start, lease_seconds=10),
        )
        assert {claim.job_id for claim in claims if claim is not None} == {"job_1", "job_2"}

        first = await store.get(job_id="job_1")
        assert first.status is LeaseStatus.RUNNING
        owner = first.owner
        assert owner is not None
        assert first.fencing_token is not None
        await store.heartbeat(
            job_id="job_1",
            owner=owner,
            fencing_token=first.fencing_token,
            now=start + timedelta(seconds=5),
            lease_seconds=10,
        )
        assert (
            await store.claim_next(owner="worker_c", now=start + timedelta(seconds=6), lease_seconds=10)
        ) is None
        reclaimed = await store.claim_next(owner="worker_c", now=start + timedelta(seconds=16), lease_seconds=10)
        assert reclaimed is not None and reclaimed.job_id == "job_1" and reclaimed.attempt == 2
        assert reclaimed.fencing_token is not None
        with pytest.raises(LeaseLostError):
            await store.complete(
                LeaseCompletion("job_1", owner, LeaseStatus.SUCCEEDED, first.fencing_token),
                now=start + timedelta(seconds=17),
            )
        completed = await store.complete(
            LeaseCompletion("job_1", "worker_c", LeaseStatus.SUCCEEDED, reclaimed.fencing_token),
            now=start + timedelta(seconds=17),
        )
        assert completed.status is LeaseStatus.SUCCEEDED

    asyncio.run(run())


def test_same_owner_cannot_complete_with_a_stale_fencing_token_after_reclaim() -> None:
    from knowledge_contracts import LeaseCompletion, LeaseLostError, LeaseStatus
    from knowledge_platform.jobs import InMemoryLeaseStore

    async def run() -> None:
        store = InMemoryLeaseStore()
        await store.add("job_1")
        start = datetime(2026, 9, 3, tzinfo=timezone.utc)
        first = await store.claim_next(owner="worker_a", now=start, lease_seconds=1)
        assert first is not None and first.fencing_token is not None
        reclaimed = await store.claim_next(owner="worker_a", now=start + timedelta(seconds=2), lease_seconds=10)
        assert reclaimed is not None and reclaimed.fencing_token is not None
        with pytest.raises(LeaseLostError):
            await store.complete(
                LeaseCompletion("job_1", "worker_a", LeaseStatus.SUCCEEDED, first.fencing_token),
                now=start + timedelta(seconds=3),
            )
        completed = await store.complete(
            LeaseCompletion("job_1", "worker_a", LeaseStatus.SUCCEEDED, reclaimed.fencing_token),
            now=start + timedelta(seconds=3),
        )
        assert completed.status is LeaseStatus.SUCCEEDED

    asyncio.run(run())
