from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeIngestionJob, KnowledgeSpace
from knowledge_platform.ingestion import AssetUploadRequest
from knowledge_platform.local.files import LocalFileService


def _principal(space="space_kb_default"):
    return Principal(subject_id="admin", scopes=("knowledge.admin", f"knowledge.space:{space}"))


def _fixture(tmp_path: Path, *, content=b"hello file\n"):
    source_dir = tmp_path / "bound"
    source_dir.mkdir()
    source = source_dir / "note.md"
    source.write_bytes(content)
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    with engine.begin() as conn:
        conn.execute(KnowledgeSpace.__table__.insert().values(id="space_kb_default", name="Default", description="", permissions_json={}))
    config = {"version": 1, "bindings": [{"id": "b", "path": str(source), "space_id": "space_kb_default"}],
              "parsers": [{"id": "native"}], "collection_id": "uploaded_files"}
    service = LocalFileService(config, catalog, tmp_path / "state")
    digest = "sha256:" + hashlib.sha256(content).hexdigest()
    request = AssetUploadRequest("asset_1", "space_kb_default", "Note", "note.md", "text/markdown", "b", digest, "once")
    return service, engine, source, request


def test_native_import_is_idempotent_and_restart_readable(tmp_path):
    service, engine, source, request = _fixture(tmp_path)
    try:
        first = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("one"), request=request))
        second = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("two"), request=request))
        assert first.status == second.status == "ok"
        assert first.data == second.data
        asset_id = first.data["upload"]["asset_id"]
        assert service.read_published(first.data["upload"]["resource_uri"]) == source.read_bytes()
        assert service.derivative_targets(asset_id)
    finally:
        service.close(); engine.dispose()

    service2 = LocalFileService({"version": 1, "bindings": [{"id": "b", "path": str(source), "space_id": "space_kb_default"}], "parsers": [{"id": "native"}], "collection_id": "uploaded_files"}, tmp_path / "catalog.db", tmp_path / "state")
    try:
        assert service2.read_published(first.data["upload"]["resource_uri"]) == source.read_bytes()
    finally:
        service2.close()


def test_full_request_fingerprint_and_cross_space_fail_closed(tmp_path):
    service, engine, source, request = _fixture(tmp_path)
    try:
        result = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("one"), request=request))
        assert result.status == "ok"
        changed = AssetUploadRequest(request.asset_id, request.space_id, request.title, request.filename, "text/plain", request.binding_id, request.content_digest, request.idempotency_key)
        assert asyncio.run(service.stage(principal=_principal(), correlation=Correlation("two"), request=changed)).status == "error"
        foreign = Principal(subject_id="admin", scopes=("knowledge.admin", "knowledge.space:other"))
        assert asyncio.run(service.stage(principal=foreign, correlation=Correlation("three"), request=request)).status == "error"
    finally:
        service.close(); engine.dispose()


def test_existing_asset_identity_cannot_be_retyped_as_file(tmp_path):
    service, engine, source, request = _fixture(tmp_path)
    try:
        with Session(engine) as session, session.begin():
            session.add(KnowledgeAsset(id="asset_1", space_id="space_kb_default", kind="document", title="old",
                source_type="other", source_uri="knowledge://spaces/space_kb_default/assets/asset_1",
                revision="old", content_digest=request.content_digest, mime_type="text/plain"))
        result = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("collision"), request=request))
        assert result.status == "error"
    finally:
        service.close(); engine.dispose()


def test_parent_symlink_is_rejected(tmp_path):
    service, engine, source, request = _fixture(tmp_path)
    other = tmp_path / "outside"
    other.mkdir()
    try:
        source.unlink()
        source.write_bytes(b"hello file\n")
        # Replace the bound parent after service construction.
        moved = tmp_path / "real"
        source.rename(moved)
        source.parent.rename(tmp_path / "bound-real")
        source.parent.symlink_to(other, target_is_directory=True)
        result = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("symlink"), request=request))
        assert result.status == "error"
    finally:
        service.close(); engine.dispose()


def test_old_published_asset_survives_failed_replacement(tmp_path):
    service, engine, source, request = _fixture(tmp_path)
    try:
        first = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("one"), request=request))
        old_uri = first.data["upload"]["resource_uri"]
        source.write_bytes(b"changed but digest mismatch")
        changed = AssetUploadRequest(request.asset_id, request.space_id, request.title, "note.bin", request.mime_type, request.binding_id, "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest(), "retry")
        failed = asyncio.run(service.stage(principal=_principal(), correlation=Correlation("fail"), request=changed))
        assert failed.status == "error"
        assert service.read_published(old_uri) == b"hello file\n"
    finally:
        service.close(); engine.dispose()


def test_cancelled_parser_marks_job_failed_and_restart_retries(tmp_path, monkeypatch):
    service, engine, source, request = _fixture(tmp_path)
    entered = asyncio.Event()

    async def blocked(*args, **kwargs):
        entered.set()
        await asyncio.Event().wait()

    service.registry.parse = blocked

    async def run():
        task = asyncio.create_task(service.import_file(principal=_principal(), request=request))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    try:
        asyncio.run(run())
        with Session(engine) as session:
            job = session.scalar(select(KnowledgeIngestionJob))
            assert job is not None and job.status == "failed"
    finally:
        service.close(); engine.dispose()

    service2 = LocalFileService({"version": 1, "bindings": [{"id": "b", "path": str(source), "space_id": "space_kb_default"}], "parsers": [{"id": "native"}], "collection_id": "uploaded_files"}, tmp_path / "catalog.db", tmp_path / "state")
    try:
        result = asyncio.run(service2.stage(principal=_principal(), correlation=Correlation("retry"), request=request))
        assert result.status == "ok"
    finally:
        service2.close()
