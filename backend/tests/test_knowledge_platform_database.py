from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

import pytest

from knowledge_contracts import Correlation, Evidence, Principal, QueryPlan, QueryPlanValidation
from knowledge_platform.database import (
    DatabaseDatasetBinding,
    DatabaseEvidenceRecord,
    DatabaseExecuteReadonlyService,
    DatabaseExecution,
    DatabaseNl2SqlService,
    DatabaseSchemaEvidence,
    DatabaseSqlCandidate,
    GatewayVannaProvider,
    InMemoryDatabaseEvidenceRepository,
    InMemoryDatabaseSchemaEvidenceRepository,
    InMemoryQueryPlanRepository,
    InMemoryQueryResultRepository,
    InMemorySqlGuardrailRepository,
    LegacyEvidenceMigrationService,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSource,
    LocalSqliteDatabaseDatasetResolver,
    LocalSqliteDatabaseSource,
    PostgresReadonlySqlValidator,
    SqlGuardrailAdminService,
    SqlGuardrailRule,
    SqliteDatabaseEvidenceRepository,
    SqliteDatabaseQueryRepository,
    SqliteDatabaseSchemaEvidenceRepository,
    SqliteReadonlyDatabaseExecutor,
    SqliteReadonlySqlValidator,
    StaticDatabaseDatasetResolver,
    StaticNl2SqlProvider,
)
from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter


def _digest(seed: str) -> str:
    return "sha256:" + seed * 64


class _Validator:
    def __init__(self, passed: bool = True) -> None:
        self.passed = passed

    def validate(self, *, sql: str, dialect: str, allowed_tables: Sequence[str]) -> QueryPlanValidation:
        assert sql and dialect and allowed_tables
        return QueryPlanValidation(self.passed, self.passed, self.passed)


class _Executor:
    def execute(self, *, plan: QueryPlan, space_id: str, allowed_tables: Sequence[str], page_size: int) -> DatabaseExecution:
        del space_id
        assert plan.sql and allowed_tables
        rows = ({"channel": "online", "amount": 10}, {"channel": "store", "amount": 8})
        return DatabaseExecution(columns=("channel", "amount"), rows=rows[:page_size], row_count=2)


class _Gateway:
    def __init__(self, *, unsafe: bool = False) -> None:
        self.unsafe = unsafe
        self.kwargs = None

    def get_related_ddl(self, question: str):
        assert question
        return ["CREATE TABLE sales (amount integer)"]

    def get_related_documentation(self, question: str):
        return [{"content": "Sales amount is gross revenue."}]

    def get_related_entities(self, question: str):
        return [{"canonical_name": "/etc/passwd" if self.unsafe else "online", "entity_type": "channel"}]

    def generate_sql(self, question: str, **kwargs: object) -> str:
        self.kwargs = kwargs
        return "SELECT channel, SUM(amount) FROM sales GROUP BY channel"


def _services(*, validator: _Validator | None = None, guardrails=None):
    binding = DatabaseDatasetBinding(
        dataset_id="dataset_sales",
        space_id="space_sales",
        dataset_version="1.2.0",
        deployment_revision="deploy-1",
        dialect="postgresql",
        allowed_tables=("sales",),
        semantic_context_hash=_digest("a"),
        source_revision=_digest("b"),
        provider_version="vanna-local-shadow",
    )
    datasets = StaticDatabaseDatasetResolver({("space_sales", "dataset_sales"): binding})
    plans = InMemoryQueryPlanRepository()
    evidence = Evidence(
        asset_id="schema_sales",
        resource_uri="knowledge://spaces/space_sales/assets/schema_sales",
        locator={"section": "ddl"},
        quote="sales table schema",
        revision=_digest("c"),
        matched_by=("schema",),
    )
    nl2sql = DatabaseNl2SqlService(
        datasets=datasets,
        provider=StaticNl2SqlProvider(
            {
                "last year revenue": DatabaseSqlCandidate(
                    sql="SELECT channel, SUM(amount) FROM sales GROUP BY channel",
                    evidence=(evidence,),
                    provider_version="vanna-local-shadow",
                )
            }
        ),
        validator=validator or _Validator(),
        plans=plans,
        guardrails=guardrails,
    )
    execute = DatabaseExecuteReadonlyService(
        datasets=datasets,
        plans=plans,
        validator=validator or _Validator(),
        executor=_Executor(),
        results=InMemoryQueryResultRepository(),
        guardrails=guardrails,
    )
    return nl2sql, execute, datasets


