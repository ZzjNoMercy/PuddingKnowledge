"""Platform-owned semantic Markdown registry and Admin boundary.

Semantic Markdown is content, not executable configuration.  A definition is
first stored as a reviewable draft and becomes visible to runtime consumers
only after an explicit Admin decision in the owning Space.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

from knowledge_contracts import (
    Correlation,
    Evidence,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryResult,
    is_valid_knowledge_uri,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_TYPE_RE = re.compile(r"^[a-z][a-z0-9_:-]{0,63}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|cookie|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc|opt|usr|root)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_LEGACY_KEYS = frozenset({"session_id", "query_id", "run_id", "goal_id", "analytics_model_id"})
_SEMANTIC_TYPES = frozenset({"measure", "dimension", "grain", "relation", "analytics_model"})
_STATUSES = frozenset({"waiting_for_confirmation", "active", "rejected", "retired"})
_MAX_BODY_CHARS = 128 * 1024
_MAX_JSON_BYTES = 128 * 1024
_MAX_RESOURCE_READ_BYTES = 8 * 1024 * 1024


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _unsafe(value: object) -> bool:
    if isinstance(value, str):
        return bool(_SECRET_RE.search(value) or _PATH_RE.search(value))
    if isinstance(value, Mapping):
        return any(str(key).casefold() in _LEGACY_KEYS or _unsafe(key) or _unsafe(item) for key, item in value.items())
    if isinstance(value, (list, tuple)):
        return any(_unsafe(item) for item in value)
    return False


def _text(value: object, *, field: str, limit: int, allow_empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > limit or (not allow_empty and not value.strip()):
        raise ValueError(f"{field} is invalid")
    if _unsafe(value):
        raise ValueError(f"{field} contains unsafe data")
    return value.strip() if not allow_empty else value.strip()


def _ids(value: object, *, field: str, limit: int = 100) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{field} is invalid")
    result = tuple(_text(item, field=f"{field}[]", limit=160) for item in value)
    if any(not _ID_RE.fullmatch(item) for item in result) or len(set(result)) != len(result):
        raise ValueError(f"{field} contains invalid or duplicate ids")
    return result


def _string_list(value: object, *, field: str, limit: int = 100) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{field} is invalid")
    result = tuple(_text(item, field=f"{field}[]", limit=256) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must be unique")
    return result


def _json_object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping) or _unsafe(value):
        raise ValueError(f"{field} is invalid")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is not JSON-safe") from error
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field} is too large")
    return dict(value)


def _admin_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.admin", "knowledge:admin", "knowledge.semantic_markdown_admin", "knowledge:semantic_markdown_admin"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _read_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None or not ({"knowledge.read", "knowledge:read", "knowledge.admin", "knowledge:admin"} & scopes):
        return False
    scoped_spaces = {
        scope.split(":", 1)[1]
        for scope in scopes
        if scope.startswith("knowledge.space:")
    } | {
        scope.split(":", 2)[2]
        for scope in scopes
        if scope.startswith("knowledge:space:")
    }
    return not scoped_spaces or space_id in scoped_spaces


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _resource_uri(space_id: str, asset_id: str) -> str:
    # The portable URI contract intentionally excludes ':' from path segments,
    # while typed semantic IDs commonly use ``measure:name``.
    return f"knowledge://spaces/{space_id}/semantics/{asset_id.replace(':', '-') }"


@dataclass(frozen=True, slots=True)
class SemanticMarkdownDefinition:
    id: str
    space_id: str
    semantic_type: str
    name: str
    description: str = ""
    aliases: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    frontmatter: Mapping[str, object] = field(default_factory=dict)
    body: str = ""

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("semantic Markdown identity is invalid")
        if not _TYPE_RE.fullmatch(self.semantic_type) or self.semantic_type not in _SEMANTIC_TYPES:
            raise ValueError("semantic Markdown type is unsupported")
        object.__setattr__(self, "name", _text(self.name, field="name", limit=500))
        object.__setattr__(self, "description", _text(self.description, field="description", limit=4000, allow_empty=True))
        object.__setattr__(self, "aliases", _string_list(self.aliases, field="aliases"))
        object.__setattr__(self, "tags", _ids(self.tags, field="tags"))
        object.__setattr__(self, "frontmatter", _json_object(self.frontmatter, field="frontmatter"))
        body = _text(self.body, field="body", limit=_MAX_BODY_CHARS)
        if not body:
            raise ValueError("body must not be empty")
        object.__setattr__(self, "body", body)

    def definition(self) -> dict[str, object]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "type": self.semantic_type,
            "name": self.name,
            "description": self.description,
            "aliases": list(self.aliases),
            "tags": list(self.tags),
            "frontmatter": dict(self.frontmatter),
            "body": self.body,
        }

    @property
    def definition_digest(self) -> str:
        return _digest(self.definition())


@dataclass(frozen=True, slots=True)
class SemanticMarkdownRecord:
    definition: SemanticMarkdownDefinition
    status: str
    definition_digest: str
    created_at: str
    updated_at: str
    actor_digest: str
    decision_digest: str = ""

    def __post_init__(self) -> None:
        if self.status not in _STATUSES or self.definition_digest != self.definition.definition_digest:
            raise ValueError("semantic Markdown record is invalid")
        if not _DIGEST_RE.fullmatch(self.actor_digest) or (self.decision_digest and not _DIGEST_RE.fullmatch(self.decision_digest)):
            raise ValueError("semantic Markdown audit digest is invalid")

    def public(self) -> dict[str, object]:
        return {**self.definition.definition(), "status": self.status, "definition_digest": self.definition_digest, "created_at": self.created_at, "updated_at": self.updated_at, "actor_digest": self.actor_digest, "decision_digest": self.decision_digest}


class SemanticMarkdownRepository(Protocol):
    def create(self, *, record: SemanticMarkdownRecord) -> SemanticMarkdownRecord: ...
    def decide(self, *, asset_id: str, space_id: str, decision: str, expected_status: str, actor_digest: str, decision_digest: str) -> SemanticMarkdownRecord: ...
    def list(self, *, space_id: str, status: str | None = None) -> tuple[SemanticMarkdownRecord, ...]: ...


class InMemorySemanticMarkdownRepository:
    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SemanticMarkdownRecord] = {}

    def create(self, *, record: SemanticMarkdownRecord) -> SemanticMarkdownRecord:
        key = (record.definition.space_id, record.definition.id)
        prior = self._records.get(key)
        if prior is not None and prior.definition_digest != record.definition_digest:
            raise ValueError("semantic Markdown identity already has a different definition")
        self._records[key] = prior or record
        return self._records[key]

    def decide(self, *, asset_id: str, space_id: str, decision: str, expected_status: str, actor_digest: str, decision_digest: str) -> SemanticMarkdownRecord:
        prior = self._records.get((space_id, asset_id))
        if prior is None:
            raise LookupError("semantic Markdown asset does not exist")
        if prior.status != expected_status:
            if prior.decision_digest == decision_digest:
                return prior
            raise ValueError("semantic Markdown decision conflicts with current state")
        if decision not in {"confirm", "reject"} or expected_status != "waiting_for_confirmation":
            raise ValueError("semantic Markdown decision is invalid")
        status = "active" if decision == "confirm" else "rejected"
        updated = SemanticMarkdownRecord(prior.definition, status, prior.definition_digest, prior.created_at, _now(), actor_digest, decision_digest)
        self._records[(space_id, asset_id)] = updated
        return updated

    def list(self, *, space_id: str, status: str | None = None) -> tuple[SemanticMarkdownRecord, ...]:
        return tuple(record for (record_space, _), record in sorted(self._records.items()) if record_space == space_id and (status is None or record.status == status))


class SqliteSemanticMarkdownRepository:
    """Durable local registry; the database path is supplied by the host."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("semantic Markdown database must not be a symlink")
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS knowledge_semantic_assets (
                    space_id TEXT NOT NULL, id TEXT NOT NULL,
                    type TEXT NOT NULL, name TEXT NOT NULL, description TEXT NOT NULL,
                    aliases TEXT NOT NULL, tags TEXT NOT NULL, frontmatter TEXT NOT NULL,
                    body TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('waiting_for_confirmation','active','rejected','retired')),
                    definition_digest TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    actor_digest TEXT NOT NULL, decision_digest TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (space_id, id)
                )
            """)

    @staticmethod
    def _decode(row: sqlite3.Row) -> SemanticMarkdownRecord:
        definition = SemanticMarkdownDefinition(id=str(row["id"]), space_id=str(row["space_id"]), semantic_type=str(row["type"]), name=str(row["name"]), description=str(row["description"] or ""), aliases=tuple(json.loads(row["aliases"])), tags=tuple(json.loads(row["tags"])), frontmatter=json.loads(row["frontmatter"]), body=str(row["body"]))
        return SemanticMarkdownRecord(definition, str(row["status"]), str(row["definition_digest"]), str(row["created_at"]), str(row["updated_at"]), str(row["actor_digest"]), str(row["decision_digest"] or ""))

    def create(self, *, record: SemanticMarkdownRecord) -> SemanticMarkdownRecord:
        d = record.definition
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM knowledge_semantic_assets WHERE space_id = ? AND id = ?", (d.space_id, d.id)).fetchone()
            if row is not None:
                prior = self._decode(row)
                if prior.definition_digest != record.definition_digest:
                    raise ValueError("semantic Markdown identity already has a different definition")
                return prior
            connection.execute("INSERT INTO knowledge_semantic_assets (space_id,id,type,name,description,aliases,tags,frontmatter,body,status,definition_digest,created_at,updated_at,actor_digest,decision_digest) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (d.space_id, d.id, d.semantic_type, d.name, d.description, json.dumps(list(d.aliases), ensure_ascii=False), json.dumps(list(d.tags), ensure_ascii=False), json.dumps(d.frontmatter, ensure_ascii=False, sort_keys=True), d.body, record.status, record.definition_digest, record.created_at, record.updated_at, record.actor_digest, record.decision_digest))
        return record

    def decide(self, *, asset_id: str, space_id: str, decision: str, expected_status: str, actor_digest: str, decision_digest: str) -> SemanticMarkdownRecord:
        if decision not in {"confirm", "reject"} or expected_status != "waiting_for_confirmation":
            raise ValueError("semantic Markdown decision is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT * FROM knowledge_semantic_assets WHERE space_id = ? AND id = ?", (space_id, asset_id)).fetchone()
            if row is None:
                raise LookupError("semantic Markdown asset does not exist")
            prior = self._decode(row)
            if prior.status != expected_status:
                if prior.decision_digest == decision_digest:
                    return prior
                raise ValueError("semantic Markdown decision conflicts with current state")
            status = "active" if decision == "confirm" else "rejected"
            updated = _now()
            connection.execute("UPDATE knowledge_semantic_assets SET status=?,updated_at=?,actor_digest=?,decision_digest=? WHERE space_id=? AND id=? AND status=?", (status, updated, actor_digest, decision_digest, space_id, asset_id, expected_status))
            return SemanticMarkdownRecord(prior.definition, status, prior.definition_digest, prior.created_at, updated, actor_digest, decision_digest)

    def list(self, *, space_id: str, status: str | None = None) -> tuple[SemanticMarkdownRecord, ...]:
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("space_id is invalid")
        with self._connect() as connection:
            if status is None:
                rows = connection.execute("SELECT * FROM knowledge_semantic_assets WHERE space_id=? ORDER BY id", (space_id,)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM knowledge_semantic_assets WHERE space_id=? AND status=? ORDER BY id", (space_id, status)).fetchall()
        return tuple(self._decode(row) for row in rows)


class SemanticMarkdownAdminService:
    def __init__(self, *, repository: SemanticMarkdownRepository) -> None:
        self._repository = repository

    def discover(self, *, principal: Principal, correlation: Correlation, space_id: str, status: str | None = None) -> QueryResult:
        if not _ID_RE.fullmatch(space_id) or (status is not None and status not in _STATUSES):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown discovery request is invalid")
        if not _admin_authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic Markdown discovery requires Admin scope")
        try:
            records = self._repository.list(space_id=space_id, status=status)
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown registry is unavailable")
        return QueryResult(status="ok", trace_id=correlation.trace_id, answer="已发现语义 Markdown。", data={"assets": [record.public() for record in records]}, provenance=Provenance(space_id=space_id, dataset_id=None, dataset_version=None, capability="semantic_markdown_admin", catalog_revision=None))

    def prepare(self, *, principal: Principal, correlation: Correlation, definition: SemanticMarkdownDefinition) -> QueryResult:
        if not _admin_authorized(principal, definition.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic Markdown authoring requires Admin scope")
        now = _now()
        record = SemanticMarkdownRecord(definition, "waiting_for_confirmation", definition.definition_digest, now, now, _digest(principal.subject_id))
        try:
            written = self._repository.create(record=record)
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown definition conflicts with existing state")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown registry is unavailable")
        return QueryResult(status="ok", trace_id=correlation.trace_id, answer="语义 Markdown 已进入待确认状态。", data={"asset": written.public()}, evidence=(Evidence(asset_id=definition.id, resource_uri=_resource_uri(definition.space_id, definition.id), locator={"section": "semantic_markdown_definition"}, quote=definition.definition_digest, revision=definition.definition_digest, matched_by=("admin_authoring",)),), provenance=Provenance(space_id=definition.space_id, dataset_id=None, dataset_version=None, capability="semantic_markdown_admin", catalog_revision=None))

    def decide(self, *, principal: Principal, correlation: Correlation, asset_id: str, space_id: str, decision: str, expected_status: str) -> QueryResult:
        if not _ID_RE.fullmatch(asset_id) or not _ID_RE.fullmatch(space_id) or decision not in {"confirm", "reject"} or expected_status != "waiting_for_confirmation":
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown decision is invalid")
        if not _admin_authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "semantic Markdown decisions require Admin scope")
        decision_digest = _digest({"asset_id": asset_id, "space_id": space_id, "decision": decision, "expected_status": expected_status})
        try:
            written = self._repository.decide(asset_id=asset_id, space_id=space_id, decision=decision, expected_status=expected_status, actor_digest=_digest(principal.subject_id), decision_digest=decision_digest)
        except (LookupError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic Markdown decision conflicts with current state")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "semantic Markdown registry is unavailable")
        return QueryResult(status="ok", trace_id=correlation.trace_id, answer="语义 Markdown 已激活。" if written.status == "active" else "语义 Markdown 已拒绝。", data={"asset": written.public(), "decision": decision}, evidence=(Evidence(asset_id=asset_id, resource_uri=_resource_uri(space_id, asset_id), locator={"section": "semantic_markdown_decision"}, quote=decision, revision=written.decision_digest, matched_by=("admin_decision", expected_status)),), provenance=Provenance(space_id=space_id, dataset_id=None, dataset_version=None, capability="semantic_markdown_admin", catalog_revision=None))

    def active(self, *, space_id: str) -> tuple[SemanticMarkdownRecord, ...]:
        return self._repository.list(space_id=space_id, status="active")

    def read_resource(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        resource_uri: str,
        start: int = 0,
        end: int = _MAX_RESOURCE_READ_BYTES,
    ) -> QueryResult:
        """Read only an explicitly active semantic definition as Markdown.

        The registry is intentionally the only identity resolver here.  No
        filesystem path, Catalog Asset, Session, or legacy semantic object is
        inferred from the URI.
        """

        parts = resource_uri.removeprefix("knowledge://").split("/")
        if (
            not is_valid_knowledge_uri(resource_uri)
            or len(parts) != 4
            or parts[0] != "spaces"
            or parts[2] != "semantics"
            or not parts[1]
            or not parts[3]
        ):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Semantic Resource URI is invalid")
        space_id = parts[1]
        if not _read_authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "knowledge.read Space scope is required")
        if type(start) is not int or type(end) is not int or start < 0 or end <= start or end - start > _MAX_RESOURCE_READ_BYTES:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Semantic Resource read range is invalid")
        try:
            records = self._repository.list(space_id=space_id, status="active")
        except Exception:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "semantic Markdown registry is unavailable")
        matches = tuple(record for record in records if _resource_uri(space_id, record.definition.id) == resource_uri)
        if not matches:
            return _error(correlation, QueryErrorCode.NOT_FOUND, "Semantic Resource was not found")
        if len(matches) != 1:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Semantic Resource identity is ambiguous")
        record = matches[0]
        body = record.definition.body.encode("utf-8")
        content = body[start:min(end, len(body))]
        content_digest = "sha256:" + hashlib.sha256(content).hexdigest()
        full_digest = "sha256:" + hashlib.sha256(body).hexdigest()
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            data={
                "semantic_asset_id": record.definition.id,
                "space_id": space_id,
                "resource_uri": resource_uri,
                "semantic_type": record.definition.semantic_type,
                "name": record.definition.name,
                "description": record.definition.description,
                "aliases": list(record.definition.aliases),
                "tags": list(record.definition.tags),
                "frontmatter": dict(record.definition.frontmatter),
                "definition_digest": record.definition.definition_digest,
                "content_digest": content_digest,
                "content_start": start,
                "content_end": start + len(content),
                "content_text": content.decode("utf-8"),
                "truncated": start + len(content) < len(body),
                "next_locator": {"start": start + len(content)} if start + len(content) < len(body) else None,
            },
            evidence=(Evidence(
                asset_id=record.definition.id,
                resource_uri=resource_uri,
                locator={"section": "semantic_markdown_body"},
                revision=full_digest,
                matched_by=("active_semantic_markdown",),
            ),),
            provenance=Provenance(
                space_id=space_id,
                dataset_id=None,
                dataset_version=None,
                capability="knowledge_read",
                catalog_revision=None,
            ),
        )
