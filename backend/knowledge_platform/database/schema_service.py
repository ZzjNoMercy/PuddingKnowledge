"""Permissioned, read-only Database Schema query service."""

from __future__ import annotations

import re

from knowledge_contracts import Correlation, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .ports import DatabaseDatasetResolver, DatabaseSchemaReader

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, *, space_id: str) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None:
        return False
    return bool({"knowledge.database_schema", "knowledge:database_schema"} & scopes) and bool(
        {f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes
    )


class DatabaseSchemaQueryService:
    """Expose only allowlisted schema facts, never connection or filesystem facts."""

    def __init__(self, *, datasets: DatabaseDatasetResolver, reader: DatabaseSchemaReader) -> None:
        self._datasets = datasets
        self._reader = reader

    def list(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        space_id: str,
        dataset_id: str,
    ) -> QueryResult:
        if not isinstance(space_id, str) or not isinstance(dataset_id, str) or not _ID_RE.fullmatch(space_id) or not _ID_RE.fullmatch(dataset_id):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id or dataset_id is invalid")
        if not _authorized(principal, space_id=space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database schema is not authorized")
        try:
            binding = self._datasets.resolve(dataset_id=dataset_id, space_id=space_id)
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database schema binding is unavailable")
        if binding is None or binding.space_id != space_id or binding.dataset_id != dataset_id:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database schema binding is unavailable")
        try:
            tables = tuple(self._reader.read(binding=binding))
            if not tables or len(tables) > 500:
                raise LookupError("database schema is unavailable")
            allowed = {item.casefold() for item in binding.allowed_tables}
            allowed.update(
                f"public.{item}".casefold()
                for item in binding.allowed_tables
                if "." not in item
            )
            if any(table.table_name.casefold() not in allowed for table in tables):
                raise LookupError("database schema exceeded the table binding")
            if any(_SECRET_RE.search(column) for table in tables for column in table.columns):
                raise PermissionError("database schema contains a secret-bearing column")
        except (LookupError, PermissionError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database schema is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "database schema read failed")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={
                "space_id": space_id,
                "dataset_id": dataset_id,
                "source_revision": binding.source_revision,
                "tables": [
                    {"table_name": item.table_name, "columns": list(item.columns), "schema_revision": item.schema_revision}
                    for item in tables
                ],
            },
            provenance=Provenance(
                space_id=space_id,
                dataset_id=dataset_id,
                dataset_version=binding.dataset_version,
                capability="database_schema",
                provider_versions={"schema": binding.provider_version or "platform"},
            ),
        )


__all__ = ["DatabaseSchemaQueryService"]
