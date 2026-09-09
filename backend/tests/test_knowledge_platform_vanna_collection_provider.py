from __future__ import annotations

from pathlib import Path

import pytest

from knowledge_contracts import Correlation, Principal
from knowledge_platform.database import (
    DatabaseDatasetBinding,
    DatabaseNl2SqlService,
    GatewayVannaProvider,
    InMemoryQueryPlanRepository,
    LocalVannaCollectionCandidateRebuilder,
    LocalVannaCollectionGateway,
    PostgresReadonlySqlValidator,
    StaticDatabaseDatasetResolver,
)

_DIGEST = "sha256:" + "a" * 64


def _candidate(tmp_path: Path) -> Path:
    root = tmp_path / "local_collection"
    rebuilder = LocalVannaCollectionCandidateRebuilder(collection_root=root, collection_name="local_collection")
    rebuilder.begin(package_revision=_DIGEST, input_digest=_DIGEST)
    rebuilder.add_ddl(source_id="source", item_id="ddl", content="CREATE TABLE sales (amount integer);")
    rebuilder.add_documentation(source_id="source", item_id="doc", content="sales contains revenue amount")
    rebuilder.add_sql_example(
        source_id="source",
        item_id="sql",
        question="sales total",
        sql="SELECT SUM(amount) FROM sales",
    )
    rebuilder.add_entity(
        source_id="source",
        item_id="entity",
        canonical_name="amount",
        entity_type="column",
        table_column="sales.amount",
        aliases=("revenue",),
    )
    rebuilder.commit()
    return root


def _binding() -> DatabaseDatasetBinding:
    return DatabaseDatasetBinding(
        dataset_id="dataset_sales",
        space_id="space_sales",
        dataset_version="local-v1",
        deployment_revision="local-v1",
        dialect="postgresql",
        allowed_tables=("sales",),
        semantic_context_hash=_DIGEST,
        source_revision=_DIGEST,
    )


def test_local_collection_gateway_replays_evidence_and_exact_sql_example(tmp_path: Path) -> None:
    gateway = LocalVannaCollectionGateway(
        _candidate(tmp_path), expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
    )
    provider = GatewayVannaProvider(gateway, version="local-collection-shadow")

    candidate = provider.generate(question="sales total", binding=_binding(), semantic_asset_ids=())

    assert candidate.sql == "SELECT SUM(amount) FROM sales"
    assert {item.matched_by[-1] for item in candidate.evidence} == {"ddl", "documentation", "entities"}
    assert gateway.get_related_entities("revenue") == [
        {
            "aliases": ["revenue"],
            "canonical_name": "amount",
            "entity_type": "column",
            "id": "entity",
            "source_id": "source",
            "table_column": "sales.amount",
        }
    ]


def test_local_collection_gateway_does_not_invent_sql_or_cross_table_scope(tmp_path: Path) -> None:
    gateway = LocalVannaCollectionGateway(
        _candidate(tmp_path), expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
    )

    with pytest.raises(LookupError):
        gateway.generate_sql("sales average", allow_llm_to_see_data=False, table_names=("sales",))
    with pytest.raises(ValueError, match="table allowlist"):
        gateway.generate_sql("sales total", allow_llm_to_see_data=False, table_names=("other",))
    with pytest.raises(ValueError, match="never permits data access"):
        gateway.generate_sql("sales total", allow_llm_to_see_data=True, table_names=("sales",))


def test_local_collection_provider_issues_a_platform_query_plan(tmp_path: Path) -> None:
    binding = _binding()
    service = DatabaseNl2SqlService(
        datasets=StaticDatabaseDatasetResolver({(binding.space_id, binding.dataset_id): binding}),
        provider=GatewayVannaProvider(
            LocalVannaCollectionGateway(
                _candidate(tmp_path), expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
            ), version="local-collection-shadow"
        ),
        validator=PostgresReadonlySqlValidator(),
        plans=InMemoryQueryPlanRepository(),
    )

    result = service.generate(
        principal=Principal(
            subject_id="local-vanna-test",
            scopes=("knowledge.database_nl2sql", "knowledge.space:space_sales"),
        ),
        correlation=Correlation("local-vanna-query-plan"),
        space_id="space_sales",
        dataset_id="dataset_sales",
        question="sales total",
    )

    assert result.status == "ok"
    assert result.data["query_plan"]["validation"] == {
        "readonly": True,
        "allowed_tables": True,
        "guardrails_passed": True,
    }
    assert result.provenance is not None
    assert result.provenance.provider_versions == {"nl2sql": "local-collection-shadow"}


def test_local_collection_gateway_rechecks_digest_and_rejects_symlink(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    gateway = LocalVannaCollectionGateway(
        root, expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
    )
    (root / "ddl.jsonl").write_text("tampered\n", encoding="utf-8")
    with pytest.raises(ValueError, match="digest"):
        gateway.get_related_ddl("sales")

    root = _candidate(tmp_path / "second")
    target = root / "documentation.jsonl"
    target.unlink()
    target.symlink_to(root / "ddl.jsonl")
    with pytest.raises(ValueError, match="file set"):
        LocalVannaCollectionGateway(
            root, expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
        )


def test_local_collection_gateway_rejects_identity_mismatch(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    with pytest.raises(ValueError, match="identity"):
        LocalVannaCollectionGateway(
            root, expected_collection_name="another_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
        )

    manifest = root / "collection-manifest.json"
    manifest.write_text(manifest.read_text(encoding="utf-8").replace('"collection_name": "local_collection"', '"collection_name": "other_collection"'), encoding="utf-8")
    with pytest.raises(ValueError, match="identity"):
        LocalVannaCollectionGateway(
            root, expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
        )

    root = _candidate(tmp_path / "revision")
    with pytest.raises(ValueError, match="package revision"):
        LocalVannaCollectionGateway(
            root, expected_collection_name="local_collection", expected_package_revision="sha256:" + "b" * 64, expected_input_digest=_DIGEST
        )

    root = _candidate(tmp_path / "input")
    manifest = root / "collection-manifest.json"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace(f'"input_digest": "{_DIGEST}"', f'"input_digest": "sha256:{"c" * 64}"'),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="input digest"):
        LocalVannaCollectionGateway(
            root, expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
        )


def test_local_collection_gateway_rejects_count_drift(tmp_path: Path) -> None:
    root = _candidate(tmp_path)
    manifest = root / "collection-manifest.json"
    document = manifest.read_text(encoding="utf-8").replace('"ddl": 1', '"ddl": 2')
    manifest.write_text(document, encoding="utf-8")
    gateway = LocalVannaCollectionGateway(
        root, expected_collection_name="local_collection", expected_package_revision=_DIGEST, expected_input_digest=_DIGEST
    )
    with pytest.raises(ValueError, match="count"):
        gateway.get_related_ddl("sales")