def _activate_guardrail(repository: InMemorySqlGuardrailRepository, *, rule_id: str, rule_type: str, params: dict[str, object]) -> None:
    service = SqlGuardrailAdminService(repository=repository)
    admin = Principal(subject_id="guardrail-admin", scopes=("knowledge:admin", "knowledge:space:space_sales"))
    rule = SqlGuardrailRule(id=rule_id, space_id="space_sales", name=rule_id, rule_type=rule_type, scope={}, params=params, action="block")
    assert service.create(principal=admin, correlation=Correlation(f"create-{rule_id}"), rule=rule).status == "ok"
    assert service.decide(
        principal=admin,
        correlation=Correlation(f"confirm-{rule_id}"),
        guardrail_id=rule_id,
        space_id="space_sales",
        decision="confirm",
        expected_status="waiting_for_confirmation",
    ).status == "ok"


def test_database_services_formally_revalidate_active_platform_guardrails() -> None:
    repository = InMemorySqlGuardrailRepository()
    _activate_guardrail(repository, rule_id="require_channel", rule_type="require_sql_contains", params={"contains": "channel"})
    generate, execute, _ = _services(guardrails=repository)
    generated = generate.generate(
        principal=_principal(), correlation=Correlation("guarded-generate"), space_id="space_sales", dataset_id="dataset_sales", question="last year revenue"
    )
    assert generated.status == "ok"

    _activate_guardrail(repository, rule_id="forbid_sum", rule_type="forbid_sql_pattern", params={"pattern": r"SUM\("})
    executed = execute.execute(
        principal=_principal(), correlation=Correlation("guarded-execute"), space_id="space_sales", query_plan_id=generated.data["query_plan"]["query_plan_id"]
    )
    assert executed.status == "error"
    assert executed.error.code == "query_validation_failed"


def _principal(subject: str = "alice") -> Principal:
    return Principal(
        subject_id=subject,
        scopes=("database_nl2sql", "database_execute_readonly", "knowledge:space:space_sales"),
    )


def test_database_query_uses_two_phase_owner_bound_plan() -> None:
    generate, execute, _ = _services()
    generated = generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-generate"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    assert generated.status == "ok"
    plan = generated.data["query_plan"]
    assert plan["query_plan_id"].startswith("qp_")
    assert generated.provenance is not None
    assert generated.provenance.capability == "database_nl2sql"

    completed = execute.execute(
        principal=_principal(),
        correlation=Correlation("trace-db-execute"),
        space_id="space_sales",
        query_plan_id=plan["query_plan_id"],
        page_size=1,
        expected_sql_hash=generated.data["sql_hash"],
    )
    assert completed.status == "ok"
    assert completed.to_dict()["data"]["rows"] == [{"channel": "online", "amount": 10}]
    assert completed.data["row_count"] == 2
    assert completed.provenance is not None
    assert completed.provenance.capability == "database_execute_readonly"


def test_injected_vanna_provider_is_explicit_and_emits_portable_evidence() -> None:
    binding = _services()[2]._bindings[("space_sales", "dataset_sales")]
    gateway = _Gateway()
    candidate = GatewayVannaProvider(gateway, version="vanna-test").generate(
        question="last year revenue",
        binding=binding,
        semantic_asset_ids=("measure:revenue",),
    )
    assert len(candidate.evidence) == 3
    assert candidate.evidence[0].resource_uri.startswith("knowledge://spaces/space_sales/")
    assert gateway.kwargs["allow_llm_to_see_data"] is False
    assert gateway.kwargs["table_names"] == ("sales",)

    with pytest.raises(ValueError, match="unsafe content"):
        GatewayVannaProvider(_Gateway(unsafe=True)).generate(
            question="last year revenue", binding=binding, semantic_asset_ids=()
        )


