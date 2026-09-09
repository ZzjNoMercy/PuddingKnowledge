from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest


class _Catalog:
    def __init__(self, asset: dict[str, object], related_assets: tuple[dict[str, object], ...] = ()) -> None:
        self.catalog_revision = "sha256:" + "a" * 64
        self.asset = asset
        self.assets = {str(item["id"]): item for item in (asset, *related_assets)}

    def get_asset(self, *, asset_id: str):
        return self.asset if asset_id == self.asset["id"] else None

    def get_structured_asset(self, *, asset_id: str):
        return self.assets.get(asset_id)


class _Provider:
    def __init__(self, payloads):
        self.payloads = tuple(payloads)

    async def query(self, **_kwargs):
        return self.payloads


class _SemanticRegistry:
    def __init__(self, context) -> None:
        self.context = context

    def resolve(self, *, context_id: str, content_hash: str):
        if self.context.context_id == context_id and self.context.content_hash == content_hash:
            return self.context
        return None


def _asset(content: bytes = b"table") -> dict[str, object]:
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    return {
        "id": "tbl_sales",
        "space_id": "space_sales",
        "kind": "structured_asset",
        "capabilities": ["table_query"],
        "reference_status": "ready",
        "source_uri": "knowledge://spaces/space_sales/structured-assets/tbl_sales/source",
        "content_digest": digest,
    }


def _principal(*scopes: str):
    from knowledge_contracts import Principal

    return Principal(subject_id="tester", scopes=tuple(scopes))


def test_table_query_service_binds_catalog_asset_and_semantic_context() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import TableQueryPayload, TableQueryService

    asset = _asset()
    semantic = SimpleNamespace(
        context_id="semctx-sales",
        content_hash="sha256:" + "b" * 64,
        source_asset_ids=("tbl_sales",),
    )
    payload = TableQueryPayload(
        asset_id="tbl_sales",
        resource_uri=str(asset["source_uri"]),
        answer="销售额合计为 42。",
        columns=("brand", "sales"),
        preview_rows=({"brand": "A", "sales": 42},),
        row_count=1,
        score=1.0,
        content_digest=str(asset["content_digest"]),
        semantic_context_id=semantic.context_id,
        semantic_context_hash=semantic.content_hash,
    )
    result = __import__("asyncio").run(
        TableQueryService(
            provider=_Provider([payload]),
            catalog=_Catalog(asset),
            semantic_registry=_SemanticRegistry(semantic),
        ).query(
            principal=_principal("knowledge:table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-table"),
            query="销售额",
            asset_id="tbl_sales",
            space_id="space_sales",
            semantic_context=semantic,
        )
    )
    assert result.status == "ok"
    assert result.answer == "销售额合计为 42。"
    assert result.evidence[0].revision == asset["content_digest"]
    assert result.provenance.catalog_revision == "sha256:" + "a" * 64


def test_table_query_service_binds_logical_dataset_to_catalog_metadata() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import TableQueryPayload, TableQueryService

    asset = {
        **_asset(),
        "id": "dataset_sales",
        "source_uri": "knowledge://spaces/space_sales/structured-assets/dataset_sales/source",
        "logical_dataset": {
            "materialization": "virtual",
            "source_asset_ids": ["tbl_jan", "tbl_feb"],
        },
    }
    source_assets = tuple(
        {
            **_asset(content),
            "id": source_id,
            "source_uri": f"knowledge://spaces/space_sales/structured-assets/{source_id}/source",
        }
        for source_id, content in (("tbl_jan", b"jan"), ("tbl_feb", b"feb"))
    )
    payload = TableQueryPayload(
        asset_id="dataset_sales",
        resource_uri=str(asset["source_uri"]),
        answer="逻辑数据集包含 2 行。",
        columns=("month", "sales"),
        preview_rows=({"month": "Jan", "sales": 42},),
        row_count=2,
        score=1.0,
        content_digest=str(asset["content_digest"]),
    )
    result = asyncio.run(
        TableQueryService(provider=_Provider([payload]), catalog=_Catalog(asset, source_assets)).query(
            principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-logical"),
            query="sales",
            dataset_id="dataset_sales",
            space_id="space_sales",
        )
    )
    assert result.status == "ok"
    assert result.provenance.dataset_id == "dataset_sales"

    cross_space = asyncio.run(
        TableQueryService(
            provider=_Provider([payload]),
            catalog=_Catalog(asset, (dict(source_assets[0], space_id="space_other"), source_assets[1])),
        ).query(
            principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-logical-cross-space"),
            query="sales",
            dataset_id="dataset_sales",
            space_id="space_sales",
        )
    )
    assert cross_space.error is not None
    assert cross_space.error.code.value == "binding_unavailable"


