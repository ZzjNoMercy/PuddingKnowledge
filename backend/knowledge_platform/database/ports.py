"""Framework-neutral ports for the two-phase Database Query capability.

The Database Query Plane owns identity, authorization, query plans and
evidence.  A Vanna implementation, SQL driver, ORM, Graph runtime or web
framework may implement the ports, but none of those dependencies belong in
this contract layer.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol

from knowledge_contracts import Evidence, QueryPlan, QueryPlanValidation

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SUBJECT_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_MAX_TABLES = 500
_MAX_ROWS = 500


def _check_id(value: str, field_name: str) -> None:
    if not _ID_RE.fullmatch(value):
        raise ValueError(f"{field_name} is invalid")


@dataclass(frozen=True, slots=True)
class DatabaseDatasetBinding:
    """Current, authorized facts required to compile or execute a plan."""

    dataset_id: str
    space_id: str
    dataset_version: str
    deployment_revision: str
    dialect: str
    allowed_tables: tuple[str, ...]
    semantic_context_hash: str
    source_revision: str
    provider_version: str = ""

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.dataset_id, "dataset_id"),
            (self.space_id, "space_id"),
            (self.dataset_version, "dataset_version"),
            (self.deployment_revision, "deployment_revision"),
            (self.dialect, "dialect"),
            (self.source_revision, "source_revision"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        _check_id(self.dataset_id, "dataset_id")
        _check_id(self.space_id, "space_id")
        if not _DIGEST_RE.fullmatch(self.semantic_context_hash):
            raise ValueError("semantic_context_hash must be a sha256 digest")
        if not _DIGEST_RE.fullmatch(self.source_revision):
            raise ValueError("source_revision must be a sha256 digest")
        if not self.allowed_tables or len(self.allowed_tables) > _MAX_TABLES:
            raise ValueError("allowed_tables is invalid")
        if any(not table.strip() or len(table) > 256 for table in self.allowed_tables):
            raise ValueError("allowed_tables contains an invalid table")
        if len(set(self.allowed_tables)) != len(self.allowed_tables):
            raise ValueError("allowed_tables must be unique")


@dataclass(frozen=True, slots=True)
class DatabaseSqlCandidate:
    """A provider candidate; it is not executable until the service validates it."""

    sql: str
    evidence: tuple[Evidence, ...] = ()
    provider_version: str = ""

    def __post_init__(self) -> None:
        if not self.sql.strip() or len(self.sql) > 256 * 1024:
            raise ValueError("SQL candidate is invalid")
        if len(self.evidence) > 100:
            raise ValueError("SQL candidate evidence is too large")


@dataclass(frozen=True, slots=True)
class DatabaseExecution:
    """Bounded read-only execution result returned by a trusted provider."""

    columns: tuple[str, ...]
    rows: tuple[Mapping[str, object], ...]
    row_count: int
    limited: bool = False
    result_id: str | None = None

    def __post_init__(self) -> None:
        if len(self.columns) > 200 or len(self.rows) > _MAX_ROWS:
            raise ValueError("database result exceeds the Platform bound")
        if len(set(self.columns)) != len(self.columns) or any(not column.strip() for column in self.columns):
            raise ValueError("database result columns are invalid")
        if any(_SECRET_RE.search(column) for column in self.columns):
            raise ValueError("database result contains a secret-bearing column")
        if type(self.row_count) is not int or self.row_count < len(self.rows):
            raise ValueError("database result row_count is invalid")
        for row in self.rows:
            if not isinstance(row, Mapping) or set(row) - set(self.columns):
                raise ValueError("database result row has unknown columns")
            if any(
                not isinstance(key, str)
                or _SECRET_RE.search(key)
                or not isinstance(value, (type(None), str, int, float, bool))
                or (isinstance(value, float) and not math.isfinite(value))
                or (isinstance(value, str) and (len(value) > 1000 or _SECRET_RE.search(value)))
                for key, value in row.items()
            ):
                raise ValueError("database result contains a non-portable or secret value")
        if self.result_id is not None:
            _check_id(self.result_id, "result_id")


class DatabaseDatasetResolver(Protocol):
    def resolve(self, *, dataset_id: str, space_id: str) -> DatabaseDatasetBinding | None:
        """Return the current binding, or None without leaking hidden datasets."""


@dataclass(frozen=True, slots=True)
class DatabaseSchemaTable:
    """Portable schema facts for one allowlisted table."""

    table_name: str
    columns: tuple[str, ...]
    schema_revision: str

    def __post_init__(self) -> None:
        if not table_name_is_safe(self.table_name):
            raise ValueError("database schema table name is invalid")
        if not self.columns or len(self.columns) > 200 or len(set(self.columns)) != len(self.columns):
            raise ValueError("database schema columns are invalid")
        if any(not isinstance(column, str) or not column.strip() or len(column) > 256 for column in self.columns):
            raise ValueError("database schema column is invalid")
        if any(_SECRET_RE.search(column) for column in self.columns):
            raise ValueError("database schema contains a secret-bearing column")
        if not _DIGEST_RE.fullmatch(self.schema_revision):
            raise ValueError("database schema revision is invalid")


def table_name_is_safe(value: str) -> bool:
    """Keep schema descriptors to portable one- or two-part identifiers."""

    return bool(re.fullmatch(r"[A-Za-z0-9_$-]{1,128}(?:\.[A-Za-z0-9_$-]{1,128})?", value))


class DatabaseSchemaReader(Protocol):
    def read(self, *, binding: DatabaseDatasetBinding) -> Sequence[DatabaseSchemaTable]:
        """Return current, bounded schema facts for the binding allowlist."""


class DatabaseNl2SqlProvider(Protocol):
    def generate(
        self,
        *,
        question: str,
        binding: DatabaseDatasetBinding,
        semantic_asset_ids: Sequence[str],
    ) -> DatabaseSqlCandidate:
        """Generate a candidate using grounded evidence; never execute it."""


class DatabaseSqlValidator(Protocol):
    def validate(
        self,
        *,
        sql: str,
        dialect: str,
        allowed_tables: Sequence[str],
    ) -> QueryPlanValidation:
        """Validate read-only, table-scope and business guardrail constraints."""


class ReadonlyDatabaseExecutor(Protocol):
    def execute(
        self,
        *,
        plan: QueryPlan,
        space_id: str,
        allowed_tables: Sequence[str],
        page_size: int,
    ) -> DatabaseExecution:
        """Execute only a previously issued and revalidated QueryPlan."""


@dataclass(frozen=True, slots=True)
class StoredQueryPlan:
    """Repository record carrying authorization facts not exposed in QueryPlan."""

    plan: QueryPlan
    owner_subject_id: str
    owner_scope_digest: str
    allowed_tables: tuple[str, ...] = field(default_factory=tuple)
    source_revision: str = ""
    semantic_asset_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _SUBJECT_RE.fullmatch(self.owner_subject_id):
            raise ValueError("owner_subject_id is invalid")
        if not _DIGEST_RE.fullmatch(self.owner_scope_digest) or not self.source_revision.strip():
            raise ValueError("stored query plan binding is invalid")
        if not self.allowed_tables:
            raise ValueError("stored query plan allowed_tables is empty")
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in self.semantic_asset_ids):
            raise ValueError("stored query plan semantic_asset_ids are invalid")


class QueryPlanRepository(Protocol):
    def put(self, *, stored: StoredQueryPlan) -> None: ...

    def get(self, *, query_plan_id: str) -> StoredQueryPlan | None: ...


class QueryResultRepository(Protocol):
    def put(self, *, result: DatabaseExecution, space_id: str, owner_subject_id: str) -> str: ...


@dataclass(frozen=True, slots=True)
class DatabaseEvidenceRecord:
    """Platform-owned evidence facts; no Session/Run/Goal receipt identity."""

    evidence_id: str
    space_id: str
    dataset_id: str
    owner_subject_id: str
    source_revision: str
    allowed_tables: tuple[str, ...]
    evidence: tuple[Evidence, ...]
    expires_at: str

    def __post_init__(self) -> None:
        for value, field_name in (
            (self.evidence_id, "evidence_id"),
            (self.space_id, "space_id"),
            (self.dataset_id, "dataset_id"),
            (self.expires_at, "expires_at"),
        ):
            if not value.strip():
                raise ValueError(f"{field_name} must not be empty")
        _check_id(self.evidence_id, "evidence_id")
        _check_id(self.space_id, "space_id")
        _check_id(self.dataset_id, "dataset_id")
        if not _SUBJECT_RE.fullmatch(self.owner_subject_id) or not _DIGEST_RE.fullmatch(self.source_revision):
            raise ValueError("database evidence identity is invalid")
        try:
            expiry = datetime.fromisoformat(self.expires_at)
        except ValueError as error:
            raise ValueError("database evidence expiry is invalid") from error
        if expiry.tzinfo is None:
            raise ValueError("database evidence expiry must include a timezone")
        if not self.allowed_tables or len(self.allowed_tables) > _MAX_TABLES:
            raise ValueError("database evidence table scope is invalid")
        if len(self.evidence) > 100:
            raise ValueError("database evidence is too large")


class DatabaseEvidenceRepository(Protocol):
    def put(self, *, record: DatabaseEvidenceRecord) -> None: ...

    def get(
        self,
        *,
        evidence_id: str,
        owner_subject_id: str,
        space_id: str,
        dataset_id: str,
        source_revision: str,
        allowed_tables: Sequence[str],
    ) -> DatabaseEvidenceRecord | None: ...


@dataclass(frozen=True, slots=True)
class DatabaseSchemaEvidence:
    """Schema observation stored by Platform with bounded table scope."""

    space_id: str
    dataset_id: str
    owner_subject_id: str
    table_name: str
    columns: tuple[str, ...]
    schema_revision: str
    evidence: tuple[Evidence, ...] = ()

    def __post_init__(self) -> None:
        _check_id(self.space_id, "schema evidence space_id")
        _check_id(self.dataset_id, "schema evidence dataset_id")
        if not _SUBJECT_RE.fullmatch(self.owner_subject_id):
            raise ValueError("schema evidence owner_subject_id is invalid")
        if not self.table_name.strip() or len(self.table_name) > 256:
            raise ValueError("schema evidence table_name is invalid")
        if not self.columns or len(self.columns) > 200 or len(set(self.columns)) != len(self.columns):
            raise ValueError("schema evidence columns are invalid")
        if not _DIGEST_RE.fullmatch(self.schema_revision):
            raise ValueError("schema evidence revision is invalid")
        if any(_SECRET_RE.search(column) for column in self.columns):
            raise ValueError("schema evidence exposes a secret-bearing column")


class DatabaseSchemaEvidenceRepository(Protocol):
    def put(self, *, record: DatabaseSchemaEvidence) -> None: ...

    def get(
        self,
        *,
        space_id: str,
        dataset_id: str,
        owner_subject_id: str,
        table_name: str,
        schema_revision: str,
    ) -> DatabaseSchemaEvidence | None: ...


class VannaEvidenceProvider(DatabaseNl2SqlProvider, Protocol):
    """Named seam for the vendored Vanna adapter, not a public domain object."""
