from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.semantic import (
    InMemorySemanticMarkdownRepository,
    SemanticMarkdownAdminService,
    SemanticMarkdownDefinition,
    SqliteSemanticMarkdownRepository,
)
from knowledge_platform.transport import (
    McpQueryAdapter,
    RestAdminAdapter,
    StaticProcessingBindingResolver,
    create_admin_router,
)


def _principal(space: str = "space_sales") -> Principal:
    return Principal(subject_id="admin", scopes=("knowledge:admin", f"knowledge:space:{space}"))


def _definition(space: str = "space_sales", asset_id: str = "measure:revenue") -> SemanticMarkdownDefinition:
    return SemanticMarkdownDefinition(
        id=asset_id,
        space_id=space,
        semantic_type="measure",
        name="Revenue",
        description="Net revenue measure.",
        aliases=("sales_revenue",),
        tags=("finance",),
        frontmatter={"grain": "month"},
        body="# Revenue\n\n- Sum the approved revenue column.",
    )


def test_definition_is_bounded_portable_and_rejects_unsafe_content() -> None:
    with pytest.raises(ValueError):
        _definition(asset_id="measure:bad/../path")
    with pytest.raises(ValueError):
        SemanticMarkdownDefinition(id="measure:x", space_id="space_sales", semantic_type="entity_lookup", name="X", body="# X")
    with pytest.raises(ValueError):
        SemanticMarkdownDefinition(id="measure:x", space_id="space_sales", semantic_type="measure", name="X", body="# X\nsecret: abc")


def test_admin_requires_explicit_prepare_then_decision_and_space_scope() -> None:
    service = SemanticMarkdownAdminService(repository=InMemorySemanticMarkdownRepository())
    created = service.prepare(principal=_principal(), correlation=Correlation("create"), definition=_definition())
    assert created.to_dict()["data"]["asset"]["status"] == "waiting_for_confirmation"
    denied = service.decide(principal=_principal("space_other"), correlation=Correlation("denied"), asset_id="measure:revenue", space_id="space_sales", decision="confirm", expected_status="waiting_for_confirmation")
    assert denied.to_dict()["error"]["code"] == "permission_denied"
    active = service.decide(principal=_principal(), correlation=Correlation("confirm"), asset_id="measure:revenue", space_id="space_sales", decision="confirm", expected_status="waiting_for_confirmation")
    assert active.to_dict()["data"]["asset"]["status"] == "active"
    repeat = service.decide(principal=_principal(), correlation=Correlation("repeat"), asset_id="measure:revenue", space_id="space_sales", decision="confirm", expected_status="waiting_for_confirmation")
    assert repeat.to_dict()["data"]["asset"]["status"] == "active"
    assert service.active(space_id="space_sales")[0].definition.id == "measure:revenue"