def test_local_structured_provider_executes_explicit_logical_union(tmp_path: Path) -> None:
    from knowledge_platform.structured import LocalStructuredFileProvider, StructuredQueryProviderError

    first = tmp_path / "jan.csv"
    second = tmp_path / "feb.csv"
    first.write_text("month,sales\nJan,42\n", encoding="utf-8")
    second.write_text("month,sales\nFeb,51\n", encoding="utf-8")
    first_digest = "sha256:" + hashlib.sha256(first.read_bytes()).hexdigest()
    second_digest = "sha256:" + hashlib.sha256(second.read_bytes()).hexdigest()
    expected_digest = LocalStructuredFileProvider.logical_content_digest(
        [("tbl_jan", first_digest), ("tbl_feb", second_digest)]
    )
    provider = LocalStructuredFileProvider(
        asset_paths={},
        asset_uris={"dataset_sales": "knowledge://spaces/space_sales/structured-assets/dataset_sales/source"},
        logical_asset_sources={"dataset_sales": [("tbl_jan", first), ("tbl_feb", second)]},
    )
    results = asyncio.run(
        provider.query(
            query="sales",
            asset_id="dataset_sales",
            space_id="space_sales",
            limit=5,
            semantic_context=None,
        )
    )
    assert len(results) == 1
    assert results[0].row_count == 2
    assert results[0].content_digest == expected_digest
    assert {row["month"] for row in results[0].preview_rows} == {"Jan", "Feb"}

    incomplete = LocalStructuredFileProvider(
        asset_paths={},
        asset_uris={"dataset_sales": "knowledge://spaces/space_sales/structured-assets/dataset_sales/source"},
        logical_asset_sources={"dataset_sales": [("tbl_jan", first), ("tbl_missing", tmp_path / "missing.csv")]},
    )
    with pytest.raises(StructuredQueryProviderError, match="logical dataset source"):
        asyncio.run(
            incomplete.query(
                query="sales",
                asset_id="dataset_sales",
                space_id="space_sales",
                limit=5,
                semantic_context=None,
            )
        )


def test_logical_dataset_authoring_is_admin_only_and_stages_pending() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import (
        LogicalDatasetAuthoringRequest,
        LogicalDatasetAuthoringService,
    )

    source_assets = tuple(
        {
            **_asset(content),
            "id": source_id,
            "source_uri": f"knowledge://spaces/space_sales/structured-assets/{source_id}/source",
        }
        for source_id, content in (("tbl_jan", b"jan"), ("tbl_feb", b"feb"))
    )

    class Writer:
        def __init__(self) -> None:
            self.records: list[dict[str, object]] = []

        def create_logical_dataset(self, *, record):
            self.records.append(dict(record))
            return dict(record)

    writer = Writer()
    result = LogicalDatasetAuthoringService(
        catalog=_Catalog(source_assets[0], (source_assets[1],)), writer=writer
    ).create(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-authoring"),
        request=LogicalDatasetAuthoringRequest(
            dataset_id="dataset_sales",
            space_id="space_sales",
            title="Sales dataset",
            source_asset_ids=("tbl_jan", "tbl_feb"),
            canonical_columns=("month", "sales"),
        ),
    )
    assert result.status == "ok"
    assert writer.records[0]["reference_status"] == "pending"
    assert result.provenance.capability == "table_query"

    denied = LogicalDatasetAuthoringService(
        catalog=_Catalog(source_assets[0], (source_assets[1],)), writer=writer
    ).create(
        principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
        correlation=Correlation("trace-authoring-denied"),
        request=LogicalDatasetAuthoringRequest(
            dataset_id="dataset_denied",
            space_id="space_sales",
            title="Denied",
            source_asset_ids=("tbl_jan",),
            canonical_columns=("month",),
        ),
    )
    assert denied.error is not None
    assert denied.error.code.value == "permission_denied"
    with pytest.raises(ValueError, match="dataset_id is invalid"):
        LogicalDatasetAuthoringRequest(
            dataset_id="dataset:invalid",
            space_id="space_sales",
            title="Invalid URI identity",
            source_asset_ids=("tbl_jan",),
            canonical_columns=("month",),
        )


