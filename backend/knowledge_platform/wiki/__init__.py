"""Framework-neutral Wiki compilation application boundary."""

from .compiler import WikiCompilationError, WikiCompilationRequest, WikiCompilationResult, WikiCompilationWorker
from .local import (
    BoundedWikiContextService,
    DeterministicWikiModelGateway,
    InMemoryWikiCompilationJobStore,
    LocalImmutableRawSnapshotRepository,
    LocalRawSnapshotRepository,
    LocalWikiDraftValidator,
    LocalWikiPublishingService,
)
from .ports import (
    ModelGateway,
    RawSnapshot,
    RawSnapshotRepository,
    ValidatedWikiDraft,
    WikiCompilationClaim,
    WikiCompilationJobStore,
    WikiContextService,
    WikiDraft,
    WikiDraftValidator,
    WikiPublishingService,
    WikiValidationResult,
)
from .sqlite_jobs import SqliteWikiCompilationJobStore

__all__ = [
    "ModelGateway",
    "RawSnapshot",
    "RawSnapshotRepository",
    "ValidatedWikiDraft",
    "WikiCompilationClaim",
    "WikiCompilationJobStore",
    "WikiCompilationError",
    "WikiCompilationRequest",
    "WikiCompilationResult",
    "WikiCompilationWorker",
    "WikiContextService",
    "WikiDraft",
    "WikiDraftValidator",
    "WikiPublishingService",
    "WikiValidationResult",
    "BoundedWikiContextService",
    "DeterministicWikiModelGateway",
    "InMemoryWikiCompilationJobStore",
    "LocalImmutableRawSnapshotRepository",
    "LocalRawSnapshotRepository",
    "LocalWikiDraftValidator",
    "LocalWikiPublishingService",
    "SqliteWikiCompilationJobStore",
]
