from __future__ import annotations

import hashlib

import pytest

from knowledge_contracts import Correlation, Principal
from knowledge_platform.database import (
    InMemorySqlGuardrailRepository,
    PlatformSqlGuardrailValidator,
    SqlGuardrailAdminService,
    SqlGuardrailRule,
    SqliteSqlGuardrailRepository,
)


def _principal(*scopes: str, subject_id: str = "admin") -> Principal:
    return Principal(subject_id=subject_id, scopes=tuple(scopes))


def _rule(**overrides: object) -> SqlGuardrailRule:
    values: dict[str, object] = {
        "id": "require_sales_year",
        "space_id": "space_sales",
        "name": "Sales must use year",
        "rule_type": "require_sql_contains",
        "scope": {"table_scope": {"mode": "any", "values": ["sales"]}},
        "params": {"contains": "launch_year"},
        "action": "block",
        "message": "The sales query must retain the year grain.",
    }
    values.update(overrides)
    return SqlGuardrailRule(**values)


def test_rule_is_normalized_and_content_addressed() -> None:
    first = _rule()
    second = _rule()
    assert first.definition() == second.definition()
    assert first.definition_digest == second.definition_digest
    assert first.definition_digest.startswith("sha256:")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("rule_type", "unknown", "unsupported"),
        ("params", {"contains": "/etc/passwd"}, "unsafe"),
        ("params", {"contains": "launch_year", "unknown": "value"}, "unsupported"),
        ("scope", {"table_scope": {"values": ["sales"], "unknown": True}}, "unsupported"),
        ("scope", {"semantic_assets": ["secret_asset"]}, "unsafe"),
        ("id", "bad/id", "invalid"),
    ],
)
def test_rule_rejects_unsafe_or_unknown_definition(field: str, value: object, message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _rule(**{field: value})


def test_rule_rejects_invalid_regular_expression() -> None:
    with pytest.raises(ValueError, match="regular expression"):
        _rule(rule_type="forbid_sql_pattern", params={"pattern": "["})


def test_admin_create_requires_scope_and_does_not_activate() -> None:
    service = SqlGuardrailAdminService(repository=InMemorySqlGuardrailRepository())
    denied = service.create(
        principal=_principal("knowledge:space:space_sales"),
        correlation=Correlation("guardrail-denied"),
        rule=_rule(),
    )
    assert denied.status == "error"
    assert denied.error.code == "permission_denied"

    created = service.create(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("guardrail-create"),
        rule=_rule(),
    )
    assert created.status == "ok"
    assert created.data["guardrail"]["status"] == "waiting_for_confirmation"
    assert service.list_active(space_id="space_sales") == ()


def test_admin_decision_is_explicit_idempotent_and_state_fenced() -> None:
    repository = InMemorySqlGuardrailRepository()
    service = SqlGuardrailAdminService(repository=repository)
    principal = _principal("knowledge:admin", "knowledge:space:space_sales")
    created = service.create(
        principal=principal,
        correlation=Correlation("guardrail-create-2"),
        rule=_rule(),
    )
    assert created.status == "ok"

    confirmed = service.decide(
        principal=principal,
        correlation=Correlation("guardrail-confirm"),
        guardrail_id="require_sales_year",
        space_id="space_sales",
        decision="confirm",
        expected_status="waiting_for_confirmation",
    )
    assert confirmed.status == "ok"
    assert confirmed.data["guardrail"]["status"] == "active"
    repeated = service.decide(
        principal=principal,
        correlation=Correlation("guardrail-confirm-retry"),
        guardrail_id="require_sales_year",
        space_id="space_sales",
        decision="confirm",
        expected_status="waiting_for_confirmation",
    )
    assert repeated.status == "ok"
    assert repeated.data["guardrail"]["decision_digest"] == confirmed.data["guardrail"]["decision_digest"]

    conflict = service.decide(
        principal=principal,
        correlation=Correlation("guardrail-reject-after-confirm"),
        guardrail_id="require_sales_year",
        space_id="space_sales",
        decision="reject",
        expected_status="waiting_for_confirmation",
    )
    assert conflict.status == "error"
    assert conflict.error.code == "invalid_request"


def test_sqlite_repository_survives_restart_and_keeps_definition_digest(tmp_path) -> None:
    database = tmp_path / "guardrails.sqlite3"
    actor_digest = "sha256:" + hashlib.sha256(b"admin").hexdigest()
    rule = _rule()
    from knowledge_platform.database.guardrails import SqlGuardrailRecord

    pending = SqlGuardrailRecord(
        rule=rule,
        status="waiting_for_confirmation",
        definition_digest=rule.definition_digest,
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
        actor_digest=actor_digest,
    )
    first = SqliteSqlGuardrailRepository(database)
    assert first.create(record=pending).public()["status"] == "waiting_for_confirmation"
    second = SqliteSqlGuardrailRepository(database)
    confirmed = second.decide(
        guardrail_id=rule.id,
        space_id=rule.space_id,
        decision="confirm",
        expected_status="waiting_for_confirmation",
        actor_digest=actor_digest,
        decision_digest="sha256:" + hashlib.sha256(b"confirm").hexdigest(),
    )
    assert confirmed.status == "active"
    restored = SqliteSqlGuardrailRepository(database).list(space_id=rule.space_id, status="active")
    assert len(restored) == 1
    assert restored[0].definition_digest == rule.definition_digest


class _PassingSqlValidator:
    def validate(self, *, sql: str, dialect: str, allowed_tables: tuple[str, ...]):
        del sql, dialect, allowed_tables
        from knowledge_contracts import QueryPlanValidation

        return QueryPlanValidation(readonly=True, allowed_tables=True, guardrails_passed=True)


class _InvalidSqlValidator:
    def validate(self, **kwargs: object):
        del kwargs
        return {"passed": True}


def _active_repository(rule: SqlGuardrailRule) -> InMemorySqlGuardrailRepository:
    repository = InMemorySqlGuardrailRepository()
    service = SqlGuardrailAdminService(repository=repository)
    principal = _principal("knowledge:admin", f"knowledge:space:{rule.space_id}")
    assert service.create(principal=principal, correlation=Correlation("active-create"), rule=rule).status == "ok"
    assert (
        service.decide(
            principal=principal,
            correlation=Correlation("active-confirm"),
            guardrail_id=rule.id,
            space_id=rule.space_id,
            decision="confirm",
            expected_status="waiting_for_confirmation",
        ).status
        == "ok"
    )
    return repository


def test_platform_validator_consumes_only_active_rules_and_preserves_readonly_base() -> None:
    repository = _active_repository(_rule())
    validator = PlatformSqlGuardrailValidator(
        base_validator=_PassingSqlValidator(),
        repository=repository,
        space_id="space_sales",
    )
    blocked = validator.validate(
        sql="SELECT amount FROM sales",
        dialect="postgresql",
        allowed_tables=("sales",),
    )
    allowed = validator.validate(
        sql="SELECT launch_year, SUM(amount) FROM sales GROUP BY launch_year",
        dialect="postgresql",
        allowed_tables=("sales",),
    )
    assert blocked.guardrails_passed is False
    assert allowed.passed is True


def test_platform_validator_fail_closes_when_scoped_semantics_are_missing() -> None:
    rule = _rule(
        id="semantic_only",
        scope={"semantic_assets": ["measure:revenue"]},
        params={"contains": "launch_year"},
    )
    repository = _active_repository(rule)
    strict = PlatformSqlGuardrailValidator(
        base_validator=_PassingSqlValidator(),
        repository=repository,
        space_id="space_sales",
        strict_context=True,
    )
    relaxed = PlatformSqlGuardrailValidator(
        base_validator=_PassingSqlValidator(),
        repository=repository,
        space_id="space_sales",
        strict_context=False,
    )
    kwargs = {"sql": "SELECT amount FROM sales", "dialect": "postgresql", "allowed_tables": ("sales",)}
    assert strict.validate(**kwargs).passed is False
    assert relaxed.validate(**kwargs).passed is True


def test_platform_validator_fail_closes_on_invalid_base_validator_result() -> None:
    validator = PlatformSqlGuardrailValidator(
        base_validator=_InvalidSqlValidator(),
        repository=InMemorySqlGuardrailRepository(),
        space_id="space_sales",
    )
    result = validator.validate(sql="SELECT 1", dialect="postgresql", allowed_tables=("sales",))
    assert result.passed is False
