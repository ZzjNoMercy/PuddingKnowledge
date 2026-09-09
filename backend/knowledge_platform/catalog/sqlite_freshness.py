"""Fail-closed local Catalog writer for Collection freshness observations."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_REVISION_RE = re.compile(r"^[A-Za-z0-9._:/-]{1,240}$")
_CAPABILITIES = frozenset({"database_nl2sql", "table_query", "wiki_query", "document_rag_query"})
_STATES = frozenset({"ready", "fresh", "available", "active", "stale", "unknown", "error", "blocked", "warming"})
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")


def _has_scope(principal: Principal, scope: str) -> bool:
    return scope in principal.scopes or scope.replace(".", ":") in principal.scopes


def _space_scope(principal: Principal, space_id: str) -> bool:
    return _has_scope(principal, "knowledge.admin") or _has_scope(principal, f"knowledge.space:{space_id}")


def _parse_observed_at(value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("freshness observed_at is required")
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("freshness observed_at must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError("freshness observed_at must include a timezone")
    normalized = timestamp.astimezone(UTC)
    if normalized.timestamp() > datetime.now(UTC).timestamp() + 300:
        raise ValueError("freshness observed_at is too far in the future")
    return normalized.isoformat()


def _timestamp(value: str, field: str) -> datetime:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"freshness {field} must be an ISO-8601 timestamp") from error
    if timestamp.tzinfo is None:
        raise ValueError(f"freshness {field} must include a timezone")
    return timestamp.astimezone(UTC)


def _revision(value: str | None, field: str) -> str | None:
    if value is None:
        return None
    if (
        not isinstance(value, str)
        or not _REVISION_RE.fullmatch(value)
        or value.startswith(("/", "~"))
        or ".." in value
        or _SECRET_RE.search(value)
    ):
        raise ValueError(f"freshness {field} is invalid")
    return value


@dataclass(frozen=True, slots=True)
class CollectionFreshnessObservation:
    """Portable observation emitted by a local provider or processing worker."""

    collection_id: str
    collection_version: str
    space_id: str
    capability: str
    state: str
    observed_at: str
    mode: str | None = None
    source_revision: str | None = None
    provider_revision: str | None = None

    def __post_init__(self) -> None:
        for field in ("collection_id", "collection_version", "space_id"):
            value = getattr(self, field)
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise ValueError(f"freshness {field} is invalid")
        if not isinstance(self.capability, str) or self.capability not in _CAPABILITIES:
            raise ValueError("freshness capability is invalid")
        if not isinstance(self.state, str) or self.state.casefold() not in _STATES:
            raise ValueError("freshness state is invalid")
        _parse_observed_at(self.observed_at)
        if self.mode is not None:
            _revision(self.mode, "mode")
        _revision(self.source_revision, "source_revision")
        _revision(self.provider_revision, "provider_revision")


class CollectionFreshnessWriter(Protocol):
    def observe(
        self,
        *,
        principal: Principal,
        observation: CollectionFreshnessObservation,
    ) -> dict[str, Any]: ...


class SqliteCollectionFreshnessWriter:
    """Persist one validated observation into an explicit Catalog database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    def observe(
        self,
        *,
        principal: Principal,
        observation: CollectionFreshnessObservation,
    ) -> dict[str, Any]:
        if principal.tenant_id is not None or not _has_scope(principal, "knowledge.processing") and not _has_scope(principal, "knowledge.admin"):
            raise PermissionError("Collection freshness requires Processing scope")
        if not _space_scope(principal, observation.space_id):
            raise PermissionError("Collection freshness Space scope is required")

        observed_at = _parse_observed_at(observation.observed_at)
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT capabilities, freshness FROM knowledge_datasets WHERE id = ? AND space_id = ? AND version = ?",
                (observation.collection_id, observation.space_id, observation.collection_version),
            ).fetchone()
            if row is None:
                raise ValueError("Collection is not found")
            try:
                capabilities = json.loads(row[0] or "[]")
                freshness = json.loads(row[1] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("Collection freshness metadata is invalid") from error
            if not isinstance(capabilities, list) or any(type(item) is not str for item in capabilities):
                raise ValueError("Collection capabilities are invalid")
            if observation.capability not in capabilities:
                raise ValueError("freshness capability is not declared by the Collection")
            if not isinstance(freshness, dict):
                raise ValueError("Collection freshness is invalid")

            previous_observed_at = freshness.get("observed_at")
            if previous_observed_at is not None:
                previous_timestamp = _timestamp(previous_observed_at, "observed_at")
                if previous_timestamp > _timestamp(observed_at, "observed_at"):
                    raise ValueError("freshness observation is older than the stored observation")

            updated = dict(freshness)
            updated.update(
                {
                    "state": observation.state.casefold(),
                    "observed_at": observed_at,
                    "capability": observation.capability,
                }
            )
            if observation.mode is not None:
                updated["mode"] = observation.mode
            for field in ("source_revision", "provider_revision"):
                value = getattr(observation, field)
                if value is not None:
                    updated[field] = value
            now = datetime.now(UTC).isoformat()
            connection.execute(
                "UPDATE knowledge_datasets SET freshness = ?, updated_at = ? WHERE id = ? AND space_id = ? AND version = ?",
                (
                    json.dumps(updated, ensure_ascii=False, sort_keys=True),
                    now,
                    observation.collection_id,
                    observation.space_id,
                    observation.collection_version,
                ),
            )
        return {
            "space_id": observation.space_id,
            "collection_id": observation.collection_id,
            "collection_version": observation.collection_version,
            "capability": observation.capability,
            "freshness": updated,
        }


class CollectionFreshnessObservationService:
    """Application boundary translating provider observations to QueryResult."""

    def __init__(self, *, writer: CollectionFreshnessWriter) -> None:
        self._writer = writer

    def observe(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        observation: CollectionFreshnessObservation,
    ) -> QueryResult:
        try:
            result = self._writer.observe(principal=principal, observation=observation)
        except PermissionError:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.PERMISSION_DENIED, message="Collection freshness observation is not authorized"),
            )
        except (TypeError, ValueError):
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.BINDING_UNAVAILABLE, message="Collection freshness observation was rejected"),
            )
        except Exception:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.CAPABILITY_UNAVAILABLE, message="Collection freshness writer is unavailable", retryable=True),
            )
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="Collection freshness observation accepted.",
            data={"collection_freshness": result},
        )
