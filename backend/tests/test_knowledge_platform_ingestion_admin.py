from __future__ import annotations

import hashlib
import json
from pathlib import Path

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.ingestion import (
    AssetUploadRequest,
    LocalAssetUploadService,
    LocalPackageImportService,
    PackageImportRequest,
)
from knowledge_platform.package import KnowledgePackageBuilder, export_package_zip
from knowledge_platform.transport import RestAdminAdapter, StaticProcessingBindingResolver, create_platform_app


def _admin(space_id: str = "space_1") -> Principal:
    return Principal(
        subject_id="local-admin",
        scopes=("knowledge.admin", f"knowledge.space:{space_id}"),
    )


def _correlation() -> Correlation:
    return Correlation("ingestion-admin-test")


def _adapter(upload: LocalAssetUploadService | None = None, package: LocalPackageImportService | None = None):
    return RestAdminAdapter(
        authoring=object(),
        processing=object(),
        bindings=StaticProcessingBindingResolver({}),
        asset_upload=upload,
        package_import=package,
    )


def test_asset_upload_stages_explicit_binding_without_leaking_path_and_is_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    content = b"local knowledge"
    source.write_bytes(content)
    service = LocalAssetUploadService(bindings={"binding_1": source}, staging_root=tmp_path / "staging")
    request = AssetUploadRequest(
        asset_id="asset_upload_1",
        space_id="space_1",
        title="Local notes",
        filename="notes.md",
        mime_type="text/markdown",
        binding_id="binding_1",
        content_digest="sha256:" + hashlib.sha256(content).hexdigest(),
        idempotency_key="upload-1",
    )

    first = service.stage(principal=_admin(), correlation=_correlation(), request=request)
    second = service.stage(principal=_admin(), correlation=_correlation(), request=request)

    assert first.status == second.status == "ok"
    assert first.data == second.data
    payload = json.dumps(first.to_dict(), ensure_ascii=False)
    assert str(source) not in payload
    assert "binding_1" not in payload
    assert first.data["upload"]["status"] == "staged"
    assert (tmp_path / "staging/assets/space_1/asset_upload_1.content").read_bytes() == content
    assert "knowledge://spaces/space_1/assets/asset_upload_1/staged-content" in payload


def test_asset_upload_fails_closed_for_scope_digest_and_symlink(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    source.write_bytes(b"actual")
    service = LocalAssetUploadService(bindings={"binding_1": source}, staging_root=tmp_path / "staging")
    base = dict(
        asset_id="asset_upload_1",
        space_id="space_1",
        title="Local notes",
        filename="notes.md",
        mime_type="text/markdown",
        binding_id="binding_1",
        content_digest="sha256:" + "0" * 64,
        idempotency_key="upload-1",
    )
    assert service.stage(
        principal=Principal(subject_id="tenant", scopes=("knowledge.admin", "knowledge.space:space_1"), tenant_id="tenant"),
        correlation=_correlation(),
        request=AssetUploadRequest(**base),
    ).error.code.value == "permission_denied"
    assert service.stage(principal=_admin(), correlation=_correlation(), request=AssetUploadRequest(**base)).error.code.value == "binding_unavailable"
    link = tmp_path / "link.md"
    link.symlink_to(source)
    symlink_service = LocalAssetUploadService(bindings={"binding_1": link}, staging_root=tmp_path / "staging-2")
    valid_digest = "sha256:" + hashlib.sha256(b"actual").hexdigest()
    result = symlink_service.stage(
        principal=_admin(),
        correlation=_correlation(),
        request=AssetUploadRequest(**{**base, "content_digest": valid_digest}),
    )
    assert result.error.code.value == "binding_unavailable"


def test_package_import_is_validation_only_and_does_not_return_staging_path(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    content = b"package content"
    source.write_bytes(content)
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    package_root = tmp_path / "package"
    KnowledgePackageBuilder().build(
        output_dir=package_root,
        package_id="package_local",
        version="1.0.0",
        spaces=[{"id": "space_1", "name": "Local"}],
        collections=[
            {
                "id": "collection_1",
                "space_id": "space_1",
                "name": "Local Collection",
                "version": "v1",
                "kind": "document",
                "asset_ids": ["asset_1"],
                "capabilities": ["knowledge_read"],
            }
        ],
        assets=[
            {
                "id": "asset_1",
                "space_id": "space_1",
                "kind": "document",
                "title": "Local notes",
                "source_uri": "knowledge://spaces/space_1/assets/asset_1",
                "content_digest": digest,
            }
        ],
        asset_files={"asset_1": source},
        capabilities=["knowledge_read"],
        catalog_revision="sha256:" + "e" * 64,
    )
    package_zip = export_package_zip(package_root, tmp_path / "package.zip")
    service = LocalPackageImportService(
        bindings={"package_binding_1": package_zip},
        staging_root=tmp_path / "package-staging",
    )
    request = PackageImportRequest(package_ref="package_binding_1", idempotency_key="package-import-1")
    result = service.stage(principal=_admin(), correlation=_correlation(), request=request)

    assert result.status == "ok"
    assert result.data["package_import"]["status"] == "validated_staged"
    payload = json.dumps(result.to_dict(), ensure_ascii=False)
    assert str(package_zip) not in payload
    assert str(tmp_path) not in payload
    assert "package_revision" in payload


def test_admin_fastapi_routes_keep_upload_and_import_in_admin_plane(tmp_path: Path) -> None:
    source = tmp_path / "notes.md"
    content = b"http upload"
    source.write_bytes(content)
    upload = LocalAssetUploadService(bindings={"binding_1": source}, staging_root=tmp_path / "staging")
    package = LocalPackageImportService(bindings={}, staging_root=tmp_path / "package-staging")
    adapter = _adapter(upload=upload, package=package)
    app = create_platform_app(
        query_adapter=object(),  # route construction does not call the Query adapter
        admin_adapter=adapter,
        principal_provider=lambda: _admin(),
        correlation_provider=lambda: _correlation(),
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/assets:upload",
            json={
                "asset_id": "asset_http_1",
                "space_id": "space_1",
                "title": "HTTP notes",
                "filename": "notes.md",
                "mime_type": "text/markdown",
                "binding_id": "binding_1",
                "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
                "idempotency_key": "http-upload-1",
            },
        )
        package_response = client.post(
            "/v1/packages:import",
            json={"package_ref": "package_binding_1", "idempotency_key": "package-http-1"},
        )
    assert response.status_code == 200
    assert response.json()["status"] == "ok"
    assert package_response.status_code == 200
    assert package_response.json()["error"]["code"] == "binding_unavailable"
