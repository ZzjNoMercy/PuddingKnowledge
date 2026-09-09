"""Portable v1 query and evidence contracts.

The contract layer is deliberately smaller than any current implementation.
It describes the facts that cross a process boundary, not how a provider or
Agent happens to produce them.  Dataclasses keep this module usable by a
future Platform repository without importing FastAPI, Pydantic, SQLAlchemy,
LangChain, LangGraph, or PuddingClaw graph state.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from enum import StrEnum
from typing import Any

from ._immutables import freeze_mapping, thaw_value

_KNOWLEDGE_URI_RE = re.compile(
    r"^knowledge://[A-Za-z0-9][A-Za-z0-9._-]{0,95}(?:/[A-Za-z0-9][A-Za-z0-9._-]{0,159})+$"
)
_OPAQUE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SECRET_TEXT_RE = re.compile(
    r"(?i)(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|"
    r"(?:https?://|file:|[A-Za-z]:[\\/]|\\\\|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/)"
)
_EVIDENCE_LOCATOR_KEYS = frozenset({"page", "line_start", "line_end", "section", "chunk_id"})


def is_valid_knowledge_uri(value: str) -> bool:
    """Accept only portable, path-segmented resource identities."""

    return bool(_KNOWLEDGE_URI_RE.fullmatch(value))


class Capability(StrEnum):
    KNOWLEDGE_LIST = "knowledge_list"
    KNOWLEDGE_SEARCH = "knowledge_search"
    KNOWLEDGE_READ = "knowledge_read"
    KNOWLEDGE_QUERY = "knowledge_query"
    DOCUMENT_RAG_QUERY = "document_rag_query"
    WIKI_QUERY = "wiki_query"
    TABLE_QUERY = "table_query"
    SEMANTIC_AUTHORING = "semantic_authoring"
    CAPTURE_PROCESSING = "capture_processing"
    CONNECTOR_SYNC = "connector_sync"
    GBRAIN_PROJECTION = "gbrain_projection"
    SEMANTIC_MARKDOWN_ADMIN = "semantic_markdown_admin"
    SQL_GUARDRAIL_ADMIN = "sql_guardrail_admin"
    DATABASE_NL2SQL = "database_nl2sql"
    DATABASE_EXECUTE_READONLY = "database_execute_readonly"
    DATABASE_SCHEMA = "database_schema"


CAPABILITIES: tuple[str, ...] = tuple(capability.value for capability in Capability)


class QueryErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    NOT_FOUND = "not_found"
    PERMISSION_DENIED = "permission_denied"
    CAPABILITY_UNAVAILABLE = "capability_unavailable"
    BINDING_UNAVAILABLE = "binding_unavailable"
    INDEX_NOT_READY = "index_not_ready"
    QUERY_GENERATION_FAILED = "query_generation_failed"
    QUERY_VALIDATION_FAILED = "query_validation_failed"
    QUERY_EXECUTION_FAILED = "query_execution_failed"
    RESOURCE_LIMIT_EXCEEDED = "resource_limit_exceeded"
    STALE_QUERY_PLAN = "stale_query_plan"
    STALE_DEPLOYMENT_REVISION = "stale_deployment_revision"
    INTERNAL_ERROR = "internal_error"


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True, slots=True)
class Principal:
    """Caller identity established by the Platform boundary."""

    subject_id: str
    scopes: tuple[str, ...] = ()
    tenant_id: str | None = None

    def __post_init__(self) -> None:
        if not self.subject_id.strip():
            raise ValueError("Principal.subject_id must not be empty")


@dataclass(frozen=True, slots=True)
class Correlation:
    """Opaque tracing metadata; never an authorization source."""

    trace_id: str
    request_id: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.trace_id, str) or not self.trace_id.strip():
            raise ValueError("Correlation.trace_id must be a short opaque identifier")
        for field_name in ("trace_id", "request_id"):
            value = getattr(self, field_name)
            if value is not None and (
                not value.strip() or not _OPAQUE_ID_RE.fullmatch(value) or _SECRET_TEXT_RE.search(value)
            ):
                raise ValueError(f"Correlation.{field_name} must be a short opaque identifier")


@dataclass(frozen=True, slots=True)
class Evidence:
    asset_id: str
    resource_uri: str
    locator: Mapping[str, Any] = field(default_factory=dict)
    quote: str = ""
    score: float | None = None
    revision: str = ""
    matched_by: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not _OPAQUE_ID_RE.fullmatch(self.asset_id) or _SECRET_TEXT_RE.search(self.asset_id):
            raise ValueError("Evidence.asset_id must be a short opaque identifier")
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("Evidence.resource_uri must use knowledge://")
        if not isinstance(self.locator, Mapping):
            raise ValueError("Evidence.locator must be a mapping")
        for key, value in self.locator.items():
            if str(key) not in _EVIDENCE_LOCATOR_KEYS:
                raise ValueError("Evidence.locator contains an unsupported field")
            if isinstance(value, str):
                if (
                    not value.strip()
                    or len(value) > 256
                    or any(ord(character) < 32 for character in value)
                    or _SECRET_TEXT_RE.search(value)
                ):
                    raise ValueError("Evidence.locator contains a non-portable value")
            elif isinstance(value, float) and not math.isfinite(value):
                raise ValueError("Evidence.locator contains a non-portable value")
            elif not isinstance(value, (int, float, bool)):
                raise ValueError("Evidence.locator values must be scalar")
        object.__setattr__(self, "locator", freeze_mapping(self.locator))
        if self.quote and (
            len(self.quote) > 1200
            or any(ord(character) < 32 and character not in "\t\n\r" for character in self.quote)
            or _SECRET_TEXT_RE.search(self.quote)
        ):
            raise ValueError("Evidence.quote contains a non-portable value")
        if self.revision and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.revision):
            raise ValueError("Evidence.revision must be a sha256 digest")
        if self.score is not None and (not math.isfinite(self.score) or not 0 <= self.score <= 1):
            raise ValueError("Evidence.score must be between 0 and 1")
        if any(
            not isinstance(value, str)
            or not _OPAQUE_ID_RE.fullmatch(value)
            or _SECRET_TEXT_RE.search(value)
            for value in self.matched_by
        ):
            raise ValueError("Evidence.matched_by values must be short opaque identifiers")


@dataclass(frozen=True, slots=True)
class Provenance:
    space_id: str
    dataset_id: str | None
    dataset_version: str | None
    capability: str
    provider_versions: Mapping[str, str] = field(default_factory=dict)
    catalog_revision: str | None = None

    def __post_init__(self) -> None:
        if self.capability not in CAPABILITIES:
            raise ValueError(f"Unsupported capability: {self.capability}")
        if not isinstance(self.provider_versions, Mapping):
            raise ValueError("Provenance.provider_versions must be a mapping")
        if any(not isinstance(key, str) or not isinstance(value, str) for key, value in self.provider_versions.items()):
            raise ValueError("Provenance.provider_versions keys and values must be strings")
        object.__setattr__(self, "provider_versions", freeze_mapping(self.provider_versions))
        if self.catalog_revision is not None and not re.fullmatch(r"sha256:[0-9a-f]{64}", self.catalog_revision):
            raise ValueError("Provenance.catalog_revision must be a sha256 digest")


@dataclass(frozen=True, slots=True)
class QueryWarning:
    code: str
    message: str
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.code.strip() or not self.message.strip():
            raise ValueError("QueryWarning code and message must not be empty")
        if not isinstance(self.details, Mapping):
            raise ValueError("QueryWarning.details must be a mapping")
        object.__setattr__(self, "details", freeze_mapping(self.details))


@dataclass(frozen=True, slots=True)
class QueryError:
    code: QueryErrorCode
    message: str
    retryable: bool = False
    details: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.message.strip():
            raise ValueError("QueryError.message must not be empty")
        if not isinstance(self.details, Mapping):
            raise ValueError("QueryError.details must be a mapping")
        object.__setattr__(self, "details", freeze_mapping(self.details))


@dataclass(frozen=True, slots=True)
class QueryResult:
    """Unified result envelope for every read/query capability."""

    status: str
    answer: str = ""
    data: Mapping[str, Any] = field(default_factory=dict)
    evidence: tuple[Evidence, ...] = ()
    provenance: Provenance | None = None
    warnings: tuple[QueryWarning, ...] = ()
    trace_id: str = ""
    error: QueryError | None = None

    def __post_init__(self) -> None:
        if self.status not in {"ok", "error"}:
            raise ValueError("QueryResult.status must be ok or error")
        if self.status == "ok" and self.error is not None:
            raise ValueError("Successful QueryResult cannot carry an error")
        if self.status == "error" and self.error is None:
            raise ValueError("Error QueryResult must carry an error")
        if not isinstance(self.data, Mapping):
            raise ValueError("QueryResult.data must be a mapping")
        object.__setattr__(self, "data", freeze_mapping(self.data))
        object.__setattr__(self, "evidence", tuple(self.evidence))
        object.__setattr__(self, "warnings", tuple(self.warnings))

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-compatible representation for adapters."""

        return thaw_value(asdict(self))


