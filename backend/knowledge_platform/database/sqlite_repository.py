"""Durable SQLite repositories for the Platform Database Query Plane."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from knowledge_contracts import Evidence, QueryPlan, QueryPlanValidation

from .ports import (
    DatabaseEvidenceRecord,
    DatabaseExecution,
    DatabaseSchemaEvidence,
    StoredQueryPlan,
)


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _evidence_from_json(value: object) -> tuple[Evidence, ...]:
    if not isinstance(value, list):
        raise ValueError("stored evidence is malformed")
    if any(not isinstance(item, Mapping) for item in value):
        raise ValueError("stored evidence contains an invalid item")
    return tuple(
        Evidence(
            asset_id=str(item["asset_id"]),
            resource_uri=str(item["resource_uri"]),
            locator=item.get("locator", {}),
            quote=str(item.get("quote", "")),
            score=item.get("score"),
            revision=str(item.get("revision", "")),
            matched_by=tuple(str(entry) for entry in item.get("matched_by", [])),
        )
        for item in value
    )


def _evidence_to_dict(value: Evidence) -> dict[str, object]:
    return {
        "asset_id": value.asset_id,
        "resource_uri": value.resource_uri,
        "locator": dict(value.locator),
        "quote": value.quote,
        "score": value.score,
        "revision": value.revision,
        "matched_by": list(value.matched_by),
    }


class SqliteDatabaseQueryRepository:
    """One explicit database owns plans/results/evidence; no legacy receipt IDs."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path.expanduser().absolute()
        if self.database_path.is_symlink() or not self.database_path.is_file():
            raise FileNotFoundError(f"Platform database does not exist: {self.database_path}")
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS knowledge_query_plans (
                    query_plan_id TEXT PRIMARY KEY,
                    dataset_id TEXT NOT NULL,
                    owner_subject_id TEXT NOT NULL,
                    owner_scope_digest TEXT NOT NULL,
                    plan_json TEXT NOT NULL,
                    allowed_tables_json TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    semantic_asset_ids_json TEXT NOT NULL DEFAULT '[]',
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_database_evidence (
                    evidence_id TEXT PRIMARY KEY,
                    space_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    owner_subject_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    allowed_tables_json TEXT NOT NULL,
                    evidence_json TEXT NOT NULL,
                    expires_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS knowledge_database_schema_evidence (
                    space_id TEXT NOT NULL,
                    dataset_id TEXT NOT NULL,
                    owner_subject_id TEXT NOT NULL,
                    table_name TEXT NOT NULL,
                    schema_revision TEXT NOT NULL,
                    record_json TEXT NOT NULL,
                    PRIMARY KEY (space_id, dataset_id, owner_subject_id, table_name, schema_revision)
                );
                CREATE TABLE IF NOT EXISTS knowledge_query_results (
                    result_id TEXT PRIMARY KEY,
                    space_id TEXT NOT NULL,
                    owner_subject_id TEXT NOT NULL,
                    result_json TEXT NOT NULL
                );
                """
            )
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_query_plans)")}
            if "semantic_asset_ids_json" not in columns:
                connection.execute("ALTER TABLE knowledge_query_plans ADD COLUMN semantic_asset_ids_json TEXT NOT NULL DEFAULT '[]'")

    def put(self, *, stored: StoredQueryPlan) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    """
                    INSERT INTO knowledge_query_plans (
                        query_plan_id, dataset_id, owner_subject_id, owner_scope_digest,
                        plan_json, allowed_tables_json, source_revision, semantic_asset_ids_json, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        stored.plan.query_plan_id,
                        stored.plan.dataset_id,
                        stored.owner_subject_id,
                        stored.owner_scope_digest,
                        _json(stored.plan.to_dict()),
                        _json(list(stored.allowed_tables)),
                        stored.source_revision,
                        _json(list(stored.semantic_asset_ids)),
                        stored.plan.expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("query plan id already exists") from error

    def get(self, *, query_plan_id: str) -> StoredQueryPlan | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_subject_id, owner_scope_digest, plan_json, allowed_tables_json, source_revision, semantic_asset_ids_json "
                "FROM knowledge_query_plans WHERE query_plan_id = ?",
                (query_plan_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            plan_data = json.loads(row[2])
            validation_data = plan_data["validation"]
            plan = QueryPlan(
                query_plan_id=str(plan_data["query_plan_id"]),
                sql=str(plan_data["sql"]),
                dialect=str(plan_data["dialect"]),
                dataset_id=str(plan_data["dataset_id"]),
                dataset_version=str(plan_data["dataset_version"]),
                deployment_revision=str(plan_data["deployment_revision"]),
                semantic_context_hash=str(plan_data["semantic_context_hash"]),
                validation=QueryPlanValidation(
                    readonly=validation_data["readonly"],
                    allowed_tables=validation_data["allowed_tables"],
                    guardrails_passed=validation_data["guardrails_passed"],
                ),
                expires_at=str(plan_data["expires_at"]),
                evidence=_evidence_from_json(plan_data.get("evidence", [])),
            )
            allowed_tables = tuple(str(item) for item in json.loads(row[3]))
            return StoredQueryPlan(
                plan=plan,
                owner_subject_id=str(row[0]),
                owner_scope_digest=str(row[1]),
                allowed_tables=allowed_tables,
                source_revision=str(row[4]),
                semantic_asset_ids=tuple(str(item) for item in json.loads(row[5] or "[]")),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def put_evidence(self, *, record: DatabaseEvidenceRecord) -> None:
        evidence_json = _json([_evidence_to_dict(item) for item in record.evidence])
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_database_evidence (evidence_id, space_id, dataset_id, owner_subject_id, "
                    "source_revision, allowed_tables_json, evidence_json, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        record.evidence_id,
                        record.space_id,
                        record.dataset_id,
                        record.owner_subject_id,
                        record.source_revision,
                        _json(list(record.allowed_tables)),
                        evidence_json,
                        record.expires_at,
                    ),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("database evidence id already exists") from error

    def get_evidence(
        self,
        *,
        evidence_id: str,
        owner_subject_id: str,
        space_id: str,
        dataset_id: str,
        source_revision: str,
        allowed_tables: Sequence[str],
    ) -> DatabaseEvidenceRecord | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT owner_subject_id, space_id, dataset_id, source_revision, allowed_tables_json, evidence_json, expires_at "
                "FROM knowledge_database_evidence WHERE evidence_id = ?",
                (evidence_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            table_scope = tuple(str(item) for item in json.loads(row[4]))
            if any(
                (
                    str(row[0]) != owner_subject_id,
                    str(row[1]) != space_id,
                    str(row[2]) != dataset_id,
                    str(row[3]) != source_revision,
                    not set(allowed_tables).issubset(set(table_scope)),
                )
            ):
                return None
            record = DatabaseEvidenceRecord(
                evidence_id=evidence_id,
                space_id=space_id,
                dataset_id=dataset_id,
                owner_subject_id=owner_subject_id,
                source_revision=source_revision,
                allowed_tables=table_scope,
                evidence=_evidence_from_json(json.loads(row[5])),
                expires_at=str(row[6]),
            )
            if datetime.fromisoformat(record.expires_at) <= datetime.now(timezone.utc):
                return None
            return record
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def put_schema_evidence(self, *, record: DatabaseSchemaEvidence) -> None:
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_database_schema_evidence (space_id, dataset_id, owner_subject_id, table_name, schema_revision, record_json) VALUES (?, ?, ?, ?, ?, ?)",
                    (record.space_id, record.dataset_id, record.owner_subject_id, record.table_name.casefold(), record.schema_revision, _json({
                        "space_id": record.space_id,
                        "dataset_id": record.dataset_id,
                        "owner_subject_id": record.owner_subject_id,
                        "table_name": record.table_name,
                        "columns": list(record.columns),
                        "schema_revision": record.schema_revision,
                        "evidence": [_evidence_to_dict(item) for item in record.evidence],
                    })),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("database schema evidence already exists") from error

    def get_schema_evidence(
        self,
        *,
        space_id: str,
        dataset_id: str,
        owner_subject_id: str,
        table_name: str,
        schema_revision: str,
    ) -> DatabaseSchemaEvidence | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT record_json FROM knowledge_database_schema_evidence WHERE space_id = ? AND dataset_id = ? AND owner_subject_id = ? AND lower(table_name) = lower(?) AND schema_revision = ?",
                (space_id, dataset_id, owner_subject_id, table_name, schema_revision),
            ).fetchone()
        if row is None:
            return None
        try:
            value = json.loads(row[0])
            return DatabaseSchemaEvidence(
                space_id=str(value["space_id"]),
                dataset_id=str(value["dataset_id"]),
                owner_subject_id=str(value["owner_subject_id"]),
                table_name=str(value["table_name"]),
                columns=tuple(str(item) for item in value["columns"]),
                schema_revision=str(value["schema_revision"]),
                evidence=_evidence_from_json(value.get("evidence", [])),
            )
        except (TypeError, ValueError, KeyError, json.JSONDecodeError):
            return None

    def put_result(self, *, result: DatabaseExecution, space_id: str, owner_subject_id: str) -> str:
        result_id = result.result_id or f"result_{uuid4().hex}"
        payload = {
            "columns": list(result.columns),
            "rows": [dict(row) for row in result.rows],
            "row_count": result.row_count,
            "limited": result.limited,
            "result_id": result_id,
        }
        with self._connect() as connection:
            try:
                connection.execute(
                    "INSERT INTO knowledge_query_results (result_id, space_id, owner_subject_id, result_json) VALUES (?, ?, ?, ?)",
                    (result_id, space_id, owner_subject_id, _json(payload)),
                )
            except sqlite3.IntegrityError as error:
                raise ValueError("query result id already exists") from error
        return result_id


class SqliteDatabaseEvidenceRepository:
    """Protocol-shaped facade for Platform-owned database evidence."""

    def __init__(self, database_path: Path) -> None:
        self._repository = SqliteDatabaseQueryRepository(database_path)

    def put(self, *, record: DatabaseEvidenceRecord) -> None:
        self._repository.put_evidence(record=record)

    def get(
        self,
        *,
        evidence_id: str,
        owner_subject_id: str,
        space_id: str,
        dataset_id: str,
        source_revision: str,
        allowed_tables: Sequence[str],
    ) -> DatabaseEvidenceRecord | None:
        return self._repository.get_evidence(
            evidence_id=evidence_id,
            owner_subject_id=owner_subject_id,
            space_id=space_id,
            dataset_id=dataset_id,
            source_revision=source_revision,
            allowed_tables=allowed_tables,
        )


class SqliteDatabaseSchemaEvidenceRepository:
    """Protocol-shaped facade for Platform-owned schema evidence."""

    def __init__(self, database_path: Path) -> None:
        self._repository = SqliteDatabaseQueryRepository(database_path)

    def put(self, *, record: DatabaseSchemaEvidence) -> None:
        self._repository.put_schema_evidence(record=record)

    def get(
        self,
        *,
        space_id: str,
        dataset_id: str,
        owner_subject_id: str,
        table_name: str,
        schema_revision: str,
    ) -> DatabaseSchemaEvidence | None:
        return self._repository.get_schema_evidence(
            space_id=space_id,
            dataset_id=dataset_id,
            owner_subject_id=owner_subject_id,
            table_name=table_name,
            schema_revision=schema_revision,
        )
