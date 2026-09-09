"""Platform-owned SQL Guardrail authoring and durable registry boundaries.

This module deliberately has no dependency on the legacy analytics package or
on a host definition directory. Authoring is a two-step operation: create a
reviewable pending rule, then explicitly confirm or reject it. Active rules
are stored as bounded JSON and can be supplied to a Platform-owned validator.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
    QueryPlanValidation,
    QueryResult,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_SECRET_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")
_PATH_RE = re.compile(r"(?:^|[/\\])(?:Users|home|tmp|private|var|etc|opt|usr|root)(?:[/\\]|$)|\.\.(?:[/\\]|$)")
_LEGACY_ID_KEYS = frozenset({"session_id", "query_id", "run_id", "goal_id", "analytics_model_id"})
_RULE_TYPES = frozenset(
    {
        "forbid_sql_pattern",
        "require_sql_contains",
        "require_table_when_available",
        "require_group_by",
        "forbid_exists_distinct_pattern",
    }
)
_ACTION_TYPES = frozenset({"rewrite", "block", "warn"})
_MAX_JSON_BYTES = 128 * 1024


def _digest(value: object) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


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


def _admin_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool(
            {
                "knowledge.admin",
                "knowledge:admin",
                "knowledge.sql_guardrail_admin",
                "knowledge:sql_guardrail_admin",
            }
            & scopes
        )
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _json_object(value: object, *, field: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} must be an object")
    if _unsafe(value):
        raise ValueError(f"{field} contains unsafe data")
    try:
        encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as error:
        raise ValueError(f"{field} is not JSON-safe") from error
    if len(encoded.encode("utf-8")) > _MAX_JSON_BYTES:
        raise ValueError(f"{field} is too large")
    return dict(value)


def _nonempty_string(value: object, *, field: str, limit: int = 256) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.strip()) > limit:
        raise ValueError(f"{field} is invalid")
    if _unsafe(value):
        raise ValueError(f"{field} contains unsafe data")
    return value.strip()


def _string_list(value: object, *, field: str, limit: int = 500) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or len(value) > limit:
        raise ValueError(f"{field} is invalid")
    result = tuple(_nonempty_string(item, field=f"{field}[]", limit=256) for item in value)
    if len(set(result)) != len(result):
        raise ValueError(f"{field} must be unique")
    return result


def _validate_scope(value: object) -> dict[str, object]:
    scope = _json_object(value or {}, field="scope")
    if set(scope) - {"table_scope", "semantic_assets", "intent_any"}:
        raise ValueError("scope contains unsupported fields")
    table_scope = scope.get("table_scope", {})
    if not isinstance(table_scope, Mapping):
        raise ValueError("scope.table_scope must be an object")
    if set(table_scope) - {"mode", "values"}:
        raise ValueError("scope.table_scope contains unsupported fields")
    mode = str(table_scope.get("mode", "any"))
    if mode not in {"any", "all"}:
        raise ValueError("scope.table_scope.mode is invalid")
    return {
        "table_scope": {
            "mode": mode,
            "values": list(_string_list(table_scope.get("values", []), field="scope.table_scope.values")),
        },
        "semantic_assets": list(_string_list(scope.get("semantic_assets", []), field="scope.semantic_assets")),
        "intent_any": list(_string_list(scope.get("intent_any", []), field="scope.intent_any")),
    }


def _validate_params(rule_type: str, value: object) -> dict[str, object]:
    params = _json_object(value or {}, field="params")
    if rule_type == "forbid_sql_pattern":
        if set(params) - {"pattern", "unless_contains", "unless_pattern", "flags"}:
            raise ValueError("params contains unsupported fields")
        params["pattern"] = _nonempty_string(params.get("pattern"), field="params.pattern", limit=4096)
        try:
            re.compile(params["pattern"])
        except re.error as error:
            raise ValueError("params.pattern is not a valid regular expression") from error
        for name in ("unless_contains", "unless_pattern"):
            if name in params and params[name] != "":
                params[name] = _nonempty_string(params[name], field=f"params.{name}", limit=4096)
                if name == "unless_pattern":
                    try:
                        re.compile(params[name])
                    except re.error as error:
                        raise ValueError("params.unless_pattern is not a valid regular expression") from error
        if "flags" in params:
            params["flags"] = list(_string_list(params["flags"], field="params.flags", limit=8))
    elif rule_type == "require_sql_contains":
        if set(params) - {"contains", "when_contains_any"}:
            raise ValueError("params contains unsupported fields")
        params["contains"] = _nonempty_string(params.get("contains"), field="params.contains", limit=4096)
        if "when_contains_any" in params:
            params["when_contains_any"] = list(
                _string_list(params["when_contains_any"], field="params.when_contains_any")
            )
    elif rule_type == "require_table_when_available":
        if set(params) - {"required_table", "fallback_table"}:
            raise ValueError("params contains unsupported fields")
        params["required_table"] = _nonempty_string(params.get("required_table"), field="params.required_table")
        if params.get("fallback_table"):
            params["fallback_table"] = _nonempty_string(params["fallback_table"], field="params.fallback_table")
    elif rule_type == "require_group_by":
        if set(params) - {"require_columns", "forbidden_columns_only"}:
            raise ValueError("params contains unsupported fields")
        required = _string_list(params.get("require_columns", []), field="params.require_columns")
        forbidden = _string_list(params.get("forbidden_columns_only", []), field="params.forbidden_columns_only")
        if not required and not forbidden:
            raise ValueError("group-by guardrail requires columns")
        params = {"require_columns": list(required), "forbidden_columns_only": list(forbidden)}
    elif rule_type == "forbid_exists_distinct_pattern":
        if set(params) - {"table", "distinct_column", "min_exists_count"}:
            raise ValueError("params contains unsupported fields")
        params["table"] = _nonempty_string(params.get("table"), field="params.table")
        params["distinct_column"] = _nonempty_string(params.get("distinct_column"), field="params.distinct_column")
        minimum = params.get("min_exists_count", 2)
        if type(minimum) is not int or minimum < 2 or minimum > 20:
            raise ValueError("params.min_exists_count is invalid")
        params["min_exists_count"] = minimum
    return params


@dataclass(frozen=True, slots=True)
class SqlGuardrailRule:
    id: str
    space_id: str
    name: str
    rule_type: str
    scope: Mapping[str, object]
    params: Mapping[str, object]
    action: str = "rewrite"
    message: str = ""
    enabled: bool = True

    def __post_init__(self) -> None:
        for value, field in ((self.id, "id"), (self.space_id, "space_id")):
            if not _ID_RE.fullmatch(value):
                raise ValueError(f"{field} is invalid")
        object.__setattr__(self, "name", _nonempty_string(self.name, field="name", limit=200))
        if self.rule_type not in _RULE_TYPES:
            raise ValueError("rule_type is unsupported")
        if self.action not in _ACTION_TYPES:
            raise ValueError("action is invalid")
        object.__setattr__(self, "scope", _validate_scope(self.scope))
        object.__setattr__(self, "params", _validate_params(self.rule_type, self.params))
        if not isinstance(self.message, str) or len(self.message) > 2000 or _unsafe(self.message):
            raise ValueError("message is invalid")
        if type(self.enabled) is not bool:
            raise ValueError("enabled must be boolean")

    def definition(self) -> dict[str, object]:
        return {
            "id": self.id,
            "space_id": self.space_id,
            "name": self.name,
            "rule_type": self.rule_type,
            "scope": dict(self.scope),
            "params": dict(self.params),
            "action": self.action,
            "message": self.message,
            "enabled": self.enabled,
        }

    @property
    def definition_digest(self) -> str:
        return _digest(self.definition())


@dataclass(frozen=True, slots=True)
class SqlGuardrailRecord:
    rule: SqlGuardrailRule
    status: str
    definition_digest: str
    created_at: str
    updated_at: str
    actor_digest: str
    decision_digest: str = ""

    def __post_init__(self) -> None:
        if self.status not in {"waiting_for_confirmation", "active", "rejected", "retired"}:
            raise ValueError("guardrail status is invalid")
        if not _DIGEST_RE.fullmatch(self.definition_digest) or not _DIGEST_RE.fullmatch(self.actor_digest):
            raise ValueError("guardrail record digest is invalid")
        if self.decision_digest and not _DIGEST_RE.fullmatch(self.decision_digest):
            raise ValueError("guardrail decision digest is invalid")
        if self.definition_digest != self.rule.definition_digest:
            raise ValueError("guardrail definition digest does not match rule")

    def public(self) -> dict[str, object]:
        return {
            "id": self.rule.id,
            "space_id": self.rule.space_id,
            "name": self.rule.name,
            "rule_type": self.rule.rule_type,
            "scope": dict(self.rule.scope),
            "params": dict(self.rule.params),
            "action": self.rule.action,
            "message": self.rule.message,
            "enabled": self.rule.enabled,
            "status": self.status,
            "definition_digest": self.definition_digest,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "actor_digest": self.actor_digest,
            "decision_digest": self.decision_digest,
        }


class SqlGuardrailRepository(Protocol):
    def create(self, *, record: SqlGuardrailRecord) -> SqlGuardrailRecord: ...

    def decide(
        self,
        *,
        guardrail_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
        actor_digest: str,
        decision_digest: str,
    ) -> SqlGuardrailRecord: ...

    def list(self, *, space_id: str, status: str | None = None) -> tuple[SqlGuardrailRecord, ...]: ...


class InMemorySqlGuardrailRepository:
    """Small deterministic repository for contract and adapter tests."""

    def __init__(self) -> None:
        self._records: dict[tuple[str, str], SqlGuardrailRecord] = {}

    def create(self, *, record: SqlGuardrailRecord) -> SqlGuardrailRecord:
        key = (record.rule.space_id, record.rule.id)
        prior = self._records.get(key)
        if prior is not None:
            if prior.definition_digest != record.definition_digest:
                raise ValueError("guardrail identity already has a different definition")
            return prior
        self._records[key] = record
        return record

    def decide(
        self,
        *,
        guardrail_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
        actor_digest: str,
        decision_digest: str,
    ) -> SqlGuardrailRecord:
        key = (space_id, guardrail_id)
        prior = self._records.get(key)
        if prior is None:
            raise LookupError("guardrail does not exist")
        if prior.status == expected_status:
            if decision not in {"confirm", "reject"}:
                raise ValueError("guardrail decision is invalid")
            status = "active" if decision == "confirm" else "rejected"
            record = SqlGuardrailRecord(
                rule=prior.rule,
                status=status,
                definition_digest=prior.definition_digest,
                created_at=prior.created_at,
                updated_at=_now(),
                actor_digest=actor_digest,
                decision_digest=decision_digest,
            )
            self._records[key] = record
            return record
        if prior.decision_digest == decision_digest:
            return prior
        raise ValueError("guardrail decision conflicts with current state")

    def list(self, *, space_id: str, status: str | None = None) -> tuple[SqlGuardrailRecord, ...]:
        return tuple(
            record
            for (record_space, _), record in sorted(self._records.items())
            if record_space == space_id and (status is None or record.status == status)
        )


class SqliteSqlGuardrailRepository:
    """Durable Platform state in an explicitly supplied SQLite database."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink():
            raise OSError("guardrail database must not be a symlink")
        current = Path(self._database_path.anchor or "/")
        for part in self._database_path.parent.parts[1:]:
            current /= part
            if current.is_symlink():
                raise OSError("guardrail database parent directories must not be symlinks")
        self._database_path.parent.mkdir(parents=True, exist_ok=True)
        self._ensure_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        return connection

    def _ensure_schema(self) -> None:
        with self._connect() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS knowledge_sql_guardrails (
                    space_id TEXT NOT NULL,
                    id TEXT NOT NULL,
                    status TEXT NOT NULL CHECK(status IN ('waiting_for_confirmation', 'active', 'rejected', 'retired')),
                    definition_json TEXT NOT NULL,
                    definition_digest TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    actor_digest TEXT NOT NULL,
                    decision_digest TEXT NOT NULL DEFAULT '',
                    PRIMARY KEY (space_id, id)
                )
                """
            )

    @staticmethod
    def _decode(row: sqlite3.Row) -> SqlGuardrailRecord:
        definition = json.loads(str(row["definition_json"]))
        if not isinstance(definition, Mapping):
            raise ValueError("stored guardrail definition is invalid")
        rule = SqlGuardrailRule(
            id=str(definition.get("id") or ""),
            space_id=str(definition.get("space_id") or ""),
            name=str(definition.get("name") or ""),
            rule_type=str(definition.get("rule_type") or ""),
            scope=definition.get("scope") or {},
            params=definition.get("params") or {},
            action=str(definition.get("action") or ""),
            message=str(definition.get("message") or ""),
            enabled=definition.get("enabled", True),
        )
        return SqlGuardrailRecord(
            rule=rule,
            status=str(row["status"]),
            definition_digest=str(row["definition_digest"]),
            created_at=str(row["created_at"]),
            updated_at=str(row["updated_at"]),
            actor_digest=str(row["actor_digest"]),
            decision_digest=str(row["decision_digest"] or ""),
        )

    def create(self, *, record: SqlGuardrailRecord) -> SqlGuardrailRecord:
        encoded = json.dumps(record.rule.definition(), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            prior = connection.execute(
                "SELECT * FROM knowledge_sql_guardrails WHERE space_id = ? AND id = ?",
                (record.rule.space_id, record.rule.id),
            ).fetchone()
            if prior is not None:
                decoded = self._decode(prior)
                if decoded.definition_digest != record.definition_digest:
                    raise ValueError("guardrail identity already has a different definition")
                return decoded
            connection.execute(
                "INSERT INTO knowledge_sql_guardrails (space_id, id, status, definition_json, definition_digest, created_at, updated_at, actor_digest, decision_digest) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    record.rule.space_id,
                    record.rule.id,
                    record.status,
                    encoded,
                    record.definition_digest,
                    record.created_at,
                    record.updated_at,
                    record.actor_digest,
                    record.decision_digest,
                ),
            )
        return record

    def decide(
        self,
        *,
        guardrail_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
        actor_digest: str,
        decision_digest: str,
    ) -> SqlGuardrailRecord:
        if decision not in {"confirm", "reject"} or expected_status != "waiting_for_confirmation":
            raise ValueError("guardrail decision is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM knowledge_sql_guardrails WHERE space_id = ? AND id = ?",
                (space_id, guardrail_id),
            ).fetchone()
            if row is None:
                raise LookupError("guardrail does not exist")
            prior = self._decode(row)
            if prior.status != expected_status:
                if prior.decision_digest == decision_digest:
                    return prior
                raise ValueError("guardrail decision conflicts with current state")
            status = "active" if decision == "confirm" else "rejected"
            updated_at = _now()
            connection.execute(
                "UPDATE knowledge_sql_guardrails SET status = ?, updated_at = ?, actor_digest = ?, decision_digest = ? WHERE space_id = ? AND id = ? AND status = ?",
                (status, updated_at, actor_digest, decision_digest, space_id, guardrail_id, expected_status),
            )
            return SqlGuardrailRecord(
                rule=prior.rule,
                status=status,
                definition_digest=prior.definition_digest,
                created_at=prior.created_at,
                updated_at=updated_at,
                actor_digest=actor_digest,
                decision_digest=decision_digest,
            )

    def list(self, *, space_id: str, status: str | None = None) -> tuple[SqlGuardrailRecord, ...]:
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("space_id is invalid")
        with self._connect() as connection:
            if status is None:
                rows = connection.execute(
                    "SELECT * FROM knowledge_sql_guardrails WHERE space_id = ? ORDER BY id", (space_id,)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM knowledge_sql_guardrails WHERE space_id = ? AND status = ? ORDER BY id",
                    (space_id, status),
                ).fetchall()
        return tuple(self._decode(row) for row in rows)


class SqlGuardrailAdminService:
    """Create and decide rules without mutating the active set implicitly."""

    def __init__(self, *, repository: SqlGuardrailRepository) -> None:
        self._repository = repository

    def create(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        rule: SqlGuardrailRule,
    ) -> QueryResult:
        if not _admin_authorized(principal, rule.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "SQL guardrail authoring requires Admin scope")
        actor_digest = _digest(principal.subject_id)
        now = _now()
        record = SqlGuardrailRecord(
            rule=rule,
            status="waiting_for_confirmation",
            definition_digest=rule.definition_digest,
            created_at=now,
            updated_at=now,
            actor_digest=actor_digest,
        )
        try:
            written = self._repository.create(record=record)
        except (TypeError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail definition conflicts with existing state")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "SQL guardrail registry is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="SQL Guardrail 已进入待确认状态。",
            data={"guardrail": written.public()},
            evidence=(
                Evidence(
                    asset_id=rule.id,
                    resource_uri=f"knowledge://spaces/{rule.space_id}/sql-guardrails/{rule.id}",
                    locator={"section": "sql_guardrail_definition"},
                    quote=rule.definition_digest,
                    revision=rule.definition_digest,
                    matched_by=("admin_authoring",),
                ),
            ),
            provenance=Provenance(
                space_id=rule.space_id,
                dataset_id=None,
                dataset_version=None,
                capability="sql_guardrail_admin",
                catalog_revision=None,
            ),
        )

    def decide(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        guardrail_id: str,
        space_id: str,
        decision: str,
        expected_status: str,
    ) -> QueryResult:
        if not _ID_RE.fullmatch(guardrail_id) or not _ID_RE.fullmatch(space_id):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail identity is invalid")
        if not _admin_authorized(principal, space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "SQL guardrail decisions require Admin scope")
        if decision not in {"confirm", "reject"} or expected_status != "waiting_for_confirmation":
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail decision is invalid")
        actor_digest = _digest(principal.subject_id)
        decision_digest = _digest(
            {"guardrail_id": guardrail_id, "space_id": space_id, "decision": decision, "expected_status": expected_status}
        )
        try:
            written = self._repository.decide(
                guardrail_id=guardrail_id,
                space_id=space_id,
                decision=decision,
                expected_status=expected_status,
                actor_digest=actor_digest,
                decision_digest=decision_digest,
            )
        except PermissionError:
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "SQL guardrail decision is not authorized")
        except (LookupError, ValueError):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "SQL guardrail decision conflicts with current state")
        except Exception:
            return _error(correlation, QueryErrorCode.CAPABILITY_UNAVAILABLE, "SQL guardrail registry is unavailable")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="SQL Guardrail 已激活。" if written.status == "active" else "SQL Guardrail 已拒绝。",
            data={"guardrail": written.public(), "decision": decision},
            evidence=(
                Evidence(
                    asset_id=guardrail_id,
                    resource_uri=f"knowledge://spaces/{space_id}/sql-guardrails/{guardrail_id}",
                    locator={"section": "sql_guardrail_decision"},
                    quote=decision,
                    revision=written.decision_digest,
                    matched_by=("admin_decision", expected_status),
                ),
            ),
            provenance=Provenance(
                space_id=space_id,
                dataset_id=None,
                dataset_version=None,
                capability="sql_guardrail_admin",
                catalog_revision=None,
            ),
        )

    def list_active(self, *, space_id: str) -> tuple[SqlGuardrailRecord, ...]:
        return self._repository.list(space_id=space_id, status="active")


def _sql_contains(sql: str, value: str) -> bool:
    return re.search(rf"(?<![A-Za-z0-9_]){re.escape(value.strip())}(?![A-Za-z0-9_])", sql, re.IGNORECASE) is not None


def _uses_table(sql: str, table: str) -> bool:
    short_name = table.strip().split(".")[-1]
    return re.search(
        rf"\b(?:from|join)\s+(?:[A-Za-z0-9_]+\.)?{re.escape(short_name)}\b", sql, re.IGNORECASE
    ) is not None


def _guardrail_applies(
    rule: SqlGuardrailRule,
    *,
    allowed_tables: Sequence[str],
    semantic_asset_ids: Sequence[str],
    question: str,
) -> bool | None:
    table_scope = rule.scope.get("table_scope") if isinstance(rule.scope.get("table_scope"), Mapping) else {}
    scoped_tables = {str(item).strip().casefold() for item in table_scope.get("values", []) if str(item).strip()}
    available = {str(item).strip().strip('"').casefold() for item in allowed_tables}
    available |= {item.split(".")[-1] for item in available}
    if scoped_tables:
        if str(table_scope.get("mode", "any")) == "all":
            if not scoped_tables.issubset(available):
                return False
        elif not scoped_tables.intersection(available):
            return False
    required_semantic = {str(item) for item in rule.scope.get("semantic_assets", [])}
    if required_semantic and not required_semantic.issubset(set(semantic_asset_ids)):
        return None if not semantic_asset_ids else False
    intents = tuple(str(item).casefold() for item in rule.scope.get("intent_any", []))
    if intents and not question.strip():
        return None
    if intents and not any(intent in question.casefold() for intent in intents):
        return False
    return True


def _guardrail_fails(rule: SqlGuardrailRule, *, sql: str, allowed_tables: Sequence[str]) -> bool:
    params = rule.params
    if rule.rule_type == "forbid_sql_pattern":
        flags = 0 if "case_sensitive" in {str(item).casefold() for item in params.get("flags", [])} else re.IGNORECASE
        if re.search(str(params["pattern"]), sql, flags) is None:
            return False
        unless_contains = str(params.get("unless_contains") or "")
        unless_pattern = str(params.get("unless_pattern") or "")
        return not (
            (unless_contains and _sql_contains(sql, unless_contains))
            or (unless_pattern and re.search(unless_pattern, sql, flags))
        )
    if rule.rule_type == "require_sql_contains":
        triggers = tuple(str(item) for item in params.get("when_contains_any", []))
        if triggers and not any(_sql_contains(sql, item) for item in triggers):
            return False
        return not _sql_contains(sql, str(params["contains"]))
    if rule.rule_type == "require_table_when_available":
        required = str(params["required_table"])
        if required.casefold() not in {str(item).split(".")[-1].casefold() for item in allowed_tables}:
            return False
        if _uses_table(sql, required):
            return False
        fallback = str(params.get("fallback_table") or "")
        return not (fallback and _uses_table(sql, fallback))
    if rule.rule_type == "require_group_by":
        match = re.search(r"\bgroup\s+by\s+(.+?)(?:\border\s+by\b|\blimit\b|$)", sql, re.IGNORECASE | re.DOTALL)
        grouped = {part.strip().split(".")[-1].strip(' `"').casefold() for part in (match.group(1).split(",") if match else ())}
        required = {str(item).split(".")[-1].strip(' `"').casefold() for item in params.get("require_columns", [])}
        forbidden = {str(item).split(".")[-1].strip(' `"').casefold() for item in params.get("forbidden_columns_only", [])}
        return bool((required and not required.issubset(grouped)) or (forbidden and grouped == forbidden))
    if rule.rule_type == "forbid_exists_distinct_pattern":
        return (
            _uses_table(sql, str(params["table"]))
            and len(re.findall(r"\b(?:not\s+)?exists\s*\(", sql, re.IGNORECASE)) >= int(params["min_exists_count"])
            and re.search(r"\bcount\s*\(\s*distinct\b", sql, re.IGNORECASE) is not None
            and str(params["distinct_column"]).casefold() in sql.casefold()
        )
    return True


class PlatformSqlGuardrailValidator:
    """Compose active Platform rules with an existing read-only SQL validator."""

    def __init__(
        self,
        *,
        base_validator: object,
        repository: SqlGuardrailRepository,
        space_id: str,
        semantic_asset_ids: Sequence[str] = (),
        question: str = "",
        strict_context: bool = True,
    ) -> None:
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("guardrail validator space_id is invalid")
        self._base_validator = base_validator
        self._repository = repository
        self._space_id = space_id
        self._semantic_asset_ids = tuple(semantic_asset_ids)
        self._question = question
        self._strict_context = strict_context

    def validate(self, *, sql: str, dialect: str, allowed_tables: Sequence[str]) -> QueryPlanValidation:
        base = self._base_validator.validate(sql=sql, dialect=dialect, allowed_tables=allowed_tables)
        if not isinstance(base, QueryPlanValidation):
            return QueryPlanValidation(readonly=False, allowed_tables=False, guardrails_passed=False)
        if not base.passed:
            return base
        for record in self._repository.list(space_id=self._space_id, status="active"):
            applies = _guardrail_applies(
                record.rule,
                allowed_tables=allowed_tables,
                semantic_asset_ids=self._semantic_asset_ids,
                question=self._question,
            )
            if applies is None:
                if self._strict_context:
                    return QueryPlanValidation(base.readonly, base.allowed_tables, False)
                continue
            if applies and _guardrail_fails(record.rule, sql=sql, allowed_tables=allowed_tables) and record.rule.action != "warn":
                return QueryPlanValidation(base.readonly, base.allowed_tables, False)
        return base


__all__ = [
    "InMemorySqlGuardrailRepository",
    "SqlGuardrailAdminService",
    "PlatformSqlGuardrailValidator",
    "SqlGuardrailRecord",
    "SqlGuardrailRepository",
    "SqlGuardrailRule",
    "SqliteSqlGuardrailRepository",
]
