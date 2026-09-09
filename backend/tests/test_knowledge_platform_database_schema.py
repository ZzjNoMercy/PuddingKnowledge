from __future__ import annotations

import asyncio
from collections.abc import Sequence

import pytest

from knowledge_contracts import Correlation, Principal
from knowledge_platform.database import (
    DatabaseDatasetBinding,
    DatabaseSchemaQueryService,
    DatabaseSchemaTable,
    StaticDatabaseDatasetResolver,
)
from knowledge_platform.transport import RestQueryAdapter


def _digest(seed: str) -> str:
    return "sha256:" + seed * 64


def _binding(*, allowed_tables: tuple[str, ...] = ("sales",)) -> DatabaseDatasetBinding:
    return DatabaseDatasetBinding(
        dataset_id="dataset_sales",
        space_id="space_sales",
        dataset_version="1.0.0",
        deployment_revision="deploy-local",
        dialect="postgresql",
        allowed_tables=allowed_tables,
        semantic_context_hash=_digest("a"),
        source_revision=_digest("b"),
        provider_version="local-test",
    )


class _Reader:
    def __init__(self, tables: Sequence[DatabaseSchemaTable]) -> None:
        self.tables = tuple(tables)

    def read(self, *, binding: DatabaseDatasetBinding) -> Sequence[DatabaseSchemaTable]:
        assert binding.dataset_id == "dataset_sales"
        return self.tables


def _service(reader: _Reader, *, binding: DatabaseDatasetBinding | None = None) -> DatabaseSchemaQueryService:
    current = binding or _binding()
    return DatabaseSchemaQueryService(
        datasets=StaticDatabaseDatasetResolver({(current.space_id, current.dataset_id): current}),
        reader=reader,
    )


def _principal(*scopes: str, tenant_id: str | None = None) -> Principal:
    return Principal(
        subject_id="schema-reader",
        scopes=("knowledge.database_schema", "knowledge:space:space_sales", *scopes),
        tenant_id=tenant_id,
    )


def _table(name: str = "public.sales") -> DatabaseSchemaTable:
    return DatabaseSchemaTable(
        table_name=name,
        columns=("id", "amount"),
        schema_revision=_digest("c"),
    )


def test_database_schema_is_space_and_binding_scoped_and_portable() -> None:
    result = _service(_Reader((_table(),))).list(
        principal=_principal(),
        correlation=Correlation("schema-ok"),
        space_id="space_sales",
        dataset_id="dataset_sales",
    )

    assert result.status == "ok"
    assert result.to_dict()["data"] == {
        "space_id": "space_sales",
        "dataset_id": "dataset_sales",
        "source_revision": _digest("b"),
        "tables": [{"table_name": "public.sales", "columns": ["id", "amount"], "schema_revision": _digest("c")}],
    }
    assert result.provenance is not None
    assert result.provenance.capability == "database_schema"
    assert "path" not in str(result.to_dict())
    assert "connection" not in str(result.to_dict())


def test_database_schema_fails_closed_for_tenant_or_missing_space_scope() -> None:
    service = _service(_Reader((_table(),)))
    tenant = service.list(
        principal=_principal(tenant_id="tenant-1"),
        correlation=Correlation("schema-tenant"),
        space_id="space_sales",
        dataset_id="dataset_sales",
    )
    missing_space = service.list(
        principal=Principal("schema-reader", scopes=("knowledge.database_schema",)),
        correlation=Correlation("schema-space"),
        space_id="space_sales",
        dataset_id="dataset_sales",
    )

    assert tenant.error is not None and tenant.error.code.value == "permission_denied"
    assert missing_space.error is not None and missing_space.error.code.value == "permission_denied"


def test_database_schema_rejects_table_or_column_escape_from_reader() -> None:
    outside = _service(_Reader((_table("public.other"),))).list(
        principal=_principal(),
        correlation=Correlation("schema-table-escape"),
        space_id="space_sales",
        dataset_id="dataset_sales",
    )
    with pytest.raises(ValueError, match="secret-bearing"):
        DatabaseSchemaTable(
            table_name="public.sales",
            columns=("id", "api_token"),
            schema_revision=_digest("c"),
        )

    assert outside.error is not None and outside.error.code.value == "binding_unavailable"


def test_rest_adapter_exposes_schema_as_a_fixed_get_route() -> None:
    service = _service(_Reader((_table(),)))
    adapter = RestQueryAdapter(
        catalog=object(),
        search=object(),
        asset_read=object(),
        document=object(),
        wiki=object(),
        database_schema=service,
    )
    response = asyncio.run(adapter.handle(
        method="GET",
        path="/v1/database/schema",
        principal=_principal(),
        correlation=Correlation("schema-rest"),
        body={"space_id": "space_sales", "dataset_id": "dataset_sales"},
    ))

    assert response["status"] == "ok"
    assert response["data"]["tables"][0]["table_name"] == "public.sales"
