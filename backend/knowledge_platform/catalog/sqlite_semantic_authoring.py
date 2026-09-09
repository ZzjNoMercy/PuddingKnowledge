"""Transactional SQLite writer for Platform semantic-dimension jobs."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from datetime import datetime, timezone
from pathlib import Path

from knowledge_contracts import Principal

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc|opt|usr|root)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_LEGACY_ID_KEYS = frozenset({"session_id", "query_id", "run_id", "goal_id"})


def _unsafe(value: object) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_RE.search(value) or _PATH_RE.search(value))
    if isinstance(value, Mapping):
        return any(
            str(key).casefold() in _LEGACY_ID_KEYS or _unsafe(key) or _unsafe(item)
            for key, item in value.items()
        )
    if isinstance(value, (list, tuple)):
        return any(_unsafe(item) for item in value)
    return False


class SqliteSemanticDimensionJobWriter:
    """Create only queued jobs after atomic source lineage validation."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError(f"Catalog database does not exist: {self._database_path}")

    def create_authoring_job(self, *, record: Mapping[str, object]) -> dict[str, object]:
        required = (
            "id",
            "kind",
            "dimension_id",
            "adapter",
            "scope_uri",
            "scope_json",
            "input_snapshot_json",
            "status",
            "current_step",
            "progress",
            "source_snapshot",
        )
        if any(field not in record for field in required):
            raise ValueError("authoring job record is incomplete")
        job_id = str(record["id"])
        dimension_id = str(record["dimension_id"])
        adapter = str(record["adapter"])
        if not _ID_RE.fullmatch(job_id) or not _ID_RE.fullmatch(dimension_id) or not _ID_RE.fullmatch(adapter):
            raise ValueError("authoring job identity is invalid")
        if record["kind"] != "semantic_dimension_build" or record["status"] != "queued" or record["current_step"] != "queued":
            raise ValueError("authoring job state is invalid")
        if record["progress"] != 0:
            raise ValueError("queued authoring job must have zero progress")
        space_id = self._space_from_uri(str(record["scope_uri"]), dimension_id)
        if not self._authorized(principal=record.get("principal"), space_id=space_id):
            raise PermissionError("authoring job persistence requires Admin scope")
        scope = record["scope_json"]
        snapshot = record["input_snapshot_json"]
        source_snapshot = record["source_snapshot"]
        if not isinstance(scope, Mapping) or not isinstance(snapshot, Mapping) or _unsafe(scope) or _unsafe(snapshot):
            raise ValueError("authoring job metadata is unsafe")
        if not isinstance(source_snapshot, (list, tuple)) or not source_snapshot:
            raise ValueError("authoring job source snapshot is invalid")
        normalized_sources = []
        for item in source_snapshot:
            if not isinstance(item, Mapping) or set(item) != {"asset_id", "content_digest"}:
                raise ValueError("authoring job source snapshot is invalid")
            asset_id = str(item["asset_id"])
            digest = str(item["content_digest"])
            if not _ID_RE.fullmatch(asset_id) or not _DIGEST_RE.fullmatch(digest) or _unsafe(item):
                raise ValueError("authoring job source snapshot is unsafe")
            normalized_sources.append({"asset_id": asset_id, "content_digest": digest})
        snapshot_source_ids = snapshot.get("source_asset_ids")
        if snapshot_source_ids != [item["asset_id"] for item in normalized_sources]:
            raise ValueError("authoring job source lineage is invalid")
        now = datetime.now(timezone.utc).isoformat()
        result = {
            "id": job_id,
            "kind": "semantic_dimension_build",
            "dimension_id": dimension_id,
            "adapter": adapter,
            "scope_uri": str(record["scope_uri"]),
            "scope_json": dict(scope),
            "input_snapshot_json": dict(snapshot),
            "status": "queued",
            "current_step": "queued",
            "progress": 0,
            "staging_uri": "",
            "staging_reference_digest": "",
            "published_uri": "",
            "published_reference_digest": "",
            "result_summary_json": {},
            "correlation_json": dict(record.get("correlation_json") or {}),
            "error_message": "",
            "retry_count": 0,
            "metadata_json": dict(record.get("metadata_json") or {}),
            "created_at": now,
            "updated_at": now,
            "started_at": None,
            "finished_at": None,
            "lease_owner": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
            "attempt": 0,
        }
        with sqlite3.connect(self._database_path) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            existing = connection.execute(
                "SELECT id, kind, dimension_id, adapter, scope_uri, scope_json, input_snapshot_json, status, "
                "current_step, progress, staging_uri, staging_reference_digest, published_uri, published_reference_digest, "
                "result_summary_json, correlation_json, error_message, retry_count, metadata_json, created_at, updated_at, "
                "started_at, finished_at, lease_owner, lease_expires_at, heartbeat_at, attempt "
                "FROM knowledge_authoring_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if existing is not None:
                if existing[6] != json.dumps(snapshot, ensure_ascii=False, sort_keys=True) or existing[5] != json.dumps(scope, ensure_ascii=False, sort_keys=True):
                    raise ValueError("authoring job identity already has a different definition")
                return dict(zip(result, existing, strict=True))
            for item in normalized_sources:
                source = connection.execute(
                    "SELECT space_id, content_digest, reference_status, capabilities "
                    "FROM knowledge_structured_assets WHERE id = ?",
                    (item["asset_id"],),
                ).fetchone()
                if source is None or source[0] != space_id or source[1] != item["content_digest"] or source[2] not in {"ready", "verified", "active"}:
                    raise ValueError("semantic dimension source is not approved")
                try:
                    capabilities = json.loads(source[3] or "[]")
                except json.JSONDecodeError as error:
                    raise ValueError("semantic dimension source capabilities are invalid") from error
                if not isinstance(capabilities, list) or "table_query" not in {str(value) for value in capabilities}:
                    raise ValueError("semantic dimension source lacks table_query capability")
            insert_values = [result[key] for key in result]
            for key in ("scope_json", "input_snapshot_json", "result_summary_json", "correlation_json", "metadata_json"):
                insert_values[list(result).index(key)] = json.dumps(result[key], ensure_ascii=False, sort_keys=True)
            connection.execute(
                """
                INSERT INTO knowledge_authoring_jobs (
                    id, kind, dimension_id, adapter, scope_uri, scope_json, input_snapshot_json,
                    status, current_step, progress, staging_uri, staging_reference_digest,
                    published_uri, published_reference_digest, result_summary_json, correlation_json,
                    error_message, retry_count, metadata_json, created_at, updated_at, started_at,
                    finished_at, lease_owner, lease_expires_at, heartbeat_at, attempt
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                tuple(insert_values),
            )
            connection.execute(
                "INSERT INTO knowledge_authoring_events (id, job_id, level, message, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (f"{job_id}_event", job_id, "info", "语义维度 Processing 任务已入队", "{}", now),
            )
        return result

    def decide_authoring_job(
        self,
        *,
        job_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
        actor_digest: str,
        correlation_digest: str,
    ) -> dict[str, object]:
        """Atomically resolve a waiting job without claiming it is published."""

        if not _ID_RE.fullmatch(job_id) or not _ID_RE.fullmatch(space_id):
            raise ValueError("semantic job decision identity is invalid")
        if decision not in {"confirm", "reject"} or expected_status not in {
            "waiting_for_publish_confirmation",
            "waiting_for_baseline_change_confirmation",
        }:
            raise ValueError("semantic job decision is invalid")
        if not _DIGEST_RE.fullmatch(actor_digest) or not _DIGEST_RE.fullmatch(correlation_digest):
            raise ValueError("semantic job decision audit digest is invalid")
        decision_id = "authoring_decision_" + hashlib.sha256(
            f"{job_id}\0{expected_status}\0{decision}".encode()
        ).hexdigest()[:48]
        now = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self._database_path) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT id, kind, dimension_id, adapter, scope_uri, status, current_step, progress, "
                "staging_uri, staging_reference_digest, published_uri, published_reference_digest, "
                "result_summary_json, correlation_json, error_message, retry_count, metadata_json, "
                "created_at, updated_at, started_at, finished_at, lease_owner, lease_expires_at, heartbeat_at, attempt "
                "FROM knowledge_authoring_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if row is None:
                raise LookupError("semantic authoring job does not exist")
            actual_space = self._space_from_uri(str(row["scope_uri"]), str(row["dimension_id"]))
            if actual_space != space_id:
                raise PermissionError("semantic authoring job belongs to another Space")
            status = str(row["status"] or "")
            if status == expected_status:
                next_status = "queued" if decision == "confirm" else "cancelled"
                connection.execute(
                    """
                    UPDATE knowledge_authoring_jobs
                       SET status = ?, current_step = ?, progress = ?, error_message = ?,
                           updated_at = ?, finished_at = ?, lease_owner = NULL,
                           lease_expires_at = NULL, heartbeat_at = NULL
                     WHERE id = ? AND status = ?
                    """,
                    (
                        next_status,
                        next_status,
                        0 if decision == "confirm" else int(row["progress"] or 0),
                        "" if decision == "confirm" else "cancelled by Platform Admin decision",
                        now,
                        None if decision == "confirm" else now,
                        job_id,
                        expected_status,
                    ),
                )
                metadata = json.dumps(
                    {
                        "actor_digest": actor_digest,
                        "correlation_digest": correlation_digest,
                        "decision": decision,
                        "expected_status": expected_status,
                    },
                    sort_keys=True,
                )
                connection.execute(
                    "INSERT OR IGNORE INTO knowledge_authoring_events "
                    "(id, job_id, level, message, metadata_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        decision_id,
                        job_id,
                        "info" if decision == "confirm" else "warning",
                        "semantic authoring job confirmed and re-queued"
                        if decision == "confirm"
                        else "semantic authoring job rejected and cancelled",
                        metadata,
                        now,
                    ),
                )
            else:
                next_status = "queued" if decision == "confirm" else "cancelled"
                decision_event = connection.execute(
                    "SELECT 1 FROM knowledge_authoring_events WHERE id = ? AND job_id = ?",
                    (decision_id, job_id),
                ).fetchone()
                if status != next_status or decision_event is None:
                    raise ValueError("semantic authoring job is not waiting for the expected decision")
            result = connection.execute(
                "SELECT id, kind, dimension_id, adapter, scope_uri, status, current_step, progress, "
                "staging_uri, staging_reference_digest, published_uri, published_reference_digest, "
                "result_summary_json, correlation_json, error_message, retry_count, metadata_json, "
                "created_at, updated_at, started_at, finished_at, lease_owner, lease_expires_at, heartbeat_at, attempt "
                "FROM knowledge_authoring_jobs WHERE id = ?",
                (job_id,),
            ).fetchone()
            if result is None:
                raise LookupError("semantic authoring job disappeared")
            return dict(result)

    @staticmethod
    def _space_from_uri(uri: str, dimension_id: str) -> str:
        expected_prefix = "knowledge://spaces/"
        suffix = f"/semantic-dimensions/{dimension_id}"
        if not uri.startswith(expected_prefix) or not uri.endswith(suffix):
            raise ValueError("authoring job scope URI is not canonical")
        space_id = uri[len(expected_prefix) : -len(suffix)]
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("authoring job Space is invalid")
        return space_id

    @staticmethod
    def _authorized(*, principal: object, space_id: str) -> bool:
        if not isinstance(principal, Principal) or principal.tenant_id is not None:
            return False
        scopes = set(principal.scopes)
        return bool(
            {
                "knowledge.admin",
                "knowledge:admin",
                "knowledge.semantic_authoring",
                "knowledge:semantic_authoring",
            }
            & scopes
        ) and bool(
            {f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes
        )