def test_sqlite_structured_asset_writer_persists_staged_logical_dataset(tmp_path: Path) -> None:
    import sqlite3

    from knowledge_contracts import Correlation
    from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
    from knowledge_platform.structured import (
        LocalStructuredFileProvider,
        LogicalDatasetAuthoringRequest,
        LogicalDatasetAuthoringService,
    )

    database = tmp_path / "catalog.sqlite3"
    with sqlite3.connect(database) as connection:
        connection.executescript(
            """
            CREATE TABLE knowledge_structured_assets (
                id TEXT PRIMARY KEY, space_id TEXT NOT NULL, source_key TEXT NOT NULL,
                document_asset_id TEXT, source_type TEXT NOT NULL, file_name TEXT NOT NULL,
                sheet_name TEXT, size_bytes INTEGER NOT NULL, modified_at TEXT, source_uri TEXT NOT NULL,
                source_reference_digest TEXT NOT NULL, logical_path_digest TEXT NOT NULL,
                profile_uri TEXT NOT NULL, profile_reference_digest TEXT NOT NULL, content_digest TEXT NOT NULL,
                profile_status TEXT NOT NULL, row_count INTEGER, column_count INTEGER NOT NULL,
                columns_json TEXT NOT NULL, reference_status TEXT NOT NULL, capabilities TEXT NOT NULL,
                metadata_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            """
        )
        source_uri = "knowledge://spaces/space_sales/structured-assets/tbl_jan/source"
        connection.execute(
            "INSERT INTO knowledge_structured_assets VALUES (?, ?, ?, NULL, 'excel', 'Jan', NULL, 1, NULL, ?, '', '', '', '', ?, 'ready', 1, 1, '[\"sales\"]', 'ready', '[\"table_query\"]', '{}', 'now', 'now')",
            ("tbl_jan", "space_sales", "tbl_jan", source_uri, "sha256:" + "1" * 64),
        )
        connection.commit()
    repository = SqliteCatalogQueryRepository(database)
    result = LogicalDatasetAuthoringService(
        catalog=repository,
        writer=SqliteStructuredAssetWriter(database),
    ).create(
        principal=_principal("knowledge:admin", "knowledge:space:space_sales"),
        correlation=Correlation("trace-sqlite-authoring"),
        request=LogicalDatasetAuthoringRequest(
            dataset_id="dataset_sales",
            space_id="space_sales",
            title="Sales dataset",
            source_asset_ids=("tbl_jan",),
            canonical_columns=("sales",),
        ),
    )
    assert result.status == "ok"
    stored = repository.get_structured_asset(asset_id="dataset_sales")
    assert stored is not None
    assert stored["reference_status"] == "pending"
    assert stored["logical_dataset"]["source_asset_ids"] == ["tbl_jan"]

    definition_digest = result.data["definition_digest"]
    writer = SqliteStructuredAssetWriter(database)
    published_digest = LocalStructuredFileProvider.logical_content_digest(
        [("tbl_jan", "sha256:" + "1" * 64)]
    )
    with pytest.raises(ValueError, match="lineage or digest"):
        writer.publish_logical_dataset(
            principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
            dataset_id="dataset_sales",
            expected_definition_digest=str(definition_digest),
            content_digest="sha256:" + "3" * 64,
            columns=("wrong",),
            row_count=3,
            source_snapshot=(
                {"asset_id": "tbl_jan", "content_digest": "sha256:" + "1" * 64, "row_count": 3},
            ),
        )
    published = writer.publish_logical_dataset(
        principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
        dataset_id="dataset_sales",
        expected_definition_digest=str(definition_digest),
        content_digest=published_digest,
        columns=("sales",),
        row_count=3,
        source_snapshot=(
            {"asset_id": "tbl_jan", "content_digest": "sha256:" + "1" * 64, "row_count": 3},
        ),
    )
    assert published["reference_status"] == "ready"
    assert repository.get_structured_asset(asset_id="dataset_sales")["content_digest"] == published_digest
    with pytest.raises(ValueError, match="no longer pending"):
        writer.publish_logical_dataset(
            principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
            dataset_id="dataset_sales",
            expected_definition_digest=str(definition_digest),
            content_digest="sha256:" + "3" * 64,
            columns=("sales",),
            row_count=4,
            source_snapshot=(
                {"asset_id": "tbl_jan", "content_digest": "sha256:" + "1" * 64, "row_count": 4},
            ),
        )


