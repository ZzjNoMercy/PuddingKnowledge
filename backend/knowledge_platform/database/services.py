"""Application services for the two-phase Database Query capability."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from datetime import datetime, timedelta, timezone

from knowledge_contracts import (
    Correlation,
    Principal,
    Provenance,
    QueryError,
    QueryErrorCode,
    QueryPlan,
    QueryPlanValidation,
    QueryResult,
)

from .guardrails import PlatformSqlGuardrailValidator, SqlGuardrailRepository
from .ports import (
    DatabaseDatasetResolver,
    DatabaseExecution,
    DatabaseNl2SqlProvider,
    DatabaseSqlValidator,
    QueryPlanRepository,
    QueryResultRepository,
    ReadonlyDatabaseExecutor,
    StoredQueryPlan,
)

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_PAGE_SIZE = 500
_PLAN_TTL_SECONDS = 300


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    ).hexdigest()


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, *, space_id: str, capability: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({f"knowledge.{capability}", capability} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _scope_digest(principal: Principal) -> str:
    return _digest({"subject_id": principal.subject_id, "scopes": sorted(set(principal.scopes))})


def _sql_digest(sql: str) -> str:
    return _digest(sql.strip())


class DatabaseNl2SqlService:
    """Compile natural language into a server-owned QueryPlan only."""

    def __init__(
        self,
        *,
        datasets: DatabaseDatasetResolver,
        provider: DatabaseNl2SqlProvider,
        validator: DatabaseSqlValidator,
        plans: QueryPlanRepository,
        ttl_seconds: int = _PLAN_TTL_SECONDS,
        guardrails: SqlGuardrailRepository | None = None,
    ) -> None:
        self._datasets = datasets
        self._provider = provider
        self._validator = validator
        self._validator_base = validator
        self._plans = plans
        self._ttl_seconds = max(1, min(int(ttl_seconds), 3600))
        self._guardrails = guardrails

    def _validator_for(
        self, *, space_id: str, semantic_asset_ids: Sequence[str], question: str = ""
    ) -> DatabaseSqlValidator:
        if self._guardrails is None:
            return self._validator_base
        return PlatformSqlGuardrailValidator(
            base_validator=self._validator_base,
            repository=self._guardrails,
            space_id=space_id,
            semantic_asset_ids=semantic_asset_ids,
            question=question,
        )

    def generate(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        space_id: str,
        dataset_id: str,
        question: str,
        semantic_asset_ids: Sequence[str] = (),
    ) -> QueryResult:
        if not isinstance(space_id, str) or not isinstance(dataset_id, str) or not _ID_RE.fullmatch(space_id) or not _ID_RE.fullmatch(dataset_id):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id or dataset_id is invalid")
        if not isinstance(question, str) or not question.strip() or len(question) > 8000:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "question is invalid")
        if not _authorized(principal, space_id=space_id, capability="database_nl2sql"):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database_nl2sql is not authorized")
        binding = self._datasets.resolve(dataset_id=dataset_id, space_id=space_id)
        if binding is None or binding.space_id != space_id or binding.dataset_id != dataset_id:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "database dataset binding is unavailable")
        if any(not isinstance(item, str) or not _ID_RE.fullmatch(item) for item in semantic_asset_ids):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "semantic_asset_ids are invalid")
        try:
            candidate = self._provider.generate(
                question=question.strip(), binding=binding, semantic_asset_ids=tuple(semantic_asset_ids)
            )
            validation = self._validator_for(
                space_id=space_id, semantic_asset_ids=semantic_asset_ids, question=question
            ).validate(
                sql=candidate.sql, dialect=binding.dialect, allowed_tables=binding.allowed_tables
            )
        except LookupError:
            return _error(correlation, QueryErrorCode.QUERY_GENERATION_FAILED, "database SQL candidate is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.QUERY_GENERATION_FAILED, "database SQL generation failed")
        if not isinstance(validation, QueryPlanValidation) or not validation.passed:
            return _error(correlation, QueryErrorCode.QUERY_VALIDATION_FAILED, "database SQL did not pass guardrails")
        issued_at = datetime.now(timezone.utc)
        expires_at = issued_at + timedelta(seconds=self._ttl_seconds)
        plan_id = "qp_" + _digest(
            {
                "subject_id": principal.subject_id,
                "correlation": correlation.trace_id,
                "dataset_id": dataset_id,
                "sql_digest": _sql_digest(candidate.sql),
                "issued_at": issued_at.isoformat(),
            }
        ).removeprefix("sha256:")[:48]
        plan = QueryPlan(
            query_plan_id=plan_id,
            sql=candidate.sql.strip(),
            dialect=binding.dialect,
            dataset_id=dataset_id,
            dataset_version=binding.dataset_version,
            deployment_revision=binding.deployment_revision,
            semantic_context_hash=binding.semantic_context_hash,
            validation=validation,
            expires_at=expires_at.isoformat(),
            evidence=tuple(candidate.evidence),
        )
        stored = StoredQueryPlan(
            plan=plan,
            owner_subject_id=principal.subject_id,
            owner_scope_digest=_scope_digest(principal),
            allowed_tables=binding.allowed_tables,
            source_revision=binding.source_revision,
            semantic_asset_ids=tuple(semantic_asset_ids),
        )
        try:
            self._plans.put(stored=stored)
        except Exception:
            return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "query plan could not be persisted")
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="数据库查询计划已生成，等待只读执行确认。",
            data={"query_plan": plan.to_dict(), "sql_hash": _sql_digest(plan.sql)},
            evidence=tuple(candidate.evidence),
            provenance=Provenance(
                space_id=space_id,
                dataset_id=dataset_id,
                dataset_version=binding.dataset_version,
                capability="database_nl2sql",
                provider_versions={"nl2sql": candidate.provider_version or binding.provider_version},
            ),
        )


class DatabaseExecuteReadonlyService:
    """Execute only an unexpired, owner-bound plan after current-state CAS checks."""

    def __init__(
        self,
        *,
        datasets: DatabaseDatasetResolver,
        plans: QueryPlanRepository,
        validator: DatabaseSqlValidator,
        executor: ReadonlyDatabaseExecutor,
        results: QueryResultRepository | None = None,
        guardrails: SqlGuardrailRepository | None = None,
    ) -> None:
        self._datasets = datasets
        self._plans = plans
        self._validator = validator
        self._validator_base = validator
        self._executor = executor
        self._results = results
        self._guardrails = guardrails

    def _validator_for(self, *, space_id: str, semantic_asset_ids: Sequence[str]) -> DatabaseSqlValidator:
        if self._guardrails is None:
            return self._validator_base
        return PlatformSqlGuardrailValidator(
            base_validator=self._validator_base,
            repository=self._guardrails,
            space_id=space_id,
            semantic_asset_ids=semantic_asset_ids,
        )

    def execute(
        self,
        *,
        principal: Principal,
        correlation: Correlation,
        space_id: str,
        query_plan_id: str,
        page_size: int = 100,
        expected_sql_hash: str | None = None,
    ) -> QueryResult:
        if not isinstance(space_id, str) or not isinstance(query_plan_id, str) or not _ID_RE.fullmatch(space_id) or not _ID_RE.fullmatch(query_plan_id):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "space_id or query_plan_id is invalid")
        if type(page_size) is not int or not 1 <= page_size <= _MAX_PAGE_SIZE:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "page_size is invalid")
        if not _authorized(principal, space_id=space_id, capability="database_execute_readonly"):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "database_execute_readonly is not authorized")
        stored = self._plans.get(query_plan_id=query_plan_id)
        if (
            not isinstance(stored, StoredQueryPlan)
            or stored.owner_subject_id != principal.subject_id
            or stored.owner_scope_digest != _scope_digest(principal)
        ):
            return _error(correlation, QueryErrorCode.STALE_QUERY_PLAN, "query plan is unavailable for this caller")
        try:
            expires_at = datetime.fromisoformat(stored.plan.expires_at)
        except ValueError:
            return _error(correlation, QueryErrorCode.STALE_QUERY_PLAN, "query plan expiry is invalid")
        if expires_at <= datetime.now(timezone.utc):
            return _error(correlation, QueryErrorCode.STALE_QUERY_PLAN, "query plan has expired")
        if expected_sql_hash is not None and expected_sql_hash != _sql_digest(stored.plan.sql):
            return _error(correlation, QueryErrorCode.STALE_QUERY_PLAN, "query plan SQL hash does not match")
        binding = self._datasets.resolve(dataset_id=stored.plan.dataset_id, space_id=space_id)
        if binding is None or any(
            (
                binding.space_id != space_id,
                binding.dataset_id != stored.plan.dataset_id,
                binding.dataset_version != stored.plan.dataset_version,
                binding.deployment_revision != stored.plan.deployment_revision,
                binding.semantic_context_hash != stored.plan.semantic_context_hash,
                binding.source_revision != stored.source_revision,
                tuple(binding.allowed_tables) != stored.allowed_tables,
            )
        ):
            return _error(correlation, QueryErrorCode.STALE_QUERY_PLAN, "dataset or deployment revision changed")
        try:
            validation = self._validator_for(
                space_id=space_id, semantic_asset_ids=stored.semantic_asset_ids
            ).validate(
                sql=stored.plan.sql, dialect=stored.plan.dialect, allowed_tables=stored.allowed_tables
            )
        except Exception:
            return _error(correlation, QueryErrorCode.QUERY_VALIDATION_FAILED, "query plan validation failed")
        if not isinstance(validation, QueryPlanValidation) or not validation.passed:
            return _error(correlation, QueryErrorCode.QUERY_VALIDATION_FAILED, "query plan guardrails no longer pass")
        try:
            execution = self._executor.execute(
                plan=stored.plan, space_id=space_id, allowed_tables=stored.allowed_tables, page_size=page_size
            )
            if not isinstance(execution, DatabaseExecution):
                raise TypeError("executor returned an invalid result")
            result_id = execution.result_id
            if self._results is not None:
                result_id = self._results.put(result=execution, space_id=space_id, owner_subject_id=principal.subject_id)
        except LookupError:
            return _error(correlation, QueryErrorCode.QUERY_EXECUTION_FAILED, "database execution result is unavailable")
        except Exception:
            return _error(correlation, QueryErrorCode.QUERY_EXECUTION_FAILED, "database read-only execution failed")
        data = {
            "query_plan_id": query_plan_id,
            "sql_hash": _sql_digest(stored.plan.sql),
            "columns": list(execution.columns),
            "rows": [dict(row) for row in execution.rows],
            "row_count": execution.row_count,
            "limited": execution.limited,
        }
        if result_id:
            data["result_id"] = result_id
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="数据库只读查询已完成。",
            data=data,
            evidence=stored.plan.evidence,
            provenance=Provenance(
                space_id=space_id,
                dataset_id=stored.plan.dataset_id,
                dataset_version=stored.plan.dataset_version,
                capability="database_execute_readonly",
                provider_versions={"executor": "platform-readonly"},
            ),
        )