def test_sqlite_repository_survives_restart_and_keeps_pending_out_of_runtime_snapshot(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite"
    service = SemanticMarkdownAdminService(repository=SqliteSemanticMarkdownRepository(database))
    service.prepare(principal=_principal(), correlation=Correlation("create"), definition=_definition())
    assert len(service.active(space_id="space_sales")) == 0
    restarted = SemanticMarkdownAdminService(repository=SqliteSemanticMarkdownRepository(database))
    restarted.decide(principal=_principal(), correlation=Correlation("confirm"), asset_id="measure:revenue", space_id="space_sales", decision="confirm", expected_status="waiting_for_confirmation")
    assert restarted.active(space_id="space_sales")[0].status == "active"


@pytest.mark.asyncio
async def test_admin_transport_exposes_discover_prepare_decide() -> None:
    service = SemanticMarkdownAdminService(repository=InMemorySemanticMarkdownRepository())
    adapter = RestAdminAdapter(authoring=object(), processing=object(), bindings=StaticProcessingBindingResolver({}), semantic_markdown=service)
    principal = _principal()
    prepared = await adapter.handle(method="POST", path="/v1/semantic-assets", principal=principal, correlation=Correlation("prepare"), body={**_definition().definition()})
    assert prepared["data"]["asset"]["status"] == "waiting_for_confirmation"
    listed = await adapter.handle(method="GET", path="/v1/semantic-assets", principal=principal, correlation=Correlation("list"), body={"space_id": "space_sales"})
    assert listed["data"]["assets"][0]["id"] == "measure:revenue"
    decided = await adapter.handle(method="POST", path="/v1/semantic-assets/measure:revenue:decision", principal=principal, correlation=Correlation("decide"), body={"space_id": "space_sales", "decision": "confirm", "expected_status": "waiting_for_confirmation"})
    assert decided["data"]["asset"]["status"] == "active"


def test_fastapi_admin_router_registers_semantic_markdown_routes() -> None:
    service = SemanticMarkdownAdminService(repository=InMemorySemanticMarkdownRepository())
    adapter = RestAdminAdapter(authoring=object(), processing=object(), bindings=StaticProcessingBindingResolver({}), semantic_markdown=service)
    app = FastAPI()
    app.include_router(create_admin_router(adapter, principal_provider=lambda: _principal(), correlation_provider=lambda: Correlation("http")))
    response = TestClient(app).post("/v1/semantic-assets", json=_definition().definition())
    assert response.status_code == 200
    assert response.json()["data"]["asset"]["status"] == "waiting_for_confirmation"


@pytest.mark.asyncio
async def test_mcp_reads_only_active_semantic_markdown_with_exact_space_scope() -> None:
    service = SemanticMarkdownAdminService(repository=InMemorySemanticMarkdownRepository())
    principal = _principal()
    service.prepare(principal=principal, correlation=Correlation("prepare-resource"), definition=_definition())
    service.prepare(
        principal=principal,
        correlation=Correlation("prepare-pending-resource"),
        definition=_definition(asset_id="measure:pending"),
    )
    mcp = McpQueryAdapter(object(), semantic_markdown=service)
    assert any("/semantics/" in item["uriTemplate"] for item in mcp.resource_templates())

    pending_uri = "knowledge://spaces/space_sales/semantics/measure-pending"
    pending = await mcp.read_resource(
        resource_uri=pending_uri,
        principal=principal,
        correlation=Correlation("pending-resource"),
        start=0,
        end=4096,
    )
    assert pending["structuredContent"]["error"]["code"] == "not_found"

    service.decide(
        principal=principal,
        correlation=Correlation("confirm-resource"),
        asset_id="measure:revenue",
        space_id="space_sales",
        decision="confirm",
        expected_status="waiting_for_confirmation",
    )
    uri = "knowledge://spaces/space_sales/semantics/measure-revenue"
    read = await mcp.read_resource(
        resource_uri=uri,
        principal=principal,
        correlation=Correlation("read-resource"),
        start=0,
        end=4096,
    )
    assert read["structuredContent"]["status"] == "ok"
    assert read["structuredContent"]["provenance"]["space_id"] == "space_sales"
    assert read["contents"][0]["mimeType"] == "text/markdown"
    assert "# Revenue" in read["contents"][0]["text"]

    denied = await mcp.read_resource(
        resource_uri=uri,
        principal=_principal("space_other"),
        correlation=Correlation("wrong-space-resource"),
        start=0,
        end=4096,
    )
    assert denied["structuredContent"]["error"]["code"] == "permission_denied"


@pytest.mark.asyncio
async def test_mcp_semantic_resource_fails_closed_on_uri_collision_range_and_tenant() -> None:
    service = SemanticMarkdownAdminService(repository=InMemorySemanticMarkdownRepository())
    principal = _principal()
    for asset_id in ("measure:a-b", "measure:a:b"):
        service.prepare(
            principal=principal,
            correlation=Correlation(f"prepare-{asset_id.replace(':', '-')}"),
            definition=_definition(asset_id=asset_id),
        )
        service.decide(
            principal=principal,
            correlation=Correlation(f"confirm-{asset_id.replace(':', '-')}"),
            asset_id=asset_id,
            space_id="space_sales",
            decision="confirm",
            expected_status="waiting_for_confirmation",
        )
    mcp = McpQueryAdapter(object(), semantic_markdown=service)
    uri = "knowledge://spaces/space_sales/semantics/measure-a-b"
    ambiguous = await mcp.read_resource(
        resource_uri=uri,
        principal=principal,
        correlation=Correlation("ambiguous-resource"),
        start=0,
        end=4096,
    )
    assert ambiguous["structuredContent"]["error"]["code"] == "binding_unavailable"
    invalid_range = await mcp.read_resource(
        resource_uri="knowledge://spaces/space_sales/semantics/measure-revenue",
        principal=principal,
        correlation=Correlation("invalid-range-resource"),
        start=5,
        end=5,
    )
    assert invalid_range["structuredContent"]["error"]["code"] == "invalid_request"
    tenant = await mcp.read_resource(
        resource_uri=uri,
        principal=Principal("tenant-user", scopes=("knowledge.read", "knowledge:space:space_sales"), tenant_id="tenant-a"),
        correlation=Correlation("tenant-resource"),
        start=0,
        end=4096,
    )
    assert tenant["structuredContent"]["error"]["code"] == "permission_denied"
