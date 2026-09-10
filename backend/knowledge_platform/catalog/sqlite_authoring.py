"""Transactional SQLite writer for Admin-plane logical Structured Assets."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from knowledge_contracts import Principal

if TYPE_CHECKING:
    from knowledge_platform.structured.ports import StructuredSourceProfile

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_COLUMN_RE = re.compile(r"^[^\x00-\x1f\x7f]{1,200}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_CAPABILITIES = {"database_nl2sql", "table_query", "wiki_query", "document_rag_query"}


def _unsafe(value: object) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_RE.search(value) or _PATH_RE.search(value))
    if isinstance(value, Mapping):
        return any(_unsafe(key) or _unsafe(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_unsafe(item) for item in value)
    return False


def _definition_digest(logical: Mapping[str, object]) -> str:
    encoded = json.dumps(
        {"logical_dataset": dict(logical)}, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _logical_content_digest(source_snapshot: Sequence[Mapping[str, object]]) -> str:
    digest = hashlib.sha256()
    for item in source_snapshot:
        digest.update(str(item["asset_id"]).encode("utf-8"))
        digest.update(b"\0")
        digest.update(str(item["content_digest"]).encode("ascii"))
        digest.update(b"\0")
    return f"sha256:{digest.hexdigest()}"


class SqliteStructuredAssetWriter:
    """Write only staged logical Structured Assets into an explicit target DB."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    def create_logical_dataset(self, *, record: Mapping[str, object]) -> dict[str, object]:
        required = ("id", "space_id", "title", "source_uri", "content_digest", "logical_dataset")
        if any(field not in record for field in required):
            raise ValueError("logical dataset record is incomplete")
        dataset_id = str(record["id"])
        space_id = str(record["space_id"])
        if not _ID_RE.fullmatch(dataset_id) or not _ID_RE.fullmatch(space_id):
            raise ValueError("logical dataset identity is invalid")
        logical = record["logical_dataset"]
        title = str(record["title"]).strip()
        if (
            not isinstance(logical, dict)
            or _unsafe(logical)
            or _unsafe(title)
            or not title
            or len(title) > 500
        ):
            raise ValueError("logical dataset metadata is unsafe")
        content_digest = str(record["content_digest"])
        if not _DIGEST_RE.fullmatch(content_digest):
            raise ValueError("logical dataset content digest is invalid")
        source_ids = logical.get("source_asset_ids")
        columns = logical.get("canonical_columns")
        if (
            not isinstance(source_ids, list)
            or not source_ids
            or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in source_ids)
            or len(set(source_ids)) != len(source_ids)
            or not isinstance(columns, list)
            or not columns
            or len(set(columns)) != len(columns)
            or any(not isinstance(item, str) or not _COLUMN_RE.fullmatch(item) or _SECRET_RE.search(item) for item in columns)
            or _definition_digest(logical) != content_digest
        ):
            raise ValueError("logical dataset definition is invalid")
        source_snapshot = record.get("source_snapshot")
        if (
            not isinstance(source_snapshot, (list, tuple))
            or [item.get("asset_id") if isinstance(item, Mapping) else None for item in source_snapshot] != source_ids
            or any(
                not isinstance(item, Mapping)
                or set(item) != {"asset_id", "content_digest"}
                or not _ID_RE.fullmatch(str(item["asset_id"]))
                or not _DIGEST_RE.fullmatch(str(item["content_digest"]))
                or _unsafe(item)
                for item in source_snapshot
            )
        ):
            raise ValueError("logical dataset source snapshot is invalid")
        encoded_metadata = json.dumps({"logical_dataset": logical}, ensure_ascii=False, sort_keys=True)
        now = datetime.now(timezone.utc).isoformat()
        uri = f"knowledge://spaces/{space_id}/structured-assets/{dataset_id}/source"
        if str(record["source_uri"]) != uri:
            raise ValueError("logical dataset source URI is not canonical")
        result = {
            "id": dataset_id,
            "space_id": space_id,
            "kind": "structured_asset",
            "source_type": "logical_concat",
            "title": str(record["title"]).strip(),
            "source_uri": uri,
            "content_digest": content_digest,
            "reference_status": "pending",
            "capabilities": ["table_query"],
            "logical_dataset": logical,
        }
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            for item in source_snapshot:
                source_row = connection.execute(
                    "SELECT space_id, content_digest, reference_status, capabilities "
                    "FROM knowledge_structured_assets WHERE id = ?",
                    (str(item["asset_id"]),),
                ).fetchone()
                if source_row is None or source_row[0] != space_id or source_row[1] != item["content_digest"]:
                    raise ValueError("logical dataset source changed during authoring")
                if source_row[2] not in {"ready", "verified", "active"}:
                    raise ValueError("logical dataset source is not approved")
                try:
                    capabilities = json.loads(source_row[3] or "[]")
                except json.JSONDecodeError as error:
                    raise ValueError("logical dataset source capabilities are invalid") from error
                if not isinstance(capabilities, list) or "table_query" not in {str(item) for item in capabilities}:
                    raise ValueError("logical dataset source lacks table_query capability")
            connection.execute(
                """
                INSERT INTO knowledge_structured_assets (
                    id, space_id, source_key, document_asset_id, source_type, file_name, sheet_name,
                    size_bytes, modified_at, source_uri, source_reference_digest, logical_path_digest,
                    profile_uri, profile_reference_digest, content_digest, profile_status, row_count,
                    column_count, columns_json, reference_status, capabilities, metadata_json, created_at, updated_at
                ) VALUES (?, ?, ?, NULL, ?, ?, NULL, 0, NULL, ?, '', '', ?, '', ?, 'missing', NULL, ?, ?, 'pending', ?, ?, ?, ?)
                """,
                (
                    dataset_id,
                    space_id,
                    f"logical:{dataset_id}",
                    "logical_concat",
                    title,
                    uri,
                    f"knowledge://spaces/{space_id}/structured-assets/{dataset_id}/profile",
                    content_digest,
                    len(columns),
                    json.dumps(columns, ensure_ascii=False),
                    json.dumps(["table_query"]),
                    encoded_metadata,
                    now,
                    now,
                ),
            )
        return result

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
    ) -> dict[str, object]:
        """CAS-publish a pending logical row after Processing validated source bytes."""

        if not _ID_RE.fullmatch(dataset_id):
            raise ValueError("logical dataset identity is invalid")
        if not _DIGEST_RE.fullmatch(expected_definition_digest) or not _DIGEST_RE.fullmatch(content_digest):
            raise ValueError("logical dataset digest is invalid")
        if type(row_count) is not int or not 0 <= row_count <= 100_000:
            raise ValueError("logical dataset row count is invalid")
        normalized_columns = [str(column) for column in columns]
        if not normalized_columns or len(set(normalized_columns)) != len(normalized_columns) or any(
            not column or len(column) > 200 or _SECRET_RE.search(column) for column in normalized_columns
        ):
            raise ValueError("logical dataset columns are invalid")
        normalized_snapshot: list[dict[str, object]] = []
        for item in source_snapshot:
            if not isinstance(item, Mapping) or set(item) != {"asset_id", "content_digest", "row_count"}:
                raise ValueError("logical dataset source snapshot is invalid")
            asset_id = str(item["asset_id"])
            item_digest = str(item["content_digest"])
            item_rows = item["row_count"]
            if (
                not _ID_RE.fullmatch(asset_id)
                or not _DIGEST_RE.fullmatch(item_digest)
                or type(item_rows) is not int
                or item_rows < 0
                or _unsafe(item)
            ):
                raise ValueError("logical dataset source snapshot is unsafe")
            normalized_snapshot.append(
                {"asset_id": asset_id, "content_digest": item_digest, "row_count": item_rows}
            )
        if not normalized_snapshot:
            raise ValueError("logical dataset source snapshot is empty")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT space_id, source_uri, file_name, content_digest, reference_status, metadata_json "
                "FROM knowledge_structured_assets WHERE id = ?",
                (dataset_id,),
            ).fetchone()
            if row is None:
                raise ValueError("logical dataset does not exist")
            if row["reference_status"] != "pending" or row["content_digest"] != expected_definition_digest:
                raise ValueError("logical dataset is no longer pending at expected definition")
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError as error:
                raise ValueError("logical dataset metadata is invalid") from error
            logical = metadata.get("logical_dataset") if isinstance(metadata, dict) else None
            if not isinstance(logical, dict) or _unsafe(logical):
                raise ValueError("logical dataset metadata is invalid")
            source_ids = logical.get("source_asset_ids")
            if (
                not isinstance(source_ids, list)
                or not source_ids
                or any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in source_ids)
                or len(set(source_ids)) != len(source_ids)
                or [item["asset_id"] for item in normalized_snapshot] != source_ids
                or _definition_digest(logical) != expected_definition_digest
                or _logical_content_digest(normalized_snapshot) != content_digest
                or logical.get("canonical_columns") != normalized_columns
                or sum(int(item["row_count"]) for item in normalized_snapshot) != row_count
            ):
                raise ValueError("logical dataset lineage or digest is invalid")
            space_id = str(row["space_id"])
            scopes = set(principal.scopes)
            if (
                principal.tenant_id is not None
                or not {"knowledge.processing", "knowledge:processing", "knowledge.admin", "knowledge:admin"} & scopes
                or not {f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes
            ):
                raise PermissionError("logical dataset publish requires Processing scope")
            for item in normalized_snapshot:
                source_row = connection.execute(
                    "SELECT space_id, content_digest, reference_status, capabilities "
                    "FROM knowledge_structured_assets WHERE id = ?",
                    (item["asset_id"],),
                ).fetchone()
                if source_row is None or source_row[0] != space_id or source_row[1] != item["content_digest"]:
                    raise ValueError("logical dataset source changed before publish")
                if source_row[2] not in {"ready", "verified", "active"}:
                    raise ValueError("logical dataset source is no longer approved")
                try:
                    capabilities = json.loads(source_row[3] or "[]")
                except json.JSONDecodeError as error:
                    raise ValueError("logical dataset source capabilities are invalid") from error
                if not isinstance(capabilities, list) or "table_query" not in {str(item) for item in capabilities}:
                    raise ValueError("logical dataset source lacks table_query capability")
            metadata = {
                "logical_dataset": logical,
                "definition_digest": expected_definition_digest,
                "source_snapshot": normalized_snapshot,
            }
            cursor = connection.execute(
                """
                UPDATE knowledge_structured_assets
                SET content_digest = ?, profile_reference_digest = ?, source_reference_digest = ?,
                    profile_status = 'ready', row_count = ?, column_count = ?, columns_json = ?,
                    reference_status = 'ready', metadata_json = ?, updated_at = ?
                WHERE id = ? AND reference_status = 'pending' AND content_digest = ?
                """,
                (
                    content_digest,
                    content_digest,
                    content_digest,
                    row_count,
                    len(normalized_columns),
                    json.dumps(normalized_columns, ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    dataset_id,
                    expected_definition_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("logical dataset publish CAS failed")
        return {
            "id": dataset_id,
            "space_id": str(row["space_id"]),
            "kind": "structured_asset",
            "source_type": "logical_concat",
            "title": str(row["file_name"] or ""),
            "source_uri": str(row["source_uri"]),
            "content_digest": content_digest,
            "reference_status": "ready",
            "capabilities": ["table_query"],
            "logical_dataset": logical,
            "row_count": row_count,
            "column_count": len(normalized_columns),
            "columns": normalized_columns,
        }

    def bind_source_asset(
        self,
        *,
        principal: Principal,
        asset_id: str,
        space_id: str,
        expected_content_digest: str,
        profile: StructuredSourceProfile,
    ) -> dict[str, object]:
        """CAS-approve a direct source Asset; no physical path is persisted."""

        scopes = set(principal.scopes)
        if principal.tenant_id is not None or not {
            "knowledge.processing",
            "knowledge:processing",
            "knowledge.admin",
            "knowledge:admin",
        } & scopes or not {
            f"knowledge.space:{space_id}",
            f"knowledge:space:{space_id}",
        } & scopes:
            raise PermissionError("source binding requires Processing scope")
        if not _ID_RE.fullmatch(asset_id) or not _ID_RE.fullmatch(space_id) or not _DIGEST_RE.fullmatch(expected_content_digest):
            raise ValueError("source binding identity is invalid")
        if profile.content_digest != expected_content_digest:
            raise ValueError("source binding digest is invalid")
        if _unsafe(profile.columns):
            raise ValueError("source binding columns are unsafe")
        now = datetime.now(timezone.utc).isoformat()
        metadata = {
            "source_binding": {
                "content_digest": expected_content_digest,
                "row_count": profile.row_count,
                "columns": list(profile.columns),
            }
        }
        with sqlite3.connect(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            row = connection.execute(
                "SELECT space_id, source_uri, file_name, content_digest, reference_status, source_type FROM knowledge_structured_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            if (
                row is None
                or row["space_id"] != space_id
                or row["reference_status"] != "pending"
                or row["source_type"] == "logical_concat"
                or row["content_digest"] != expected_content_digest
            ):
                raise ValueError("source Asset is no longer pending at expected digest")
            cursor = connection.execute(
                """
                UPDATE knowledge_structured_assets
                SET source_reference_digest = ?, profile_reference_digest = ?, profile_status = 'ready',
                    row_count = ?, column_count = ?, columns_json = ?, reference_status = 'ready',
                    metadata_json = ?, updated_at = ?
                WHERE id = ? AND space_id = ? AND reference_status = 'pending' AND content_digest = ?
                """,
                (
                    expected_content_digest,
                    expected_content_digest,
                    profile.row_count,
                    len(profile.columns),
                    json.dumps(list(profile.columns), ensure_ascii=False),
                    json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                    now,
                    asset_id,
                    space_id,
                    expected_content_digest,
                ),
            )
            if cursor.rowcount != 1:
                raise ValueError("source binding CAS failed")
        return {
            "id": asset_id,
            "space_id": space_id,
            "kind": "structured_asset",
            "title": str(row["file_name"] or ""),
            "source_uri": str(row["source_uri"]),
            "content_digest": expected_content_digest,
            "reference_status": "ready",
            "profile_status": "ready",
            "row_count": profile.row_count,
            "column_count": len(profile.columns),
            "columns": list(profile.columns),
            "capabilities": ["table_query"],
        }

    def bind_collection_provider(
        self,
        *,
        principal: Principal,
        collection_id: str,
        collection_version: str,
        space_id: str,
        capability: str,
        binding: Mapping[str, str],
    ) -> dict[str, object]:
        """Persist one explicit Collection-to-provider identity in the Catalog."""

        scopes = set(principal.scopes)
        if principal.tenant_id is not None or not (
            {"knowledge.processing", "knowledge:processing", "knowledge.admin", "knowledge:admin"} & scopes
        ):
            raise PermissionError("Collection binding requires Processing scope")
        if not all(_ID_RE.fullmatch(value) for value in (collection_id, collection_version, space_id)):
            raise ValueError("Collection binding identity is invalid")
        if capability not in _CAPABILITIES or set(binding) not in ({"asset_id"}, {"dataset_id"}, {"provider_id"}):
            raise ValueError("Collection binding is invalid")
        if "provider_id" in binding and capability not in {"document_rag_query", "wiki_query"}:
            raise ValueError("provider binding capability is invalid")
        if not ({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes):
            raise PermissionError("Collection binding Space scope is required")
        identity = next(iter(binding.values()))
        if not _ID_RE.fullmatch(identity):
            raise ValueError("Collection binding provider identity is invalid")
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._database_path) as connection:
            collection = connection.execute(
                "SELECT capabilities, asset_ids FROM knowledge_datasets WHERE id = ? AND space_id = ? AND version = ?",
                (collection_id, space_id, collection_version),
            ).fetchone()
            if collection is None:
                raise ValueError("Collection is not found")
            capabilities = json.loads(collection[0])
            asset_ids = json.loads(collection[1])
            if (
                not isinstance(capabilities, list)
                or any(type(item) is not str for item in capabilities)
                or not isinstance(asset_ids, list)
                or any(type(item) is not str for item in asset_ids)
            ):
                raise ValueError("Collection metadata is invalid")
            if capability not in capabilities:
                capabilities.append(capability)
            if "asset_id" in binding and identity not in asset_ids:
                asset_ids.append(identity)
            if capability == "table_query" and "asset_id" in binding:
                source = connection.execute(
                    "SELECT space_id, reference_status, capabilities FROM knowledge_structured_assets WHERE id = ?",
                    (identity,),
                ).fetchone()
                if source is None or source[0] != space_id or source[1] not in {"ready", "verified", "active"}:
                    raise ValueError("table Collection binding requires an approved Structured Asset")
                source_capabilities = json.loads(source[2])
                if not isinstance(source_capabilities, list) or "table_query" not in source_capabilities:
                    raise ValueError("table Collection binding capability is not approved")
            connection.execute(
                "UPDATE knowledge_datasets SET capabilities = ?, asset_ids = ?, updated_at = ? "
                "WHERE id = ? AND space_id = ? AND version = ?",
                (
                    json.dumps(capabilities, ensure_ascii=False),
                    json.dumps(asset_ids, ensure_ascii=False),
                    now,
                    collection_id,
                    space_id,
                    collection_version,
                ),
            )
            connection.execute(
                """
                INSERT INTO knowledge_collection_bindings
                    (space_id, collection_id, collection_version, capability, binding_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(space_id, collection_id, collection_version, capability)
                DO UPDATE SET binding_json = excluded.binding_json, updated_at = excluded.updated_at
                """,
                (
                    space_id,
                    collection_id,
                    collection_version,
                    capability,
                    json.dumps(dict(binding), ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
        return {
            "space_id": space_id,
            "collection_id": collection_id,
            "collection_version": collection_version,
            "capability": capability,
            "binding": dict(binding),
        }