def test_static_semantic_context_registry_binds_compiled_context() -> None:
    from knowledge_platform.structured import StaticSemanticContextRegistry
    from knowledge_platform.structured.ports import SemanticContextBinding

    context = SemanticContextBinding(
        context_id="semctx-sales",
        content_hash="sha256:" + "b" * 64,
        source_asset_ids=("tbl_sales",),
    )
    registry = StaticSemanticContextRegistry([context])
    assert registry.resolve(context_id=context.context_id, content_hash=context.content_hash) is context
    assert registry.resolve(context_id="semctx-other", content_hash=context.content_hash) is None


def test_logical_dataset_processing_verifies_source_bytes_before_ready_publish(tmp_path: Path) -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import (
        LocalStructuredFileProvider,
        LogicalDatasetAuthoringRequest,
        LogicalDatasetProcessingRequest,
        LogicalDatasetProcessingService,
    )

    first = tmp_path / "jan.csv"
    second = tmp_path / "feb.csv"
    first.write_text("month,sales\nJan,42\n", encoding="utf-8")
    second.write_text("month,sales\nFeb,51\n", encoding="utf-8")

    def source(asset_id: str, path: Path) -> dict[str, object]:
        return {
            **_asset(path.read_bytes()),
            "id": asset_id,
            "source_uri": f"knowledge://spaces/space_sales/structured-assets/{asset_id}/source",
            "sheet_name": None,
        }

    source_assets = (source("tbl_jan", first), source("tbl_feb", second))
    authoring_request = LogicalDatasetAuthoringRequest(
        dataset_id="dataset_sales",
        space_id="space_sales",
        title="Sales dataset",
        source_asset_ids=("tbl_jan", "tbl_feb"),
        canonical_columns=("month", "sales"),
    )
    dataset = {
        **_asset(),
        "id": "dataset_sales",
        "source_uri": "knowledge://spaces/space_sales/structured-assets/dataset_sales/source",
        "reference_status": "pending",
        "content_digest": authoring_request.definition_digest(),
        "logical_dataset": authoring_request.definition()["logical_dataset"],
    }

    class Publisher:
        def __init__(self, catalog: _Catalog) -> None:
            self.catalog = catalog
            self.calls = 0

        def publish_logical_dataset(self, **kwargs):
            self.calls += 1
            record = dict(self.catalog.assets[kwargs["dataset_id"]])
            record.update(
                content_digest=kwargs["content_digest"],
                reference_status="ready",
                row_count=kwargs["row_count"],
                columns=list(kwargs["columns"]),
            )
            self.catalog.assets[kwargs["dataset_id"]] = record
            return record

    catalog = _Catalog(dataset, source_assets)
    publisher = Publisher(catalog)
    service = LogicalDatasetProcessingService(
        catalog=catalog,
        profiler=LocalStructuredFileProvider(asset_paths={}, asset_uris={}),
        publisher=publisher,
    )
    result = asyncio.run(
        service.process(
            principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
            correlation=Correlation("trace-processing"),
            request=LogicalDatasetProcessingRequest(
                dataset_id="dataset_sales",
                space_id="space_sales",
                source_paths={"tbl_jan": first, "tbl_feb": second},
            ),
        )
    )
    assert result.status == "ok"
    assert result.data["dataset"]["reference_status"] == "ready"
    assert result.data["source_snapshot"][0]["asset_id"] == "tbl_jan"
    assert publisher.calls == 1

    second.write_text("month,other\nFeb,51\n", encoding="utf-8")
    dataset["reference_status"] = "pending"
    catalog.assets["dataset_sales"] = dataset
    stale = asyncio.run(
        service.process(
            principal=_principal("knowledge:processing", "knowledge:space:space_sales"),
            correlation=Correlation("trace-processing-stale"),
            request=LogicalDatasetProcessingRequest(
                dataset_id="dataset_sales",
                space_id="space_sales",
                source_paths={"tbl_jan": first, "tbl_feb": second},
            ),
        )
    )
    assert stale.error is not None
    assert stale.error.code.value == "binding_unavailable"
    assert publisher.calls == 1


