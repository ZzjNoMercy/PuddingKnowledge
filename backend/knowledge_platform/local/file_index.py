"""Small Catalog-owned FTS index for published normalized file derivatives."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text

from knowledge_contracts import CitationCandidate
from knowledge_platform.catalog.query import CatalogQueryRepository
from knowledge_platform.retrieval.local import _portable_quote
from knowledge_platform.retrieval.ports import RetrievalProviderError

CHUNKER_VERSION = "file-chunker-v1"
CHUNK_SIZE = 1200
CHUNK_OVERLAP = 100
MAX_QUERY_LENGTH = 512
MAX_LIMIT = 50
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")


def create_schema(engine) -> None:
    """Create the independent chunk/FTS tables without altering Catalog tables."""
    with engine.begin() as connection:
        connection.exec_driver_sql(
            """CREATE TABLE IF NOT EXISTS file_chunks (
                id INTEGER PRIMARY KEY,
                space_id TEXT NOT NULL,
                original_id TEXT NOT NULL,
                asset_id TEXT NOT NULL,
                digest TEXT NOT NULL,
                chunker_version TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                start_offset INTEGER NOT NULL,
                end_offset INTEGER NOT NULL,
                chunk_id TEXT NOT NULL UNIQUE,
                content TEXT NOT NULL
            )"""
        )
        connection.exec_driver_sql(
            """CREATE VIRTUAL TABLE IF NOT EXISTS file_chunks_fts USING fts5(
                chunk_id UNINDEXED, content, tokenize='trigram'
            )"""
        )


def _check_id(value: str, label: str) -> None:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ValueError(f"{label} is invalid")


def _chunks(content: str) -> list[tuple[int, int, str]]:
    if not isinstance(content, str):
        raise TypeError("content must be text")
    if not content:
        return []
    result = []
    step = CHUNK_SIZE - CHUNK_OVERLAP
    start = 0
    ordinal = 1
    while start < len(content):
        end = min(len(content), start + CHUNK_SIZE)
        result.append((start, end, content[start:end]))
        if end == len(content):
            break
        start += step
        ordinal += 1
    return result


def replace_chunks(session, space_id: str, original_id: str, asset_id: str, content: str, digest: str) -> None:
    """Replace one original's normalized chunks inside the caller's transaction."""
    for value, label in ((space_id, "space_id"), (original_id, "original_id"), (asset_id, "asset_id")):
        _check_id(value, label)
    if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
        raise ValueError("digest is invalid")
    if "sha256:" + hashlib.sha256(content.encode("utf-8")).hexdigest() != digest:
        raise ValueError("digest does not match content")
    session.execute(text("DELETE FROM file_chunks_fts WHERE chunk_id IN (SELECT chunk_id FROM file_chunks WHERE space_id=:space AND original_id=:original)"), {"space": space_id, "original": original_id})
    session.execute(text("DELETE FROM file_chunks WHERE space_id=:space AND original_id=:original"), {"space": space_id, "original": original_id})
    for ordinal, (start, end, value) in enumerate(_chunks(content), 1):
        chunk_id = f"{asset_id}:{ordinal}"
        session.execute(text("""INSERT INTO file_chunks
            (space_id, original_id, asset_id, digest, chunker_version, ordinal, start_offset, end_offset, chunk_id, content)
            VALUES (:space,:original,:asset,:digest,:version,:ordinal,:start,:end,:chunk,:content)"""), {
                "space": space_id, "original": original_id, "asset": asset_id, "digest": digest,
                "version": CHUNKER_VERSION, "ordinal": ordinal, "start": start, "end": end,
                "chunk": chunk_id, "content": value,
            })
        session.execute(text("INSERT INTO file_chunks_fts(chunk_id,content) VALUES (:chunk,:content)"), {"chunk": chunk_id, "content": value})


class FileIndexProvider:
    def __init__(self, repository: CatalogQueryRepository, files_service: Any) -> None:
        self._repository = repository
        self._files = files_service
        self._engine = getattr(files_service, "engine", None) or getattr(repository, "engine", None)
        if self._engine is None:
            raise ValueError("files_service or repository must expose the Catalog engine")

    def _check_index_binding(self, space_id: str | None) -> None:
        checker = getattr(self._files, "index_binding", None)
        if checker is None:
            return
        try:
            binding = checker(space_id)
        except Exception as error:
            raise RetrievalProviderError("published file index binding is unavailable") from error
        with self._engine.connect() as connection:
            chunk_count, = connection.execute(
                text("SELECT count(*) FROM file_chunks WHERE (:space IS NULL OR space_id=:space)"), {"space": space_id}
            ).one()
            fts_count, = connection.execute(
                text("SELECT count(*) FROM file_chunks_fts f JOIN file_chunks c ON c.chunk_id=f.chunk_id WHERE (:space IS NULL OR c.space_id=:space)"),
                {"space": space_id},
            ).one()
            asset_ids = {
                str(row[0]) for row in connection.execute(
                    text("SELECT DISTINCT asset_id FROM file_chunks WHERE (:space IS NULL OR space_id=:space)"), {"space": space_id}
                )
            }
        if binding is None:
            if chunk_count or fts_count:
                raise RetrievalProviderError("published file index has residual chunks")
            return
        if not isinstance(binding, Mapping):
            raise RetrievalProviderError("published file index binding is invalid")
        expected_ids = binding.get("asset_ids")
        expected_count = binding.get("chunk_count")
        if (
            not isinstance(expected_ids, (list, tuple, set))
            or any(not isinstance(value, str) for value in expected_ids)
            or type(expected_count) is not int
            or expected_count < 0
            or asset_ids != set(expected_ids)
            or chunk_count != expected_count
            or fts_count != expected_count
        ):
            raise RetrievalProviderError("published file index is incomplete")

    def _published_asset(self, asset: Mapping[str, object], space_id: str | None):
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or not _ID.fullmatch(asset_id):
            return None
        if str(asset.get("kind") or "") not in {"document", "parsed_document"}:
            return None
        if space_id is not None and asset.get("space_id") != space_id:
            return None
        if not isinstance(asset.get("content_digest"), str) or not _DIGEST.fullmatch(asset["content_digest"]):
            return None
        try:
            original_id = self._files.normalized_original(asset_id)
        except Exception as error:
            raise RetrievalProviderError("normalized asset source binding is unavailable") from error
        if not isinstance(original_id, str) or not _ID.fullmatch(original_id):
            return None
        original = self._repository.get_asset(asset_id=original_id)
        if original is None or original.get("space_id") != asset.get("space_id"):
            raise RetrievalProviderError("normalized derivative source binding is invalid")
        try:
            if not self._files.is_current(str(asset_id)):
                return None
        except Exception as error:
            raise RetrievalProviderError("published normalized asset authorization is unavailable") from error
        return original_id

    async def search(self, *, query: str, space_id: str | None, limit: int):
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY_LENGTH:
            raise RetrievalProviderError("retrieval query is invalid")
        if type(limit) is not int or not 0 < limit <= MAX_LIMIT:
            raise RetrievalProviderError("retrieval limit is invalid")
        revision = self._repository.catalog_revision
        self._check_index_binding(space_id)
        rows = []
        with self._engine.connect() as connection:
            params = {"space": space_id, "limit": limit * 4}
            if len(query) < 3:
                rows = connection.execute(text("SELECT space_id,original_id,asset_id,digest,ordinal,start_offset,end_offset,chunk_id,content,NULL AS indexed_content FROM file_chunks WHERE (:space IS NULL OR space_id=:space) AND instr(lower(content),lower(:query))>0 ORDER BY id LIMIT :limit"), {**params, "query": query}).mappings().all()
            else:
                phrase = '"' + query.replace('"', '""') + '"'
                rows = connection.execute(text("SELECT c.space_id,c.original_id,c.asset_id,c.digest,c.ordinal,c.start_offset,c.end_offset,c.chunk_id,c.content,file_chunks_fts.content AS indexed_content FROM file_chunks_fts JOIN file_chunks c ON c.chunk_id=file_chunks_fts.chunk_id WHERE (:space IS NULL OR c.space_id=:space) AND file_chunks_fts MATCH :match ORDER BY bm25(file_chunks_fts) LIMIT :limit"), {**params, "match": phrase}).mappings().all()
        candidates = []
        seen = set()
        for row in rows:
            if row["asset_id"] in seen:
                continue
            if row["indexed_content"] is not None and row["indexed_content"] != row["content"]:
                raise RetrievalProviderError("file index FTS binding is invalid")
            if query.casefold() not in str(row["content"]).casefold():
                raise RetrievalProviderError("file index query binding is invalid")
            asset = self._repository.get_asset(asset_id=row["asset_id"])
            if asset is None or self._published_asset(asset, space_id) != row["original_id"]:
                continue
            if str(asset.get("content_digest") or "") != row["digest"]:
                raise RetrievalProviderError("file index digest binding is invalid")
            uri = str(asset.get("source_uri") or "")
            try:
                body = self._files.read_published(uri)
                if "sha256:" + hashlib.sha256(body).hexdigest() != row["digest"] or body.decode("utf-8")[row["start_offset"]:row["end_offset"]] != row["content"]:
                    raise RetrievalProviderError("file index chunk binding is invalid")
            except UnicodeDecodeError as error:
                raise RetrievalProviderError("published normalized asset is not UTF-8") from error
            candidates.append(CitationCandidate(asset_id=row["asset_id"], resource_uri=uri, quote=_portable_quote(row["content"]), locator={"chunk_id": row["chunk_id"]}, score=1.0))
            seen.add(row["asset_id"])
            if len(candidates) >= limit:
                break
        if self._repository.catalog_revision != revision:
            raise RetrievalProviderError("Catalog changed during retrieval")
        return tuple(candidates)
