"""Provider-neutral contracts for structured/table queries."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from knowledge_contracts import Principal, is_valid_knowledge_uri

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COLUMN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SECRET_COLUMN_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_UNSAFE_VALUE_RE = re.compile(
    r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)\s*[:=]\s*\S+|"
    r"(?:bearer\s+|sk-|ghp_)[A-Za-z0-9._-]{8,}"
)
_MAX_PREVIEW_KEYS = 200
_MAX_PAYLOAD_BYTES = 256 * 1024


def _json_scalar(value: object) -> bool:
    return value is None or isinstance(value, (str, int, float, bool))


@dataclass(frozen=True, slots=True)
class TableQueryPayload:
    """A bounded provider result; arbitrary executable code never crosses it."""

    asset_id: str
    resource_uri: str
    answer: str
    columns: tuple[str, ...] = ()
    preview_rows: tuple[Mapping[str, object], ...] = ()
    row_count: int | None = None
    score: float | None = None
    content_digest: str = ""
    semantic_context_id: str | None = None
    semantic_context_hash: str | None = None

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.asset_id):
            raise ValueError("TableQueryPayload.asset_id is invalid")
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("TableQueryPayload.resource_uri is invalid")
        if not self.answer.strip() or len(self.answer) > 4000:
            raise ValueError("TableQueryPayload.answer is invalid")
        if _UNSAFE_VALUE_RE.search(self.answer):
            raise ValueError("TableQueryPayload.answer appears to contain a secret")
        if len(self.columns) > 200 or any(not _COLUMN_RE.fullmatch(column) for column in self.columns):
            raise ValueError("TableQueryPayload.columns are invalid")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("TableQueryPayload.columns must be unique")
        if any(_SECRET_COLUMN_RE.search(column) for column in self.columns):
            raise ValueError("TableQueryPayload exposes a secret-bearing column")
        if len(self.preview_rows) > 20:
            raise ValueError("TableQueryPayload.preview_rows is too large")
        for row in self.preview_rows:
            if not isinstance(row, Mapping):
                raise ValueError("TableQueryPayload.preview_rows must contain objects")
            if any(
                not isinstance(key, str)
                or not _COLUMN_RE.fullmatch(key)
                or _SECRET_COLUMN_RE.search(key)
                or not _json_scalar(value)
                for key, value in row.items()
            ):
                raise ValueError("TableQueryPayload.preview_rows contains unsafe values")
            if len(row) > _MAX_PREVIEW_KEYS or not set(row).issubset(set(self.columns)):
                raise ValueError("TableQueryPayload.preview_rows has too many or unknown columns")
            if any(isinstance(value, str) and _UNSAFE_VALUE_RE.search(value) for value in row.values()):
                raise ValueError("TableQueryPayload.preview_rows appears to contain a secret")
            if any(isinstance(value, str) and len(value) > 1000 for value in row.values()):
                raise ValueError("TableQueryPayload.preview_rows contains an oversized cell")
        if self.row_count is not None and (type(self.row_count) is not int or self.row_count < 0):
            raise ValueError("TableQueryPayload.row_count is invalid")
        if self.score is not None and (type(self.score) is not float or not 0 <= self.score <= 1):
            raise ValueError("TableQueryPayload.score is invalid")
        if not _DIGEST_RE.fullmatch(self.content_digest):
            raise ValueError("TableQueryPayload.content_digest is invalid")
        for field_name in ("semantic_context_id",):
            value = getattr(self, field_name)
            if value is not None and not _ID_RE.fullmatch(value):
                raise ValueError(f"TableQueryPayload.{field_name} is invalid")
        if self.semantic_context_hash is not None and not _DIGEST_RE.fullmatch(self.semantic_context_hash):
            raise ValueError("TableQueryPayload.semantic_context_hash is invalid")
        try:
            encoded_size = len(json.dumps(self.preview_rows, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        except (TypeError, ValueError) as error:
            raise ValueError("TableQueryPayload.preview_rows is not JSON-safe") from error
        if encoded_size > _MAX_PAYLOAD_BYTES:
            raise ValueError("TableQueryPayload.preview_rows is too large")


class StructuredQueryProviderError(RuntimeError):
    """A local or remote structured query provider failed."""


@dataclass(frozen=True, slots=True)
class StructuredSourceProfile:
    """Bounded, path-free facts observed from one structured source file."""

    columns: tuple[str, ...]
    row_count: int
    content_digest: str

    def __post_init__(self) -> None:
        if any(not _COLUMN_RE.fullmatch(column) for column in self.columns):
            raise ValueError("StructuredSourceProfile.columns are invalid")
        if len(set(self.columns)) != len(self.columns):
            raise ValueError("StructuredSourceProfile.columns must be unique")
        if type(self.row_count) is not int or self.row_count < 0:
            raise ValueError("StructuredSourceProfile.row_count is invalid")
        if not _DIGEST_RE.fullmatch(self.content_digest):
            raise ValueError("StructuredSourceProfile.content_digest is invalid")


class StructuredQueryProvider(Protocol):
    async def query(
        self,
        *,
        query: str,
        asset_id: str | None,
        space_id: str | None,
        limit: int,
        semantic_context: object | None,
    ) -> Sequence[TableQueryPayload]:
        """Return bounded table payloads, never a DataFrame or executable plan."""


class StructuredSourceProfiler(Protocol):
    def inspect_source(self, *, path: object, sheet_name: str | int | None = None) -> StructuredSourceProfile:
        """Inspect one explicitly bound source and return only bounded facts."""


class StructuredFileBindingVerifier(Protocol):
    def verify_file(
        self, *, path: Path, expected_digest: str, expected_size_bytes: int | None
    ) -> StructuredSourceProfile: ...


class StructuredAssetBindingWriter(Protocol):
    def bind_source_asset(
        self,
        *,
        principal: Principal,
        asset_id: str,
        space_id: str,
        expected_content_digest: str,
        profile: StructuredSourceProfile,
    ) -> Mapping[str, object]: ...


class StructuredAssetPublisher(Protocol):
    def publish_logical_dataset(
        self,
        *,
        principal: Principal,
        dataset_id: str,
        expected_definition_digest: str,
        content_digest: str,
        columns: Sequence[str],
        row_count: int,
        source_snapshot: Sequence[Mapping[str, object]],
    ) -> Mapping[str, object]:
        """CAS-publish a validated pending logical dataset as ready."""


class StructuredAssetCatalog(Protocol):
    @property
    def catalog_revision(self) -> str: ...

    def get_structured_asset(self, *, asset_id: str) -> Mapping[str, object] | None: ...


class SemanticContextRegistry(Protocol):
    def resolve(self, *, context_id: str, content_hash: str) -> object | None: ...


@dataclass(frozen=True, slots=True)
class SemanticContextBinding:
    """Minimal binding view accepted from the existing semantic compiler."""

    context_id: str
    content_hash: str
    source_asset_ids: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def from_object(cls, value: object | None) -> SemanticContextBinding | None:
        if value is None:
            return None
        if isinstance(value, Mapping):
            context_id = str(value.get("context_id") or "")
            content_hash = str(value.get("content_hash") or value.get("semantic_context_hash") or "")
            source_value = value.get("source_asset_ids") or ()
        else:
            context_id = str(getattr(value, "context_id", "") or "")
            content_hash = str(getattr(value, "content_hash", "") or "")
            source_value = getattr(value, "source_asset_ids", ()) or ()
        source_ids = tuple(str(item) for item in source_value if str(item))
        if not _ID_RE.fullmatch(context_id) or not _DIGEST_RE.fullmatch(content_hash):
            raise ValueError("semantic_context must expose a valid id and content hash")
        if any(not _ID_RE.fullmatch(item) for item in source_ids):
            raise ValueError("semantic_context source asset ids are invalid")
        return cls(context_id=context_id, content_hash=content_hash, source_asset_ids=source_ids)