@dataclass(frozen=True, slots=True)
class QueryPlanValidation:
    readonly: bool
    allowed_tables: bool
    guardrails_passed: bool

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (self.readonly, self.allowed_tables, self.guardrails_passed)):
            raise TypeError("QueryPlanValidation fields must be bools")

    @property
    def passed(self) -> bool:
        return self.readonly and self.allowed_tables and self.guardrails_passed


@dataclass(frozen=True, slots=True)
class QueryPlan:
    """A short-lived, server-issued plan for two-phase database querying."""

    query_plan_id: str
    sql: str
    dialect: str
    dataset_id: str
    dataset_version: str
    deployment_revision: str
    semantic_context_hash: str
    validation: QueryPlanValidation
    expires_at: str
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        for field_name in (
            "query_plan_id",
            "sql",
            "dialect",
            "dataset_id",
            "dataset_version",
            "deployment_revision",
            "semantic_context_hash",
            "expires_at",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"QueryPlan.{field_name} must not be empty")
        if not self.validation.passed:
            raise ValueError("QueryPlan validation must pass before issuance")

    def to_dict(self) -> dict[str, Any]:
        return thaw_value(asdict(self))


@dataclass(frozen=True, slots=True)
class Job:
    """Portable processing job identity; session/run IDs are not required."""

    job_id: str
    kind: str
    status: JobStatus
    space_id: str
    progress: int = 0
    revision: str | None = None
    error: QueryError | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip() or not self.kind.strip() or not self.space_id.strip():
            raise ValueError("Job identity fields must not be empty")
        if not 0 <= self.progress <= 100:
            raise ValueError("Job.progress must be between 0 and 100")
        if self.status == JobStatus.FAILED and self.error is None:
            raise ValueError("Failed Job must carry an error")

    def to_dict(self) -> dict[str, Any]:
        return thaw_value(asdict(self))
