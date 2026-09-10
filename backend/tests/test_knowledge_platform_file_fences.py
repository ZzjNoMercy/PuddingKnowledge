from __future__ import annotations

import asyncio
import hashlib
from pathlib import Path

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeIngestionJob, KnowledgeSpace
from knowledge_platform.ingestion import AssetUploadRequest
from knowledge_platform.local.files import LocalFileService


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def test_lease_replacement_cannot_publish_assets_or_chunks(tmp_path: Path):
    content = b"lease fence alpha\n"
    source = tmp_path / "note.md"
    source.write_bytes(content)
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.execute(KnowledgeSpace.__table__.insert().values(
            id="space_1", name="Fence test", description="", permissions_json={}
        ))

    service = LocalFileService(
        {
            "version": 1,
            "bindings": [{"id": "binding", "path": str(source), "space_id": "space_1"}],
            "parsers": [{"id": "native"}],
            "collection_id": "files",
        },
        catalog,
        tmp_path / "state",
    )
    request = AssetUploadRequest(
        "raw_1", "space_1", "Note", "note.md", "text/markdown", "binding",
        _digest(content), "lease-fence",
    )
    principal = Principal(subject_id="admin", scopes=("knowledge.admin", "knowledge.space:space_1"))
    original_parse = service.registry.parse

    async def steal_lease(filename, payload, *, parser_id=None):
        parsed = await original_parse(filename, payload, parser_id=parser_id)
        with service.engine.begin() as connection:
            connection.execute(
                text("UPDATE knowledge_ingestion_jobs SET lease_owner='other-worker' WHERE id LIKE 'file_job_%'"),
            )
        return parsed

    service.registry.parse = steal_lease
    try:
        result = asyncio.run(service.stage(
            principal=principal,
            correlation=Correlation("lease-fence"),
            request=request,
        ))
        assert result.status == "error"
        with Session(service.engine) as session:
            assert session.query(KnowledgeAsset).count() == 0
            job = session.query(KnowledgeIngestionJob).one()
            assert job.status == "running"
            assert job.lease_owner == "other-worker"
        with service.engine.connect() as connection:
            assert connection.execute(text("SELECT count(*) FROM file_chunks")).scalar_one() == 0
            assert connection.execute(text("SELECT count(*) FROM file_chunks_fts")).scalar_one() == 0
    finally:
        service.close()
        engine.dispose()
