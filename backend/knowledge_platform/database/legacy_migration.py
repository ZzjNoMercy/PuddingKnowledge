"""One-way bridge from legacy database receipts into Platform repositories."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol

from knowledge_contracts import Evidence, Principal

from .ports import (
    DatabaseEvidenceRecord,
    DatabaseEvidenceRepository,
    DatabaseSchemaEvidence,
    DatabaseSchemaEvidenceRepository,
)


class LegacyDatabaseEvidenceReader(Protocol):
    """Read-only legacy source; its receipt identity is intentionally not copied."""

    def list_database_evidence(self) -> Sequence[Mapping[str, object]]: ...

    def list_schema_evidence(self) -> Sequence[Mapping[str, object]]: ...


@dataclass(frozen=True, slots=True)
class LegacyEvidenceMigrationResult:
    evidence_written: int
    schema_evidence_written: int
    skipped_existing: int


def _authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.admin", "knowledge:admin"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _portable_evidence(
    raw: Mapping[str, object], *, space_id: str, dataset_id: str, source_revision: str
) -> Evidence:
    asset_id = str(raw.get("asset_id") or "legacy_evidence")
    resource_uri = str(
        raw.get("resource_uri")
        or f"knowledge://spaces/{space_id}/datasets/{dataset_id}/database-evidence/migrated/{asset_id}"
    )
    locator = raw.get("locator", {})
    matched_by = raw.get("matched_by", ())
    if not isinstance(locator, Mapping) or not isinstance(matched_by, (list, tuple)):
        raise ValueError("legacy evidence shape is invalid")
    return Evidence(
        asset_id=asset_id,
        resource_uri=resource_uri,
        locator=locator,
        quote=str(raw.get("quote") or "")[:1200],
        score=float(raw["score"]) if raw.get("score") is not None else None,
        revision=str(raw.get("revision") or source_revision),
        matched_by=tuple(str(item) for item in matched_by),
    )


def _record_id(*, space_id: str, dataset_id: str, source_revision: str, payload: object) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {"space_id": space_id, "dataset_id": dataset_id, "source_revision": source_revision, "payload": payload},
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    return f"migrated_{digest[:48]}"


class LegacyEvidenceMigrationService:
    """Copy facts, not workflow receipts, into the Platform repository."""

    def __init__(
        self,
        *,
        evidence: DatabaseEvidenceRepository,
        schema: DatabaseSchemaEvidenceRepository,
        ttl_seconds: int = 900,
    ) -> None:
        self._evidence = evidence
        self._schema = schema
        self._ttl_seconds = max(1, min(int(ttl_seconds), 86_400))

    def migrate(
        self,
        *,
        principal: Principal,
        reader: LegacyDatabaseEvidenceReader,
        space_id: str,
        dataset_id: str,
        source_revision: str,
        allowed_tables: Sequence[str],
    ) -> LegacyEvidenceMigrationResult:
        if not _authorized(principal, space_id):
            raise PermissionError("legacy evidence migration requires Admin Space scope")
        if not source_revision.startswith("sha256:") or not allowed_tables:
            raise ValueError("legacy evidence migration binding is invalid")
        evidence_written = 0
        schema_written = 0
        skipped = 0
        expires_at = (datetime.now(timezone.utc) + timedelta(seconds=self._ttl_seconds)).isoformat()
        for raw in reader.list_database_evidence():
            if not isinstance(raw, Mapping):
                raise ValueError("legacy database evidence record is invalid")
            raw_items = raw.get("evidence", raw.get("payload", raw.get("items", [])))
            if not isinstance(raw_items, (list, tuple)):
                raise ValueError("legacy database evidence items are invalid")
            if any(not isinstance(item, Mapping) for item in raw_items):
                raise ValueError("legacy database evidence contains an invalid item")
            items = tuple(
                _portable_evidence(item, space_id=space_id, dataset_id=dataset_id, source_revision=source_revision)
                for item in raw_items
                if isinstance(item, Mapping)
            )
            if not items:
                continue
            record = DatabaseEvidenceRecord(
                evidence_id=_record_id(
                    space_id=space_id,
                    dataset_id=dataset_id,
                    source_revision=source_revision,
                    payload=[item.asset_id for item in items],
                ),
                space_id=space_id,
                dataset_id=dataset_id,
                owner_subject_id=principal.subject_id,
                source_revision=source_revision,
                allowed_tables=tuple(str(table) for table in allowed_tables),
                evidence=items,
                expires_at=expires_at,
            )
            if self._evidence.get(
                evidence_id=record.evidence_id,
                owner_subject_id=principal.subject_id,
                space_id=space_id,
                dataset_id=dataset_id,
                source_revision=source_revision,
                allowed_tables=allowed_tables,
            ) is not None:
                skipped += 1
            else:
                self._evidence.put(record=record)
                evidence_written += 1
        for raw in reader.list_schema_evidence():
            if not isinstance(raw, Mapping):
                raise ValueError("legacy schema evidence record is invalid")
            table_name = str(raw.get("table_name") or "")
            columns = raw.get("columns", [])
            if not table_name or not isinstance(columns, (list, tuple)):
                raise ValueError("legacy schema evidence shape is invalid")
            schema_revision = str(raw.get("schema_revision") or source_revision)
            raw_evidence = raw.get("evidence", [])
            if not isinstance(raw_evidence, (list, tuple)) or any(
                not isinstance(item, Mapping) for item in raw_evidence
            ):
                raise ValueError("legacy schema evidence items are invalid")
            record = DatabaseSchemaEvidence(
                space_id=space_id,
                dataset_id=dataset_id,
                owner_subject_id=principal.subject_id,
                table_name=table_name,
                columns=tuple(str(column) for column in columns),
                schema_revision=schema_revision,
                evidence=tuple(
                    _portable_evidence(item, space_id=space_id, dataset_id=dataset_id, source_revision=schema_revision)
                    for item in raw_evidence
                ),
            )
            if self._schema.get(
                space_id=space_id,
                dataset_id=dataset_id,
                owner_subject_id=principal.subject_id,
                table_name=table_name,
                schema_revision=schema_revision,
            ) is not None:
                skipped += 1
            else:
                self._schema.put(record=record)
                schema_written += 1
        return LegacyEvidenceMigrationResult(evidence_written, schema_written, skipped)