def test_logical_dataset_processing_requires_processing_scope() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import LogicalDatasetProcessingRequest, LogicalDatasetProcessingService

    class NeverCalled:
        def inspect_source(self, **_kwargs):
            raise AssertionError("profiler must not run without Processing scope")

    result = asyncio.run(
        LogicalDatasetProcessingService(
            catalog=_Catalog(_asset()), profiler=NeverCalled(), publisher=NeverCalled()
        ).process(
            principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-processing-denied"),
            request=LogicalDatasetProcessingRequest(
                dataset_id="tbl_sales",
                space_id="space_sales",
                source_paths={"tbl_sales": Path("/tmp/source.csv")},
            ),
        )
    )
    assert result.error is not None
    assert result.error.code.value == "permission_denied"


def test_table_query_service_rejects_cross_space_and_over_limit() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import TableQueryPayload, TableQueryService

    asset = _asset()
    payload = TableQueryPayload(
        asset_id="tbl_sales",
        resource_uri=str(asset["source_uri"]),
        answer="ok",
        columns=("sales",),
        preview_rows=(),
        row_count=0,
        score=1.0,
        content_digest=str(asset["content_digest"]),
    )
    service = TableQueryService(provider=_Provider([payload, payload]), catalog=_Catalog(asset))
    result = __import__("asyncio").run(
        service.query(
            principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-table-limit"),
            query="sales",
            space_id="space_sales",
            limit=1,
        )
    )
    assert result.status == "error"
    assert result.error is not None
    assert result.error.code.value == "capability_unavailable"


def test_table_query_requires_global_admin_and_registered_semantic_context() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import TableQueryPayload, TableQueryService

    asset = _asset()
    payload = TableQueryPayload(
        asset_id="tbl_sales",
        resource_uri=str(asset["source_uri"]),
        answer="ok",
        columns=("sales",),
        row_count=0,
        score=1.0,
        content_digest=str(asset["content_digest"]),
    )
    service = TableQueryService(provider=_Provider([payload]), catalog=_Catalog(asset))
    global_result = __import__("asyncio").run(
        service.query(
            principal=_principal("knowledge:table_query"),
            correlation=Correlation("trace-table-global"),
            query="sales",
        )
    )
    assert global_result.error is not None
    assert global_result.error.code.value == "permission_denied"

    semantic = SimpleNamespace(
        context_id="semctx-unregistered",
        content_hash="sha256:" + "c" * 64,
        source_asset_ids=("tbl_sales",),
    )
    unregistered = __import__("asyncio").run(
        service.query(
            principal=_principal("knowledge.table_query", "knowledge:space:space_sales"),
            correlation=Correlation("trace-table-semantic"),
            query="sales",
            space_id="space_sales",
            semantic_context=semantic,
        )
    )
    assert unregistered.error is not None
    assert unregistered.error.code.value == "binding_unavailable"


def test_table_payload_rejects_secrets_and_unbounded_preview() -> None:
    from knowledge_platform.structured import TableQueryPayload

    common = {
        "asset_id": "tbl_sales",
        "resource_uri": "knowledge://spaces/space_sales/structured-assets/tbl_sales/source",
        "answer": "ok",
        "columns": ("notes",),
        "row_count": 1,
        "score": 1.0,
        "content_digest": "sha256:" + "a" * 64,
    }
    with pytest.raises(ValueError, match="secret"):
        TableQueryPayload(**common, preview_rows=({"notes": "api_key=sk-12345678"},))
    with pytest.raises(ValueError, match="too many or unknown"):
        TableQueryPayload(
            **{**common, "columns": tuple(f"c{i}" for i in range(200))},
            preview_rows=({**{f"c{i}": i for i in range(200)}, "unknown": 1},),
        )


