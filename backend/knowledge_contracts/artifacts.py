"""Portable artifact, citation, and trace DTOs."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from hashlib import sha256

from ._immutables import freeze_mapping
from .query import Correlation, Principal, is_valid_knowledge_uri

_OPAQUE_VALUE_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_CITATION_LOCATOR_KEYS = frozenset({"page", "line_start", "line_end", "section", "chunk_id"})
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_QUOTE_RE = re.compile(
    r"(?i)(?:password|api[_ -]?key|secret|token|authorization|cookie|private[_ -]?key|path)\s*[:=]|(?:sk-|ghp_|Bearer\s+)[A-Za-z0-9._-]{8,}"
)
MAX_BLOB_READ_BYTES = 8 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class CitationCandidate:
    asset_id: str
    resource_uri: str
    quote: str = ""
    locator: Mapping[str, str] = field(default_factory=dict)
    score: float | None = None

    def __post_init__(self) -> None:
        if not _OPAQUE_VALUE_RE.fullmatch(self.asset_id):
            raise ValueError("CitationCandidate.asset_id must be a short opaque identifier")
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("CitationCandidate.resource_uri must use knowledge://")
        if self.score is not None and not 0 <= self.score <= 1:
            raise ValueError("CitationCandidate.score must be between 0 and 1")
        if len(self.quote) > 1200:
            raise ValueError("CitationCandidate.quote exceeds 1200 characters")
        if any(ord(character) < 32 and character not in "\t\n\r" for character in self.quote):
            raise ValueError("CitationCandidate.quote contains control characters")
        if _SECRET_QUOTE_RE.search(self.quote):
            raise ValueError("CitationCandidate.quote appears to contain a secret")
        if any(
            key not in _CITATION_LOCATOR_KEYS
            or not isinstance(value, str)
            or not value.strip()
            or len(value) > 256
            or any(ord(character) < 32 for character in value)
            for key, value in self.locator.items()
        ):
            raise ValueError("CitationCandidate.locator contains unsupported or unsafe fields")
        if _SECRET_QUOTE_RE.search(" ".join(self.locator.values())):
            raise ValueError("CitationCandidate.locator appears to contain a secret")
        if any(
            "/" in value or "\\" in value or ".." in value or "://" in value or value.startswith("~")
            for value in self.locator.values()
        ):
            raise ValueError("CitationCandidate.locator cannot contain paths or URIs")
        object.__setattr__(self, "locator", freeze_mapping(self.locator))


@dataclass(frozen=True, slots=True)
class BlobReadRequest:
    resource_uri: str
    principal: Principal
    correlation: Correlation
    start: int = 0
    end: int | None = None
    expected_digest: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("BlobReadRequest.resource_uri must use knowledge://")
        if self.start < 0:
            raise ValueError("BlobReadRequest.start must be non-negative")
        if self.end is None:
            raise ValueError("BlobReadRequest.end is required for bounded reads")
        if self.end is not None and self.end <= self.start:
            raise ValueError("BlobReadRequest.end must be greater than start")
        if self.end is not None and self.end - self.start > MAX_BLOB_READ_BYTES:
            raise ValueError("BlobReadRequest range exceeds the maximum read size")
        if self.expected_digest is not None and not _DIGEST_RE.fullmatch(self.expected_digest):
            raise ValueError("BlobReadRequest.expected_digest must be a sha256 digest")


@dataclass(frozen=True, slots=True)
class BlobReadResult:
    resource_uri: str
    content: bytes
    content_digest: str
    start: int
    end: int
    asset_digest: str | None = None

    def __post_init__(self) -> None:
        if not is_valid_knowledge_uri(self.resource_uri):
            raise ValueError("BlobReadResult.resource_uri must use knowledge://")
        if not _DIGEST_RE.fullmatch(self.content_digest):
            raise ValueError("BlobReadResult.content_digest must be a sha256 digest")
        if self.asset_digest is not None and not _DIGEST_RE.fullmatch(self.asset_digest):
            raise ValueError("BlobReadResult.asset_digest must be a sha256 digest")
        if self.start < 0 or self.end < self.start or self.end - self.start != len(self.content):
            raise ValueError("BlobReadResult range does not match content length")
        if self.end - self.start > MAX_BLOB_READ_BYTES:
            raise ValueError("BlobReadResult exceeds the maximum read size")


def validate_blob_read_result(request: BlobReadRequest, result: BlobReadResult) -> None:
    """Fail closed when a provider returns a different resource or range."""

    if result.resource_uri != request.resource_uri:
        raise ValueError("BlobReadResult resource does not match request")
    if result.start != request.start or result.end > request.end:
        raise ValueError("BlobReadResult range does not fit request")
    if request.expected_digest is not None and request.expected_digest not in {
        result.content_digest,
        result.asset_digest,
    }:
        raise ValueError("BlobReadResult digest does not match request")
    actual_digest = f"sha256:{sha256(result.content).hexdigest()}"
    if result.content_digest != actual_digest:
        raise ValueError("BlobReadResult content digest is invalid")


@dataclass(frozen=True, slots=True)
class TraceDimension:
    key: str
    value: str

    def __post_init__(self) -> None:
        if self.key not in {
            "provider",
            "operation",
            "capability",
            "asset_id",
            "resource",
            "index",
            "revision",
            "job",
            "status",
            "kind",
        }:
            raise ValueError("TraceDimension.key is not allowlisted")
        if not _OPAQUE_VALUE_RE.fullmatch(self.value):
            raise ValueError("TraceDimension key/value must be short opaque values")
        if _SECRET_QUOTE_RE.search(self.value):
            raise ValueError("TraceDimension.value appears to contain a secret")


@dataclass(frozen=True, slots=True)
class TraceEvent:
    trace_id: str
    span_id: str
    name: str
    phase: str
    timestamp: str
    correlation: Correlation
    status: str = "running"
    input_digest: str | None = None
    output_digest: str | None = None
    dimensions: tuple[TraceDimension, ...] = ()

    def __post_init__(self) -> None:
        for field_name in ("trace_id", "span_id", "name", "phase", "timestamp"):
            if not getattr(self, field_name).strip():
                raise ValueError(f"TraceEvent.{field_name} must not be empty")
        if not _OPAQUE_VALUE_RE.fullmatch(self.trace_id) or not _OPAQUE_VALUE_RE.fullmatch(self.span_id):
            raise ValueError("TraceEvent IDs must be short opaque values")
        if not _OPAQUE_VALUE_RE.fullmatch(self.name) or not _OPAQUE_VALUE_RE.fullmatch(self.phase):
            raise ValueError("TraceEvent.name and phase must be short opaque values")
        try:
            parsed_timestamp = datetime.fromisoformat(self.timestamp.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("TraceEvent.timestamp must be an ISO-8601 timestamp") from exc
        if parsed_timestamp.tzinfo is None:
            raise ValueError("TraceEvent.timestamp must include a timezone")
        if self.status not in {"running", "ok", "error"}:
            raise ValueError("TraceEvent.status must be running, ok, or error")
        for digest_name in ("input_digest", "output_digest"):
            digest = getattr(self, digest_name)
            if digest is not None and not _DIGEST_RE.fullmatch(digest):
                raise ValueError(f"TraceEvent.{digest_name} must be a sha256 digest")
