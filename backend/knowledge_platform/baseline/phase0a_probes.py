"""Deterministic, side-effect-free probes for Platform contract coverage.

These probes exercise the extracted Platform contracts and ports only.  They
are useful runtime observations, but they intentionally do not claim to cover
legacy Claw, Vanna, database, or production worker paths.
"""

from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from knowledge_contracts import (
    AgentProtocolEnvelope,
    CapabilityDescriptor,
    CapabilityInventory,
    CitationCandidate,
    Correlation,
    LeaseCompletion,
    LeaseStatus,
    Principal,
    QueryPlan,
    QueryPlanValidation,
)
from knowledge_platform.agent import AgentCapabilitySurfaceBuilder
from knowledge_platform.catalog.models import KnowledgeAuthoringEvent, KnowledgeAuthoringJob
from knowledge_platform.catalog.rehearsal import build_table_snapshot
from knowledge_platform.evidence.normalizer import DeterministicCitationNormalizer
from knowledge_platform.jobs import InMemoryLeaseStore
from knowledge_platform.wiki import (
    RawSnapshot,
    WikiCompilationClaim,
    WikiCompilationRequest,
    WikiCompilationWorker,
    WikiDraft,
    WikiValidationResult,
)

_NOW = datetime(2026, 1, 1, tzinfo=timezone.utc)
_CONTENT_DIGEST = "sha256:" + "a" * 64


def _run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


async def _lease_observation() -> dict[str, object]:
    store = InMemoryLeaseStore()
    await store.add("probe-job")
    claimed = await store.claim_next(owner="probe-owner", now=_NOW, lease_seconds=60)
    if claimed is None:
        raise RuntimeError("lease probe did not claim its queued job")
    completed = await store.complete(
        LeaseCompletion(
            job_id="probe-job",
            owner="probe-owner",
            status=LeaseStatus.SUCCEEDED,
            fencing_token=claimed.fencing_token,
        ),
        now=_NOW,
    )
    claimed_observation = asdict(claimed)
    # Fencing tokens are intentionally per-claim random values.  They prove
    # ownership to the lease adapter but must not make a replay artifact
    # nondeterministic or persist a live token.
    claimed_observation["fencing_token"] = "fencing-token-redacted"
    return {"claimed": claimed_observation, "completed": asdict(completed)}


def probe_knowledge_catalog_and_processing() -> dict[str, object]:
    snapshot = build_table_snapshot(
        "knowledge_assets",
        [{"id": "asset-1", "title": "probe", "credential_ref": "vault://ref"}],
        primary_key_fields=("id",),
        secret_fields=("credential_ref",),
    )
    lease = _run(_lease_observation())
    return {"table_snapshot": asdict(snapshot), "lease": lease}


def probe_retrieval_and_evidence() -> dict[str, object]:
    candidate = CitationCandidate(
        asset_id="asset-1",
        resource_uri="knowledge://assets/asset-1",
        quote="bounded probe evidence",
        locator={"page": "1"},
        score=1.0,
    )
    evidence = DeterministicCitationNormalizer().normalize((candidate, candidate))
    if len(evidence) != 1:
        raise RuntimeError("retrieval probe did not deduplicate evidence")
    return {"evidence": [asdict(item) for item in evidence]}


class _ProbeSnapshots:
    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        return RawSnapshot(snapshot_id, source_revision, "knowledge://snapshots/1", "probe content", _CONTENT_DIGEST)


class _ProbeContext:
    async def build_context(self, snapshot: RawSnapshot) -> str:
        return snapshot.content


class _ProbeModel:
    async def generate(self, *, context: str, snapshot: RawSnapshot) -> WikiDraft:
        return WikiDraft("probe/page.md", "Probe", context, snapshot.snapshot_id, snapshot.source_revision)


class _ProbeValidator:
    async def validate(self, draft: WikiDraft, *, snapshot: RawSnapshot) -> WikiValidationResult:
        return WikiValidationResult(True, receipt_id="receipt-probe")