def test_local_sqlite_executor_reads_only_explicit_space_dataset_binding(tmp_path) -> None:
    import sqlite3

    from knowledge_platform.database import SqliteReadonlyDatabaseExecutor, SqliteReadonlySqlValidator

    database = tmp_path / "sales.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (channel TEXT, amount INTEGER)")
        connection.executemany("INSERT INTO sales VALUES (?, ?)", [("online", 10), ("store", 8)])
        connection.commit()
    generate, _, datasets = _services()
    plans = InMemoryQueryPlanRepository()
    generated = DatabaseNl2SqlService(
        datasets=datasets,
        provider=generate._provider,
        validator=_Validator(),
        plans=plans,
    ).generate(
        principal=_principal(),
        correlation=Correlation("trace-local-sqlite-generate"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    execute = DatabaseExecuteReadonlyService(
        datasets=datasets,
        plans=plans,
        validator=_Validator(),
        executor=SqliteReadonlyDatabaseExecutor(
            {("space_sales", "dataset_sales"): database}, validator=SqliteReadonlySqlValidator()
        ),
    )
    result = execute.execute(
        principal=_principal(),
        correlation=Correlation("trace-local-sqlite-execute"),
        space_id="space_sales",
        query_plan_id=generated.data["query_plan"]["query_plan_id"],
        page_size=1,
    )
    assert result.status == "ok"
    assert result.to_dict()["data"]["row_count"] == 2
    assert result.to_dict()["data"]["rows"][0]["channel"] == "online"

    with pytest.raises(PermissionError):
        SqliteReadonlyDatabaseExecutor(
            {("space_sales", "dataset_sales"): database}
        ).execute(
            plan=QueryPlan(
                query_plan_id="qp_direct_bad",
                sql="DROP TABLE sales",
                dialect="sqlite",
                dataset_id="dataset_sales",
                dataset_version="1",
                deployment_revision="deploy-1",
                semantic_context_hash=_digest("a"),
                validation=QueryPlanValidation(True, True, True),
                expires_at="2099-01-01T00:00:00+00:00",
            ),
            space_id="space_sales",
            allowed_tables=("sales",),
            page_size=1,
        )


def test_local_sqlite_executor_rejects_stale_source_revision(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (amount INTEGER)")
        connection.execute("INSERT INTO sales VALUES (10)")
    expected_revision = SqliteReadonlyDatabaseExecutor.file_digest(database)
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sales VALUES (20)")
    plan = QueryPlan(
        query_plan_id="qp_stale_source",
        sql="SELECT amount FROM sales",
        dialect="sqlite",
        dataset_id="dataset_sales",
        dataset_version="1",
        deployment_revision="deploy-1",
        semantic_context_hash=_digest("a"),
        validation=QueryPlanValidation(True, True, True),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    with pytest.raises(LookupError, match="source revision is stale"):
        SqliteReadonlyDatabaseExecutor(
            {("space_sales", "dataset_sales"): database},
            validator=SqliteReadonlySqlValidator(),
            source_revisions={("space_sales", "dataset_sales"): expected_revision},
        ).execute(plan=plan, space_id="space_sales", allowed_tables=("sales",), page_size=5)


def test_local_sqlite_dataset_resolver_binds_current_schema_without_exposing_path(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (channel TEXT, amount INTEGER)")
        connection.execute("INSERT INTO sales VALUES ('online', 10)")
        connection.commit()

    resolver = LocalSqliteDatabaseDatasetResolver(
        [
            LocalSqliteDatabaseSource(
                dataset_id="dataset_sales",
                space_id="space_sales",
                path=database,
                allowed_tables=("sales",),
                dataset_version="local-sqlite-v1",
                deployment_revision="local-shadow-v1",
                semantic_context_hash=_digest("a"),
            )
        ]
    )
    binding = resolver.resolve(dataset_id="dataset_sales", space_id="space_sales")
    assert binding is not None
    assert binding.dialect == "sqlite"
    assert binding.source_revision == SqliteReadonlyDatabaseExecutor.file_digest(database)
    assert not hasattr(binding, "path")

    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO sales VALUES ('store', 8)")
        connection.commit()
    refreshed = resolver.resolve(dataset_id="dataset_sales", space_id="space_sales")
    assert refreshed is not None
    assert refreshed.source_revision != binding.source_revision

    with pytest.raises(LookupError, match="table binding is unavailable"):
        LocalSqliteDatabaseDatasetResolver(
            [
                LocalSqliteDatabaseSource(
                    dataset_id="dataset_missing",
                    space_id="space_sales",
                    path=database,
                    allowed_tables=("missing",),
                    dataset_version="local-sqlite-v1",
                    deployment_revision="local-shadow-v1",
                    semantic_context_hash=_digest("a"),
                )
            ]
        ).resolve(dataset_id="dataset_missing", space_id="space_sales")


def test_local_sqlite_dataset_resolver_rejects_secret_schema(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "secret.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE credentials (username TEXT, password TEXT)")
        connection.commit()
    resolver = LocalSqliteDatabaseDatasetResolver(
        [
            LocalSqliteDatabaseSource(
                dataset_id="dataset_credentials",
                space_id="space_sales",
                path=database,
                allowed_tables=("credentials",),
                dataset_version="local-sqlite-v1",
                deployment_revision="local-shadow-v1",
                semantic_context_hash=_digest("a"),
            )
        ]
    )
    with pytest.raises(PermissionError, match="secret-bearing column"):
        resolver.resolve(dataset_id="dataset_credentials", space_id="space_sales")


def test_local_sqlite_dataset_resolver_rejects_database_file_symlink(tmp_path: Path) -> None:
    import sqlite3

    database = tmp_path / "source.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.execute("CREATE TABLE sales (amount INTEGER)")
        connection.commit()
    alias = tmp_path / "alias.sqlite3"
    try:
        alias.symlink_to(database)
    except OSError:
        pytest.skip("filesystem does not support symlinks")
    resolver = LocalSqliteDatabaseDatasetResolver(
        [
            LocalSqliteDatabaseSource(
                dataset_id="dataset_sales",
                space_id="space_sales",
                path=alias,
                allowed_tables=("sales",),
                dataset_version="local-sqlite-v1",
                deployment_revision="local-shadow-v1",
                semantic_context_hash=_digest("a"),
            )
        ]
    )
    with pytest.raises(LookupError, match="binding is unavailable"):
        resolver.resolve(dataset_id="dataset_sales", space_id="space_sales")


def test_local_postgres_source_is_loopback_only_and_validator_is_database_scoped() -> None:
    source = LocalPostgresDatabaseSource(
        dataset_id="dataset_vehicle",
        space_id="space_sales",
        host="127.0.0.1",
        port=5432,
        database="insight_data",
        username="pet",
        allowed_tables=("vehicle_model_base",),
        dataset_version="local-postgres-v1",
        deployment_revision="local-shadow-v1",
        semantic_context_hash=_digest("a"),
    )
    resolver = LocalPostgresDatabaseDatasetResolver([source])
    assert resolver.resolve.__name__ == "resolve"
    assert not hasattr(source, "to_dict")

    with pytest.raises(ValueError, match="loopback host"):
        LocalPostgresDatabaseSource(
            dataset_id="dataset_vehicle",
            space_id="space_sales",
            host="db.example.com",
            port=5432,
            database="insight_data",
            username="pet",
            allowed_tables=("vehicle_model_base",),
            dataset_version="local-postgres-v1",
            deployment_revision="local-shadow-v1",
            semantic_context_hash=_digest("a"),
        )

    validator = PostgresReadonlySqlValidator()
    valid = validator.validate(
        sql="SELECT energy_type, COUNT(*) FROM vehicle_model_base GROUP BY energy_type",
        dialect="postgresql",
        allowed_tables=("vehicle_model_base",),
    )
    assert valid.passed
    assert not validator.validate(
        sql="SELECT * FROM other_table",
        dialect="postgresql",
        allowed_tables=("vehicle_model_base",),
    ).passed
    assert not validator.validate(
        sql="SELECT * FROM public.vehicle_model_base; DROP TABLE vehicle_model_base",
        dialect="postgresql",
        allowed_tables=("vehicle_model_base",),
    ).passed
    assert not validator.validate(
        sql="SELECT pg_read_file('/etc/passwd') FROM vehicle_model_base",
        dialect="postgresql",
        allowed_tables=("vehicle_model_base",),
    ).passed


def test_legacy_evidence_migration_strips_receipt_identity_and_is_idempotent() -> None:
    class Reader:
        def list_database_evidence(self):
            return [
                {
                    "session_id": "legacy-session",
                    "run_id": "legacy-run",
                    "evidence": [
                        {
                            "asset_id": "schema_sales",
                            "resource_uri": "knowledge://spaces/space_sales/assets/schema_sales",
                            "locator": {"section": "ddl"},
                            "quote": "sales table",
                            "matched_by": ["legacy"],
                        }
                    ],
                }
            ]

        def list_schema_evidence(self):
            return [{"table_name": "sales", "columns": ["channel", "amount"]}]

    evidence_repo = InMemoryDatabaseEvidenceRepository()
    schema_repo = InMemoryDatabaseSchemaEvidenceRepository()
    service = LegacyEvidenceMigrationService(evidence=evidence_repo, schema=schema_repo)
    principal = _principal()
    principal = Principal(
        subject_id=principal.subject_id,
        scopes=("knowledge:admin", "knowledge:space:space_sales"),
    )
    first = service.migrate(
        principal=principal,
        reader=Reader(),
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    )
    assert first == type(first)(evidence_written=1, schema_evidence_written=1, skipped_existing=0)
    second = service.migrate(
        principal=principal,
        reader=Reader(),
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    )
    assert second == type(second)(evidence_written=0, schema_evidence_written=0, skipped_existing=2)
    stored = evidence_repo.get(
        evidence_id=next(iter(evidence_repo._items)),
        owner_subject_id="alice",
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    )
    assert stored is not None
    assert not hasattr(stored, "session_id")
    with pytest.raises(PermissionError):
        service.migrate(
            principal=Principal(subject_id="tenant", tenant_id="t1", scopes=("knowledge:admin",)),
            reader=Reader(),
            space_id="space_sales",
            dataset_id="dataset_sales",
            source_revision=_digest("b"),
            allowed_tables=("sales",),
        )


def test_database_query_rejects_tenant_owner_and_sql_hash_mismatch() -> None:
    generate, execute, _ = _services()
    denied = generate.generate(
        principal=Principal(
            subject_id="tenant-user",
            tenant_id="tenant-1",
            scopes=("database_nl2sql", "knowledge:space:space_sales"),
        ),
        correlation=Correlation("trace-db-denied"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    assert denied.error is not None
    assert denied.error.code.value == "permission_denied"

    generated = generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-generate-2"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    plan_id = generated.data["query_plan"]["query_plan_id"]
    stale = execute.execute(
        principal=_principal(),
        correlation=Correlation("trace-db-stale-hash"),
        space_id="space_sales",
        query_plan_id=plan_id,
        expected_sql_hash=_digest("z"),
    )
    assert stale.error is not None
    assert stale.error.code.value == "stale_query_plan"


def test_database_query_rejects_failed_guardrail_and_dataset_revision_change() -> None:
    generate, execute, datasets = _services(validator=_Validator(False))
    rejected = generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-guardrail"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    assert rejected.error is not None
    assert rejected.error.code.value == "query_validation_failed"

    generate, execute, datasets = _services()
    generated = generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-revision-generate"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    plan_id = generated.data["query_plan"]["query_plan_id"]
    current = datasets._bindings[("space_sales", "dataset_sales")]
    datasets._bindings[("space_sales", "dataset_sales")] = DatabaseDatasetBinding(
        dataset_id=current.dataset_id,
        space_id=current.space_id,
        dataset_version="1.3.0",
        deployment_revision=current.deployment_revision,
        dialect=current.dialect,
        allowed_tables=current.allowed_tables,
        semantic_context_hash=current.semantic_context_hash,
        source_revision=current.source_revision,
    )
    stale = execute.execute(
        principal=_principal(),
        correlation=Correlation("trace-db-revision-execute"),
        space_id="space_sales",
        query_plan_id=plan_id,
    )
    assert stale.error is not None
    assert stale.error.code.value == "stale_query_plan"


def test_database_query_rest_and_mcp_adapters_share_the_same_services() -> None:
    generate, execute, _ = _services()
    rest = RestQueryAdapter(
        catalog=object(),
        search=object(),
        asset_read=object(),
        document=object(),
        wiki=object(),
        database_nl2sql=generate,
        database_execute=execute,
    )
    import asyncio

    generated = asyncio.run(
        rest.handle(
            method="POST",
            path="/v1/database/nl2sql",
            principal=_principal(),
            correlation=Correlation("trace-db-rest"),
            body={"space_id": "space_sales", "dataset_id": "dataset_sales", "question": "last year revenue"},
        )
    )
    assert generated["status"] == "ok"
    plan_id = generated["data"]["query_plan"]["query_plan_id"]
    mcp = McpQueryAdapter(rest)
    completed = asyncio.run(
        mcp.call_tool(
            name="database_execute_readonly",
            arguments={"space_id": "space_sales", "query_plan_id": plan_id, "page_size": 1},
            principal=_principal(),
            correlation=Correlation("trace-db-mcp"),
        )
    )
    assert completed["structuredContent"]["status"] == "ok"
    assert {item["name"] for item in mcp.tool_descriptors()} >= {
        "database_nl2sql",
        "database_execute_readonly",
    }


def test_database_plan_requires_current_scope_snapshot_and_evidence_repo_is_not_session_bound() -> None:
    generate, execute, _ = _services()
    generated = generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-scope-generate"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    plan_id = generated.data["query_plan"]["query_plan_id"]
    changed_scope = Principal(
        subject_id="alice",
        scopes=("database_nl2sql", "database_execute_readonly", "knowledge:space:space_sales", "new-scope"),
    )
    stale = execute.execute(
        principal=changed_scope,
        correlation=Correlation("trace-db-scope-execute"),
        space_id="space_sales",
        query_plan_id=plan_id,
    )
    assert stale.error is not None
    assert stale.error.code.value == "stale_query_plan"

    evidence = Evidence(
        asset_id="schema_sales",
        resource_uri="knowledge://spaces/space_sales/assets/schema_sales",
        locator={"section": "ddl"},
        revision=_digest("c"),
    )
    record = DatabaseEvidenceRecord(
        evidence_id="evidence_sales",
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
        evidence=(evidence,),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    repo = InMemoryDatabaseEvidenceRepository()
    repo.put(record=record)
    assert repo.get(
        evidence_id="evidence_sales",
        owner_subject_id="alice",
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    ) == record
    assert repo.get(
        evidence_id="evidence_sales",
        owner_subject_id="bob",
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    ) is None

    expired = DatabaseEvidenceRecord(
        evidence_id="evidence_expired",
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
        evidence=(evidence,),
        expires_at="2000-01-01T00:00:00+00:00",
    )
    repo.put(record=expired)
    assert repo.get(
        evidence_id="evidence_expired",
        owner_subject_id="alice",
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    ) is None


def test_database_result_and_schema_evidence_reject_secret_bearing_content() -> None:
    import pytest

    with pytest.raises(ValueError, match="secret-bearing column"):
        DatabaseExecution(columns=("password",), rows=({"password": "x"},), row_count=1)
    with pytest.raises(ValueError, match="secret value"):
        DatabaseExecution(columns=("value",), rows=({"value": "token=abc"},), row_count=1)
    with pytest.raises(ValueError, match="secret-bearing column"):
        DatabaseSchemaEvidence(
            space_id="space_sales",
            dataset_id="dataset_sales",
            owner_subject_id="alice",
            table_name="sales",
            columns=("api_key",),
            schema_revision=_digest("d"),
        )

    schema = DatabaseSchemaEvidence(
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        table_name="sales",
        columns=("channel",),
        schema_revision=_digest("d"),
    )
    schema_repo = InMemoryDatabaseSchemaEvidenceRepository()
    schema_repo.put(record=schema)
    assert schema_repo.get(
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        table_name="SALES",
        schema_revision=_digest("d"),
    ) == schema


def test_sqlite_database_repository_persists_plan_evidence_schema_and_result(tmp_path) -> None:
    import sqlite3

    database = tmp_path / "platform.sqlite3"
    with sqlite3.connect(database):
        pass
    repository = SqliteDatabaseQueryRepository(database)
    generate, _, datasets = _services()
    # Reuse the deterministic provider/validator, but persist the generated plan
    # in the explicit Platform database rather than the in-memory test store.
    persistent_generate = DatabaseNl2SqlService(
        datasets=datasets,
        provider=generate._provider,
        validator=generate._validator,
        plans=repository,
    )
    generated = persistent_generate.generate(
        principal=_principal(),
        correlation=Correlation("trace-db-sqlite-generate"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="last year revenue",
    )
    assert generated.status == "ok"
    plan_id = generated.data["query_plan"]["query_plan_id"]
    assert SqliteDatabaseQueryRepository(database).get(query_plan_id=plan_id) is not None

    evidence = Evidence(
        asset_id="schema_sales",
        resource_uri="knowledge://spaces/space_sales/assets/schema_sales",
        locator={"section": "ddl"},
        revision=_digest("c"),
    )
    record = DatabaseEvidenceRecord(
        evidence_id="evidence_persisted",
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
        evidence=(evidence,),
        expires_at="2099-01-01T00:00:00+00:00",
    )
    repository.put_evidence(record=record)
    assert SqliteDatabaseQueryRepository(database).get_evidence(
        evidence_id="evidence_persisted",
        owner_subject_id="alice",
        space_id="space_sales",
        dataset_id="dataset_sales",
        source_revision=_digest("b"),
        allowed_tables=("sales",),
    ) == record
    schema = DatabaseSchemaEvidence(
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        table_name="sales",
        columns=("channel",),
        schema_revision=_digest("d"),
    )
    repository.put_schema_evidence(record=schema)
    assert SqliteDatabaseQueryRepository(database).get_schema_evidence(
        space_id="space_sales",
        dataset_id="dataset_sales",
        owner_subject_id="alice",
        table_name="SALES",
        schema_revision=_digest("d"),
    ) == schema
    result_id = repository.put_result(
        result=DatabaseExecution(columns=("channel",), rows=({"channel": "online"},), row_count=1),
        space_id="space_sales",
        owner_subject_id="alice",
    )
    assert result_id.startswith("result_")


def test_sqlite_evidence_facades_conform_to_legacy_migration_ports(tmp_path: Path) -> None:
    database = tmp_path / "evidence.sqlite3"
    database.touch()

    class Reader:
        def list_database_evidence(self):
            return [{"evidence": [{"asset_id": "schema_sales", "resource_uri": "knowledge://spaces/space_sales/assets/schema_sales", "locator": {"section": "ddl"}, "quote": "sales", "matched_by": ["legacy"]}]}]

        def list_schema_evidence(self):
            return [{"table_name": "sales", "columns": ["channel", "amount"]}]

    principal = Principal(subject_id="alice", scopes=("knowledge:admin", "knowledge:space:space_sales"))
    service = LegacyEvidenceMigrationService(
        evidence=SqliteDatabaseEvidenceRepository(database),
        schema=SqliteDatabaseSchemaEvidenceRepository(database),
    )
    first = service.migrate(principal=principal, reader=Reader(), space_id="space_sales", dataset_id="dataset_sales", source_revision=_digest("b"), allowed_tables=("sales",))
    second = LegacyEvidenceMigrationService(
        evidence=SqliteDatabaseEvidenceRepository(database),
        schema=SqliteDatabaseSchemaEvidenceRepository(database),
    ).migrate(principal=principal, reader=Reader(), space_id="space_sales", dataset_id="dataset_sales", source_revision=_digest("b"), allowed_tables=("sales",))
    assert first == type(first)(evidence_written=1, schema_evidence_written=1, skipped_existing=0)
    assert second == type(second)(evidence_written=0, schema_evidence_written=0, skipped_existing=2)
