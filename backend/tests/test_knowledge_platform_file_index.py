from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.metadata import KNOWLEDGE_METADATA
from knowledge_platform.catalog.models import KnowledgeSpace
from knowledge_platform.ingestion import AssetUploadRequest
from knowledge_platform.local.files import LocalFileService
from knowledge_platform.local.file_index import create_schema, replace_chunks, FileIndexProvider
from knowledge_platform.retrieval.ports import RetrievalProviderError


def digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


class Repo:
    catalog_revision = "sha256:" + "a" * 64

    def __init__(self, engine, assets):
        self.engine, self.assets = engine, assets

    def get_asset(self, *, asset_id):
        return self.assets.get(asset_id)


class Files:
    def __init__(self, body, current=True, original_id="raw_1", engine=None):
        self.body, self.current, self.original_id, self.engine = body, current, original_id, engine
        self.binding = None

    def read_published(self, uri):
        return self.body

    def is_current(self, asset_id):
        return self.current

    def normalized_original(self, asset_id):
        return self.original_id if asset_id == "md_1" else None



def setup(tmp_path: Path, body: str):
    engine = create_engine("sqlite:///" + str(tmp_path / "catalog.db"))
    create_schema(engine)
    original = {
        "id": "raw_1", "space_id": "space_1", "source_uri": "knowledge://spaces/space_1/assets/raw_1",
        "revision": digest("raw"), "content_digest": digest("raw"),
    }
    normalized = {
        "id": "md_1", "space_id": "space_1", "kind": "document",
        "source_uri": "knowledge://spaces/space_1/assets/md_1", "revision": digest(body),
        "content_digest": digest(body), "mime_type": "text/markdown",
    }
    return engine, Repo(engine, {"raw_1": original, "md_1": normalized}), Files(body.encode(), engine=engine)


@pytest.mark.asyncio
async def test_search_returns_normalized_asset_with_verified_quote_and_locator(tmp_path):
    body = "alpha introduction\n" + "x" * 1180 + "\nsecond alpha section"
    engine, repo, files = setup(tmp_path, body)
    with engine.begin() as connection:
        replace_chunks(connection, "space_1", "raw_1", "md_1", body, digest(body))
    result = await FileIndexProvider(repo, files).search(query="alpha", space_id="space_1", limit=5)
    assert len(result) == 1
    assert result[0].asset_id == "md_1"
    assert result[0].resource_uri.endswith("/md_1")
    assert result[0].locator["chunk_id"].startswith("md_1:")
    assert "alpha" in result[0].quote


@pytest.mark.asyncio
async def test_short_query_uses_bounded_instr_fallback_and_limit(tmp_path):
    body = "AI is useful; AI is bounded."
    engine, repo, files = setup(tmp_path, body)
    with engine.begin() as connection:
        replace_chunks(connection, "space_1", "raw_1", "md_1", body, digest(body))
    result = await FileIndexProvider(repo, files).search(query="AI", space_id="space_1", limit=1)
    assert len(result) == 1


@pytest.mark.asyncio
async def test_cross_space_and_stale_or_tampered_rows_are_not_returned(tmp_path):
    body = "alpha text"
    engine, repo, files = setup(tmp_path, body)
    with engine.begin() as connection:
        replace_chunks(connection, "space_1", "raw_1", "md_1", body, digest(body))
    assert await FileIndexProvider(repo, files).search(query="alpha", space_id="space_2", limit=5) == ()

    with engine.begin() as connection:
        connection.execute(text("DELETE FROM file_chunks_fts"))
        connection.execute(text("UPDATE file_chunks SET content='tampered' WHERE chunk_id='md_1:1'"))
        connection.execute(text("INSERT INTO file_chunks_fts(chunk_id,content) VALUES ('md_1:1','alpha')"))
    with pytest.raises(RetrievalProviderError, match="FTS binding"):
        await FileIndexProvider(repo, files).search(query="alpha", space_id="space_1", limit=5)


def test_replace_chunks_is_transactional_and_rollback_keeps_old_index(tmp_path):
    body = "original alpha"
    engine, repo, files = setup(tmp_path, body)
    with engine.begin() as connection:
        replace_chunks(connection, "space_1", "raw_1", "md_1", body, digest(body))
    with pytest.raises(RuntimeError):
        with engine.begin() as connection:
            replace_chunks(connection, "space_1", "raw_1", "md_1", "new beta", digest("new beta"))
            raise RuntimeError("rollback")
    with engine.connect() as connection:
        row = connection.execute(text("SELECT content FROM file_chunks WHERE chunk_id='md_1:1'")).scalar_one()
    assert row == body


@pytest.mark.asyncio
async def test_published_collection_with_deleted_chunks_fails_closed(tmp_path):
    body = "alpha text"
    engine, repo, files = setup(tmp_path, body)
    with engine.begin() as connection:
        replace_chunks(connection, "space_1", "raw_1", "md_1", body, digest(body))
    files.binding = {"asset_ids": ["md_1"], "chunk_count": 1}
    files.index_binding = lambda space_id: files.binding
    with engine.begin() as connection:
        connection.execute(text("DELETE FROM file_chunks_fts"))
        connection.execute(text("DELETE FROM file_chunks"))
    with pytest.raises(RetrievalProviderError, match="incomplete"):
        await FileIndexProvider(repo, files).search(query="alpha", space_id="space_1", limit=5)


def test_real_catalog_and_file_service_import_are_searchable(tmp_path):
    source = tmp_path / "note.md"
    body = b"authoritative alpha document\n"
    source.write_bytes(body)
    catalog = tmp_path / "catalog.db"
    engine = create_engine(f"sqlite:///{catalog}")
    KNOWLEDGE_METADATA.create_all(engine)
    with engine.begin() as connection:
        connection.execute(KnowledgeSpace.__table__.insert().values(
            id="space_1", name="Test", description="", permissions_json={}
        ))
    config = {
        "version": 1,
        "bindings": [{"id": "binding", "path": str(source), "space_id": "space_1"}],
        "parsers": [{"id": "native"}],
        "collection_id": "files",
    }
    service = LocalFileService(config, catalog, tmp_path / "state")
    try:
        request = AssetUploadRequest(
            "raw_1", "space_1", "Note", "note.md", "text/markdown", "binding",
            digest(body.decode()), "once",
        )
        from knowledge_contracts import Correlation, Principal
        principal = Principal(subject_id="admin", scopes=("knowledge.admin", "knowledge.space:space_1"))
        result = __import__("asyncio").run(service.stage(
            principal=principal, correlation=Correlation("integration"), request=request
        ))
        assert result.status == "ok"
        repository = SqliteCatalogQueryRepository(catalog)
        candidates = __import__("asyncio").run(FileIndexProvider(repository, service).search(
            query="alpha", space_id="space_1", limit=5
        ))
        assert [candidate.asset_id for candidate in candidates] == [result.data["upload"]["normalized_asset_id"]]
    finally:
        service.close()
        engine.dispose()