def test_local_structured_provider_rejects_symlink_parent(tmp_path: Path) -> None:
    from knowledge_platform.structured import LocalStructuredFileProvider, StructuredQueryProviderError

    real_dir = tmp_path / "real"
    real_dir.mkdir()
    real_file = real_dir / "sales.csv"
    real_file.write_text("sales\n42\n", encoding="utf-8")
    link_dir = tmp_path / "link"
    link_dir.symlink_to(real_dir, target_is_directory=True)
    provider = LocalStructuredFileProvider(
        asset_paths={"tbl_sales": link_dir / "sales.csv"},
        asset_uris={"tbl_sales": "knowledge://spaces/space_sales/structured-assets/tbl_sales/source"},
    )
    with pytest.raises(StructuredQueryProviderError):
        __import__("asyncio").run(
            provider.query(
                query="sales", asset_id="tbl_sales", space_id="space_sales", limit=1, semantic_context=None
            )
        )


def test_local_structured_provider_is_explicit_and_hides_secret_columns(tmp_path: Path) -> None:
    from knowledge_platform.structured import LocalStructuredFileProvider

    path = tmp_path / "sales.csv"
    path.write_text("brand,sales,password\nA,42,do-not-return\n", encoding="utf-8")
    provider = LocalStructuredFileProvider(
        asset_paths={"tbl_sales": path},
        asset_uris={"tbl_sales": "knowledge://spaces/space_sales/structured-assets/tbl_sales/source"},
    )
    results = __import__("asyncio").run(
        provider.query(
            query="sales",
            asset_id="tbl_sales",
            space_id="space_sales",
            limit=5,
            semantic_context=None,
        )
    )
    assert len(results) == 1
    assert results[0].columns == ("brand", "sales")
    assert results[0].preview_rows[0]["sales"] == "42"
    assert "password" not in results[0].answer

    with pytest.raises(ValueError):
        LocalStructuredFileProvider(
            asset_paths={"tbl_sales": path},
            asset_uris={"tbl_other": "knowledge://spaces/space_sales/structured-assets/tbl_other/source"},
        )


def test_local_structured_provider_reads_explicit_excel_sheet(tmp_path: Path) -> None:
    pd = pytest.importorskip("pandas")
    from knowledge_platform.structured import LocalStructuredFileProvider

    path = tmp_path / "sales.xlsx"
    with pd.ExcelWriter(path) as writer:
        pd.DataFrame({"brand": ["A"], "sales": [42]}).to_excel(writer, sheet_name="Orders", index=False)
    provider = LocalStructuredFileProvider(
        asset_paths={"tbl_sales": path},
        asset_uris={"tbl_sales": "knowledge://spaces/space_sales/structured-assets/tbl_sales/source"},
        asset_sheets={"tbl_sales": "Orders"},
    )
    results = __import__("asyncio").run(
        provider.query(
            query="sales",
            asset_id="tbl_sales",
            space_id="space_sales",
            limit=5,
            semantic_context=None,
        )
    )
    assert len(results) == 1
    assert results[0].row_count == 1
    assert results[0].preview_rows[0]["sales"] == 42


def test_table_query_is_exposed_by_rest_and_mcp_adapters() -> None:
    from knowledge_contracts import Correlation
    from knowledge_platform.structured import TableQueryPayload, TableQueryService
    from knowledge_platform.transport import McpQueryAdapter, RestQueryAdapter

    asset = _asset()
    payload = TableQueryPayload(
        asset_id="tbl_sales",
        resource_uri=str(asset["source_uri"]),
        answer="ok",
        columns=("sales",),
        row_count=1,
        score=1.0,
        content_digest=str(asset["content_digest"]),
    )
    table = TableQueryService(
        provider=_Provider([payload]),
        catalog=_Catalog(asset),
    )
    rest = RestQueryAdapter(
        catalog=object(),
        search=object(),
        asset_read=object(),
        document=object(),
        wiki=object(),
        table=table,
    )
    principal = _principal("knowledge.table_query", "knowledge:space:space_sales")
    result = asyncio.run(
        rest.handle(
            method="POST",
            path="/v1/table/query",
            principal=principal,
            correlation=Correlation("trace-table-rest"),
            body={"query": "sales", "space_id": "space_sales"},
        )
    )
    assert result["status"] == "ok"
    mcp_result = asyncio.run(
        McpQueryAdapter(rest).call_tool(
            name="table_query",
            arguments={"query": "sales", "space_id": "space_sales"},
            principal=principal,
            correlation=Correlation("trace-table-mcp"),
        )
    )
    assert mcp_result["structuredContent"]["status"] == "ok"
