"""Executable, non-production Phase 0B rehearsal for the core Catalog slice.

The runner deliberately accepts explicit SQLAlchemy connections rather than
application settings. It reads the legacy ``knowledge_bases`` and
``knowledge_documents`` tables through reflected Core tables, maps them to
the independent Platform Catalog, and never imports the legacy ORM. A caller
must provide a file-reference checker when physical paths are present; the
target stores only stable URI/digest metadata, never host paths.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import Connection, MetaData, Table, and_, func, inspect, or_, select
from sqlalchemy.engine import Engine

from .metadata import KNOWLEDGE_METADATA
from .migrations import migrate_to_latest
from .models import KnowledgeAsset, KnowledgeDataset, KnowledgeSpace
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot

CORE_SOURCE_TABLES = ("knowledge_bases", "knowledge_documents")
CORE_TARGET_TABLES = ("knowledge_spaces", "knowledge_assets", "knowledge_datasets")
FAILURE_CHECKPOINTS = frozenset(
    {"after_schema", "after_spaces", "after_assets", "after_datasets", "before_verification"}
)
_SECRET_KEY = re.compile(
    r"(?:password|passphrase|token|secret|api[_-]?key|access[_-]?key|refresh|private[_-]?key|credential)",
    re.IGNORECASE,
)
_SECRET_VALUE = re.compile(
    r"(?:password|passphrase|token|secret|api[_-]?key|access[_-]?key|refresh|private[_-]?key|credential|bearer)",
    re.IGNORECASE,
)


class RehearsalInjectedFailure(RuntimeError):
    """Controlled failure used to prove that a target transaction rolls back."""


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _redact(value: Any, *, key: str | None = None) -> Any:
    if key is not None and _SECRET_KEY.search(key):
        return "<redacted>"
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {str(child_key): _redact(child_value, key=str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [_redact(child_value) for child_value in value]
    if isinstance(value, tuple):
        return [_redact(child_value) for child_value in value]
    return value


def _secrets_are_redacted(value: Any, *, key: str | None = None) -> bool:
    if key is not None and _SECRET_KEY.search(key):
        return value == "<redacted>"
    if isinstance(value, str) and _SECRET_VALUE.search(value):
        return value == "<redacted>"
    if isinstance(value, Mapping):
        return all(_secrets_are_redacted(child_value, key=str(child_key)) for child_key, child_value in value.items())
    if isinstance(value, (list, tuple)):
        return all(_secrets_are_redacted(child_value) for child_value in value)
    return True


def _json_safe(value: Any) -> Any:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, Mapping):
        return {str(key): _json_safe(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(child) for child in value]
    return value


def _engines_are_independent(source_engine: Engine, target_engine: Engine) -> bool:
    """Reject engine aliases that resolve to the same physical database."""

    if source_engine is target_engine:
        return False
    source_url = source_engine.url.render_as_string(hide_password=True)
    target_url = target_engine.url.render_as_string(hide_password=True)
    source_memory = source_engine.url.database in (None, ":memory:")
    target_memory = target_engine.url.database in (None, ":memory:")
    if source_url == target_url and not (source_memory and target_memory):
        return False
    if source_engine.url.drivername.startswith("sqlite") and target_engine.url.drivername.startswith("sqlite"):
        source_path = source_engine.url.database
        target_path = target_engine.url.database
        if source_path not in (None, ":memory:") and target_path not in (None, ":memory:"):
            return os.path.realpath(os.path.abspath(str(source_path))) != os.path.realpath(
                os.path.abspath(str(target_path))
            )
    return True


def _source_ref_digest(*references: str | None) -> str:
    return _digest([reference for reference in references if reference])


def _version_for_revision(source_revision: str) -> str:
    return "legacy-" + hashlib.sha256(source_revision.encode("utf-8")).hexdigest()[:16]


def _require_columns(table: Table, required: Sequence[str]) -> None:
    missing = sorted(set(required) - set(table.c.keys()))
    if missing:
        raise RehearsalVerificationError(f"{table.name}: required source columns missing: {missing}")


def _reflect_rows(connection: Connection, table_name: str, required: Sequence[str]) -> list[dict[str, Any]]:
    table = Table(table_name, MetaData(), autoload_with=connection)
    _require_columns(table, required)
    return [dict(row) for row in connection.execute(select(table)).mappings().all()]


def _canonical_space(row: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "id": f"space_{row['id']}",
        "name": str(row["name"]),
        "description": str(row.get("description") or ""),
        "permissions_json": {},
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_asset(row: Mapping[str, Any], *, source_revision: str, space_id: str) -> dict[str, Any]:
    document_id = str(row["id"])
    asset_id = f"asset_{document_id}_{hashlib.sha256(source_revision.encode('utf-8')).hexdigest()[:12]}"
    content_sha = str(row.get("content_sha256") or "")
    content_digest = (
        content_sha if content_sha.startswith("sha256:") else f"sha256:{content_sha}" if content_sha else _digest("")
    )
    metadata = _redact(row.get("doc_metadata") or {})
    metadata.update(
        {
            "legacy_document_id": document_id,
            "legacy_status": str(row.get("status") or ""),
            "legacy_virtual_path": str(row.get("virtual_path") or ""),
            "legacy_source_type": str(row.get("source_type") or ""),
            "source_reference_digest": _source_ref_digest(
                str(row.get("source_path") or ""), str(row.get("storage_path") or "")
            ),
            "origin_url_digest": _source_ref_digest(str(row.get("origin_url") or "")),
        }
    )
    return {
        "id": asset_id,
        "space_id": space_id,
        "kind": "document",
        "title": str(row["title"]),
        "description": "",
        "mime_type": str(row.get("mime_type") or "application/octet-stream"),
        "source_type": str(row.get("source_type") or "legacy"),
        "source_uri": f"knowledge://spaces/{space_id}/assets/{asset_id}",
        "revision": content_digest or f"source:{source_revision}",
        "content_digest": content_digest,
        "permissions_json": {},
        "metadata_json": _json_safe(metadata),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
    }


def _canonical_dataset(
    space: Mapping[str, Any], assets: Sequence[Mapping[str, Any]], *, source_revision: str
) -> dict[str, Any]:
    space_id = str(space["id"])
    dataset_id = f"dataset_{space_id.removeprefix('space_')}"
    version = _version_for_revision(source_revision)
    asset_ids = [str(asset["id"]) for asset in assets if str(asset["space_id"]) == space_id]
    manifest = {"asset_ids": asset_ids, "source_revision": source_revision, "version": version}
    return {
        "id": dataset_id,
        "space_id": space_id,
        "name": str(space["name"]),
        "version": version,
        "kind": "document-rag",
        "description": str(space.get("description") or ""),
        "asset_ids": asset_ids,
        "semantic_asset_ids": [],
        "capabilities": ["knowledge_list", "knowledge_search", "knowledge_read", "document_rag_query"],
        "freshness": {"mode": "snapshot", "source_revision": source_revision},
        "permissions_json": {},
        "manifest_digest": _digest(manifest),
        "created_at": space.get("created_at"),
        "updated_at": space.get("updated_at"),
    }


def _table_snapshot_rows(
    connection: Connection,
    table: Table,
    *,
    fields: Sequence[str],
    primary_key_fields: Sequence[str],
    expected_primary_keys: Sequence[tuple[Any, ...]] | None,
) -> list[dict[str, Any]]:
    if expected_primary_keys == ():
        return []
    statements = [select(table)]
    if expected_primary_keys is not None:
        # SQLite rejects a single OR expression with more than 1000 terms.
        # Large event/job slices are common in a real local Catalog, so split
        # the key lookup into bounded statements while preserving the same
        # exact-key semantics for composite primary keys.
        key_values = tuple(expected_primary_keys)
        chunk_size = 500 if len(primary_key_fields) > 1 else 900
        statements = []
        for offset in range(0, len(key_values), chunk_size):
            chunk = key_values[offset : offset + chunk_size]
            if len(primary_key_fields) == 1:
                statements.append(select(table).where(table.c[primary_key_fields[0]].in_([key[0] for key in chunk])))
                continue
            predicates = [
                and_(*(table.c[field] == value for field, value in zip(primary_key_fields, key, strict=True)))
                for key in chunk
            ]
            statements.append(select(table).where(or_(*predicates)))
    rows = [dict(row) for statement in statements for row in connection.execute(statement).mappings().all()]
    return [{field: _json_safe(row.get(field)) for field in fields} for row in rows]


def _out_of_scope_snapshot(
    connection: Connection,
    table: Table,
    *,
    fields: Sequence[str],
    primary_key_fields: Sequence[str],
    expected_primary_keys: Sequence[tuple[Any, ...]],
    lease_fields: Sequence[str] = (),
    file_reference_fields: Sequence[str] = (),
) -> Any:
    all_rows = _table_snapshot_rows(
        connection,
        table,
        fields=fields,
        primary_key_fields=primary_key_fields,
        expected_primary_keys=None,
    )
    expected = set(expected_primary_keys)
    extras = [row for row in all_rows if tuple(row.get(field) for field in primary_key_fields) not in expected]
    if not extras:
        return None
    return build_table_snapshot(
        f"{table.name}:out_of_scope",
        extras,
        primary_key_fields=primary_key_fields,
        lease_fields=lease_fields,
        file_reference_fields=file_reference_fields,
    )


def _upsert_immutable(
    connection: Connection, table: Table, row: Mapping[str, Any], primary_keys: Sequence[str]
) -> None:
    predicates = [table.c[key] == row[key] for key in primary_keys]
    existing = connection.execute(select(table).where(*predicates)).mappings().first()
    if existing is None:
        connection.execute(table.insert().values(**dict(row)))
        return
    existing_values = {key: _json_safe(value) for key, value in existing.items() if key in row}
    incoming_values = {key: _json_safe(value) for key, value in row.items()}
    if existing_values != incoming_values:
        raise RehearsalVerificationError(f"{table.name}: immutable row differs on retry for {primary_keys}")


def _target_database_state(engine: Engine) -> tuple[tuple[str, int, str, str], ...]:
    with engine.connect() as connection:
        inspector = inspect(connection)
        counts: list[tuple[str, int, str, str]] = []
        for table_name in sorted(inspector.get_table_names()):
            table = Table(table_name, MetaData(), autoload_with=connection)
            count = connection.execute(select(func.count()).select_from(table)).scalar_one()
            schema = {
                "columns": [
                    {
                        "name": column["name"],
                        "type": str(column["type"]),
                        "nullable": bool(column["nullable"]),
                    }
                    for column in inspector.get_columns(table_name)
                ],
                "primary_key": inspector.get_pk_constraint(table_name),
                "indexes": inspector.get_indexes(table_name),
                "unique_constraints": inspector.get_unique_constraints(table_name),
                "foreign_keys": inspector.get_foreign_keys(table_name),
            }
            rows = [dict(row) for row in connection.execute(select(table)).mappings().all()]
            normalized_rows = sorted(
                (_json_safe(row) for row in rows),
                key=lambda row: json.dumps(row, ensure_ascii=False, sort_keys=True, default=str),
            )
            counts.append((table_name, int(count), _digest(schema), _digest(normalized_rows)))
        return tuple(counts)


def _maybe_fail(requested_checkpoint: str | None, checkpoint: str) -> None:
    if requested_checkpoint == checkpoint:
        raise RehearsalInjectedFailure(f"injected rehearsal failure at {checkpoint}")


@dataclass(frozen=True, slots=True)
class CoreCatalogRehearsalResult:
    """Machine-readable evidence for the core Catalog rehearsal slice."""

    installation_id: str
    source_tables: tuple[str, ...]
    target_tables: tuple[str, ...]
    excluded_source_tables: tuple[str, ...]
    source_to_target: tuple[tuple[str, str, str], ...]
    report: RehearsalReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-catalog-rehearsal/v1",
            "installation_id": self.installation_id,
            "source_tables": list(self.source_tables),
            "target_tables": list(self.target_tables),
            "excluded_source_tables": list(self.excluded_source_tables),
            "source_to_target": [
                {"source_type": source_type, "source_id": source_id, "target_uri": target_uri}
                for source_type, source_id, target_uri in self.source_to_target
            ],
            "report": self.report.to_dict(),
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def run_core_catalog_rehearsal(
    source: Connection,
    target: Connection,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    file_reference_checker: Callable[[str], bool] | None = None,
    failure_checkpoint: str | None = None,
) -> CoreCatalogRehearsalResult:
    """Copy and verify the KB/document slice without changing active state.

    The caller owns the target transaction. A raised error therefore rolls the
    target back when called inside ``with engine.begin()``; the source is only
    queried. The operation is applied twice so retry idempotency is evidence,
    not a claim based on a primary-key inspection.
    """

    if not installation_id.strip():
        raise ValueError("installation_id must not be empty")
    if not source_revision.strip() or not target_revision.strip() or source_revision == target_revision:
        raise ValueError("source_revision and target_revision must be distinct and non-empty")
    if failure_checkpoint is not None and failure_checkpoint not in FAILURE_CHECKPOINTS:
        raise ValueError(f"unsupported failure_checkpoint: {failure_checkpoint}")
    source_url = source.engine.url.render_as_string(hide_password=True)
    target_url = target.engine.url.render_as_string(hide_password=True)
    same_in_memory_database = source_url == target_url and source.engine.url.database in (None, ":memory:")
    if source.engine is target.engine or (source_url == target_url and not same_in_memory_database):
        raise RehearsalVerificationError("source and target must use independent engines")
    if set(inspect(source).get_table_names()) < set(CORE_SOURCE_TABLES):
        missing = sorted(set(CORE_SOURCE_TABLES) - set(inspect(source).get_table_names()))
        raise RehearsalVerificationError(f"source Catalog is missing required tables: {missing}")

    base_rows = _reflect_rows(
        source,
        "knowledge_bases",
        ("id", "name", "description", "created_at", "updated_at"),
    )
    document_rows = _reflect_rows(
        source,
        "knowledge_documents",
        (
            "id",
            "knowledge_base_id",
            "title",
            "source_type",
            "source_path",
            "storage_path",
            "virtual_path",
            "mime_type",
            "content_sha256",
            "status",
            "doc_metadata",
            "origin_url",
            "created_at",
            "updated_at",
        ),
    )
    base_ids = {str(row["id"]) for row in base_rows}
    orphan_documents = sorted(str(row["id"]) for row in document_rows if str(row["knowledge_base_id"]) not in base_ids)
    if orphan_documents:
        raise RehearsalVerificationError(f"source foreign-key check failed for documents: {orphan_documents}")

    references = sorted(
        {
            str(row[field])
            for row in document_rows
            for field in ("source_path", "storage_path")
            if row.get(field) not in (None, "")
        }
    )
    if references and file_reference_checker is None:
        raise RehearsalVerificationError("file_reference_checker is required when source paths are present")
    file_reachable = not references or all(file_reference_checker(reference) for reference in references)
    if not file_reachable:
        raise RehearsalVerificationError("source file-reference reachability check failed")

    spaces = [_canonical_space(row) for row in base_rows]
    assets = [
        _canonical_asset(row, source_revision=source_revision, space_id=f"space_{row['knowledge_base_id']}")
        for row in document_rows
    ]
    datasets = [_canonical_dataset(space, assets, source_revision=source_revision) for space in spaces]

    target_space_table = KnowledgeSpace.__table__
    target_asset_table = KnowledgeAsset.__table__
    target_dataset_table = KnowledgeDataset.__table__
    migrate_to_latest(target)
    _maybe_fail(failure_checkpoint, "after_schema")
    target_tables = {table.name for table in KNOWLEDGE_METADATA.sorted_tables}
    if not set(CORE_TARGET_TABLES) <= target_tables:
        raise RehearsalVerificationError("Platform Catalog schema is missing the core target tables")

    for space in spaces:
        _upsert_immutable(target, target_space_table, space, ("id",))
    _maybe_fail(failure_checkpoint, "after_spaces")
    for asset in assets:
        _upsert_immutable(target, target_asset_table, asset, ("id",))
    _maybe_fail(failure_checkpoint, "after_assets")
    for dataset in datasets:
        _upsert_immutable(target, target_dataset_table, dataset, ("space_id", "id", "version"))
    _maybe_fail(failure_checkpoint, "after_datasets")

    # A second application is the replay/idempotency proof. It must not add or
    # mutate rows, and it must produce the same target snapshot.
    for space in spaces:
        _upsert_immutable(target, target_space_table, space, ("id",))
    for asset in assets:
        _upsert_immutable(target, target_asset_table, asset, ("id",))
    for dataset in datasets:
        _upsert_immutable(target, target_dataset_table, dataset, ("space_id", "id", "version"))
    _maybe_fail(failure_checkpoint, "before_verification")

    source_snapshot_rows = {
        name: [_json_safe(row) for row in rows]
        for name, rows in {"spaces": spaces, "assets": assets, "datasets": datasets}.items()
    }
    target_snapshot_rows = {
        "spaces": _table_snapshot_rows(
            target,
            target_space_table,
            fields=tuple(spaces[0]) if spaces else tuple(target_space_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((space["id"],) for space in spaces),
        ),
        "assets": _table_snapshot_rows(
            target,
            target_asset_table,
            fields=tuple(assets[0]) if assets else tuple(target_asset_table.c.keys()),
            primary_key_fields=("id",),
            expected_primary_keys=tuple((asset["id"],) for asset in assets),
        ),
        "datasets": _table_snapshot_rows(
            target,
            target_dataset_table,
            fields=tuple(datasets[0]) if datasets else tuple(target_dataset_table.c.keys()),
            primary_key_fields=("space_id", "id", "version"),
            expected_primary_keys=tuple(
                (dataset["space_id"], dataset["id"], dataset["version"]) for dataset in datasets
            ),
        ),
    }
    # Empty source tables still need a stable field set, while populated target
    # rows are restricted to the exact canonical fields used for comparison.
    if not spaces:
        target_snapshot_rows["spaces"] = []
    if not assets:
        target_snapshot_rows["assets"] = []
    if not datasets:
        target_snapshot_rows["datasets"] = []
    target_out_of_scope = tuple(
        snapshot
        for snapshot in (
            _out_of_scope_snapshot(
                target,
                target_space_table,
                fields=tuple(spaces[0]) if spaces else tuple(target_space_table.c.keys()),
                primary_key_fields=("id",),
                expected_primary_keys=tuple((space["id"],) for space in spaces),
            ),
            _out_of_scope_snapshot(
                target,
                target_asset_table,
                fields=tuple(assets[0]) if assets else tuple(target_asset_table.c.keys()),
                primary_key_fields=("id",),
                expected_primary_keys=tuple((asset["id"],) for asset in assets),
            ),
            _out_of_scope_snapshot(
                target,
                target_dataset_table,
                fields=tuple(datasets[0]) if datasets else tuple(target_dataset_table.c.keys()),
                primary_key_fields=("space_id", "id", "version"),
                expected_primary_keys=tuple(
                    (dataset["space_id"], dataset["id"], dataset["version"]) for dataset in datasets
                ),
            ),
        )
        if snapshot is not None
    )
    source_tables = tuple(
        build_table_snapshot(
            name,
            rows,
            primary_key_fields=("id",) if name != "datasets" else ("space_id", "id", "version"),
            file_reference_fields=("source_uri",) if name == "assets" else (),
        )
        for name, rows in source_snapshot_rows.items()
    )
    target_tables_snapshots = tuple(
        build_table_snapshot(
            name,
            rows,
            primary_key_fields=("id",) if name != "datasets" else ("space_id", "id", "version"),
            file_reference_fields=("source_uri",) if name == "assets" else (),
        )
        for name, rows in target_snapshot_rows.items()
    )
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=source_tables,
        target_tables=target_tables_snapshots,
        target_out_of_scope_tables=target_out_of_scope,
        retry_idempotent=True,
        checks={
            "secret_redaction": all(_secrets_are_redacted(asset["metadata_json"]) for asset in assets),
            "file_reachability": file_reachable,
            "lease_state": True,
            "foreign_keys": True,
        },
        check_scopes={
            "secret_redaction": "legacy document metadata is recursively redacted before target insert",
            "file_reachability": "source_path and storage_path references in the core slice",
            "lease_state": "not_applicable: lease-bearing job tables are excluded from this core slice",
            "foreign_keys": "legacy KB/document relationship checked; target uses application-level stable IDs",
        },
    )
    report.verify_safe()
    mappings = tuple(
        [("knowledge_base", str(row["id"]), f"knowledge://spaces/space_{row['id']}") for row in base_rows]
        + [
            ("knowledge_document", str(row["id"]), asset["source_uri"])
            for row, asset in zip(document_rows, assets, strict=True)
        ]
    )
    return CoreCatalogRehearsalResult(
        installation_id=installation_id,
        source_tables=CORE_SOURCE_TABLES,
        target_tables=CORE_TARGET_TABLES,
        excluded_source_tables=("knowledge_source_connections", "knowledge_source_items", "knowledge_sync_runs"),
        source_to_target=mappings,
        report=report,
    )


def run_core_catalog_rehearsal_with_rollback_probes(
    source: Connection,
    target_engine: Engine,
    *,
    installation_id: str,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    file_reference_checker: Callable[[str], bool] | None = None,
    failure_checkpoints: Sequence[str] = (
        "after_schema",
        "after_spaces",
        "after_assets",
        "after_datasets",
        "before_verification",
    ),
) -> CoreCatalogRehearsalResult:
    """Run failure probes, prove rollback, then execute the successful copy.

    This helper owns short target transactions for the probes. It refuses to
    call a probe successfully: each requested checkpoint must raise the
    controlled failure, and all core target row counts must return to their
    pre-probe baseline before the next probe starts.
    """

    checkpoints = tuple(failure_checkpoints)
    unknown_checkpoints = sorted(set(checkpoints) - FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise ValueError("failure_checkpoints must not be empty")
    if unknown_checkpoints:
        raise ValueError(f"unsupported failure_checkpoints: {unknown_checkpoints}")
    # Prepare schema outside the data-copy probes. SQLite DDL is not
    # consistently transactional across versions, so a probe must not confuse
    # first-time schema creation with a data-copy rollback guarantee.
    with target_engine.begin() as target:
        migrate_to_latest(target)
    baseline = _target_database_state(target_engine)
    for checkpoint in checkpoints:
        try:
            with target_engine.begin() as target:
                run_core_catalog_rehearsal(
                    source,
                    target,
                    installation_id=installation_id,
                    source_revision=source_revision,
                    target_revision=f"{target_revision}-probe-{checkpoint}",
                    active_revision=active_revision,
                    file_reference_checker=file_reference_checker,
                    failure_checkpoint=checkpoint,
                )
        except RehearsalInjectedFailure:
            pass
        else:
            raise RehearsalVerificationError(f"failure probe did not fail at {checkpoint}")
        after_failure = _target_database_state(target_engine)
        if after_failure != baseline:
            raise RehearsalVerificationError(
                f"target database state changed after rollback probe {checkpoint}: "
                f"baseline={baseline}, after={after_failure}"
            )

    with target_engine.begin() as target:
        result = run_core_catalog_rehearsal(
            source,
            target,
            installation_id=installation_id,
            source_revision=source_revision,
            target_revision=target_revision,
            active_revision=active_revision,
            file_reference_checker=file_reference_checker,
        )
    return replace(result, report=replace(result.report, injected_failure_checkpoints=checkpoints))
