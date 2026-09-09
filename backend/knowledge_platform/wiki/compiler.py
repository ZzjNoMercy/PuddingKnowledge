"""Application worker for Raw Snapshot -> validated Published Wiki."""

from __future__ import annotations

from dataclasses import dataclass

from knowledge_contracts import is_valid_knowledge_uri

from .ports import (
    ModelGateway,
    RawSnapshotRepository,
    ValidatedWikiDraft,
    WikiCompilationJobStore,
    WikiContextService,
    WikiDraftValidator,
    WikiPublishingService,
)


class WikiCompilationError(RuntimeError):
    """Compilation stopped before publishing a valid Wiki draft."""


@dataclass(frozen=True, slots=True)
class WikiCompilationRequest:
    snapshot_id: str
    source_revision: str
    source_uri: str
    content_digest: str
    idempotency_key: str

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "source_revision", "source_uri", "content_digest", "idempotency_key"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"WikiCompilationRequest.{field_name} must not be empty")
        if not is_valid_knowledge_uri(self.source_uri):
            raise ValueError("WikiCompilationRequest.source_uri must use knowledge://")


@dataclass(frozen=True, slots=True)
class WikiCompilationResult:
    snapshot_id: str
    source_revision: str
    resource_uri: str

    def __post_init__(self) -> None:
        for field_name in ("snapshot_id", "source_revision"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"WikiCompilationResult.{field_name} must not be empty")
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("WikiCompilationResult.resource_uri must use knowledge://")


class WikiCompilationWorker:
    """Coordinate ports; never instantiate a public Tool or mutate raw input."""

    def __init__(
        self,
        *,
        snapshots: RawSnapshotRepository,
        context: WikiContextService,
        model: ModelGateway,
        validator: WikiDraftValidator,
        publisher: WikiPublishingService,
        jobs: WikiCompilationJobStore,
    ) -> None:
        self._snapshots = snapshots
        self._context = context
        self._model = model
        self._validator = validator
        self._publisher = publisher
        self._jobs = jobs

    async def compile(self, request: WikiCompilationRequest) -> WikiCompilationResult:
        claim = await self._jobs.claim(idempotency_key=request.idempotency_key)
        if not claim.acquired:
            if claim.existing_resource_uri is None:
                raise WikiCompilationError("Wiki compilation is already in progress")
            return WikiCompilationResult(request.snapshot_id, request.source_revision, claim.existing_resource_uri)
        try:
            snapshot = await self._snapshots.get(
                snapshot_id=request.snapshot_id, source_revision=request.source_revision
            )
            if (snapshot.snapshot_id, snapshot.source_revision) != (request.snapshot_id, request.source_revision):
                raise WikiCompilationError("Raw snapshot identity does not match compilation request")
            if (snapshot.source_uri, snapshot.content_digest) != (request.source_uri, request.content_digest):
                raise WikiCompilationError("Raw snapshot source fingerprint does not match compilation request")
            context = await self._context.build_context(snapshot)
            draft = await self._model.generate(context=context, snapshot=snapshot)
            if (draft.source_snapshot_id, draft.source_revision) != (snapshot.snapshot_id, snapshot.source_revision):
                raise WikiCompilationError("Wiki draft source identity does not match raw snapshot")
            validation = await self._validator.validate(draft, snapshot=snapshot)
            if not validation.valid:
                raise WikiCompilationError("Wiki draft validation failed: " + "; ".join(validation.errors))
            validated_draft = ValidatedWikiDraft(draft=draft, receipt_id=validation.receipt_id)
            resource_uri = await self._publisher.publish(
                validated_draft,
                snapshot=snapshot,
                idempotency_key=request.idempotency_key,
            )
            if not is_valid_knowledge_uri(resource_uri):
                raise WikiCompilationError("Wiki publisher returned a non-portable resource URI")
            await self._jobs.complete(idempotency_key=request.idempotency_key, resource_uri=resource_uri)
            return WikiCompilationResult(snapshot.snapshot_id, snapshot.source_revision, resource_uri)
        except Exception:
            await self._jobs.release(idempotency_key=request.idempotency_key)
            raise