class _ProbePublisher:
    async def publish(self, draft: Any, *, snapshot: RawSnapshot, idempotency_key: str) -> str:
        return "knowledge://wiki/probe-page"


class _ProbeJobs:
    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        return WikiCompilationClaim(True)

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        return None

    async def release(self, *, idempotency_key: str) -> None:
        return None


def probe_wiki_compilation() -> dict[str, object]:
    worker = WikiCompilationWorker(
        snapshots=_ProbeSnapshots(),
        context=_ProbeContext(),
        model=_ProbeModel(),
        validator=_ProbeValidator(),
        publisher=_ProbePublisher(),
        jobs=_ProbeJobs(),
    )
    request = WikiCompilationRequest(
        snapshot_id="snapshot-1",
        source_revision="revision-1",
        source_uri="knowledge://snapshots/1",
        content_digest=_CONTENT_DIGEST,
        idempotency_key="probe-compile-1",
    )
    result = _run(worker.compile(request))
    return {"result": asdict(result)}


def probe_structured_query_and_vanna_boundary() -> dict[str, object]:
    principal = Principal("probe-user", scopes=("knowledge.query",), tenant_id="tenant-1")
    correlation = Correlation("probe-trace", request_id="probe-request")
    envelope, dropped = AgentProtocolEnvelope.from_legacy(
        principal=principal,
        correlation=correlation,
        payload={"query": "probe", "analytics_model_id": "legacy-model", "nested": {"analytics_model_id": "old"}},
    )
    plan = QueryPlan(
        query_plan_id="probe-plan",
        sql="SELECT 1",
        dialect="sqlite",
        dataset_id="dataset-1",
        dataset_version="v1",
        deployment_revision="deploy-1",
        semantic_context_hash=_CONTENT_DIGEST,
        validation=QueryPlanValidation(True, True, True),
        expires_at="2026-01-01T00:01:00+00:00",
    )
    if "analytics_model_id" in str(envelope.payload) or not dropped:
        raise RuntimeError("protocol probe did not remove the legacy Analytics field")
    return {"dropped_fields": list(dropped), "query_plan_id": plan.query_plan_id}


def probe_semantic_authoring() -> dict[str, object]:
    job = KnowledgeAuthoringJob(
        id="probe-authoring-job",
        dimension_id="dimension-1",
        adapter="probe-adapter",
        scope_uri="knowledge://spaces/space-1",
        input_snapshot_json={"revision": "source-1"},
        created_at=_NOW,
        updated_at=_NOW,
    )
    event = KnowledgeAuthoringEvent(
        id="probe-authoring-event",
        job_id=job.id,
        level="info",
        message="probe",
        metadata_json={"status": "queued"},
        created_at=_NOW,
    )
    return {"job_table": job.__table__.name, "event_table": event.__table__.name, "job_id": job.id}


class _ProbeDiscovery:
    async def discover(self, *, principal: Principal) -> CapabilityInventory:
        return CapabilityInventory(
            capabilities=(
                CapabilityDescriptor("probe/read", "probe-provider", "read", True, True, ("knowledge",)),
            ),
            issuer_id="probe-authority",
        )


class _ProbeInvoker:
    async def invoke(self, capability: CapabilityDescriptor, arguments: dict[str, object], *, principal: Principal, correlation: Correlation) -> dict[str, object]:
        return {"capability": capability.capability_id, "status": "ok"}


def probe_harness_wiring_boundary() -> dict[str, object]:
    principal = Principal("probe-user")
    correlation = Correlation("probe-trace")
    surface = _run(
        AgentCapabilitySurfaceBuilder(
            _ProbeDiscovery(), trusted_issuer_ids=frozenset({"probe-authority"})
        ).build(principal=principal, correlation=correlation)
    )
    result = _run(surface.invoke("probe/read", {"resource_uri": "knowledge://assets/asset-1"}, invoker=_ProbeInvoker()))
    return {"capabilities": [item.capability_id for item in surface.capabilities], "result": result}
