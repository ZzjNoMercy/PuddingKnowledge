"""Catalog-bound candidate Index rebuild staging for the Platform Admin plane."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Evidence, Principal, Provenance, QueryError, QueryErrorCode, QueryResult

from .query import CatalogQueryRepository
from .vector_rebuild import VectorRebuildPlanError, build_text_chunks, build_vector_rebuild_manifest

_ID_RE = r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$"
_DIGEST_RE = r"^sha256:[0-9a-f]{64}$"
_MAX_SOURCE_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class IndexRebuildRequest:
    space_id: str
    collection_id: str
    collection_version: str
    capability: str
    provider_id: str
    idempotency_key: str


def _valid_id(value: Any) -> bool:
    import re

    return isinstance(value, str) and bool(re.fullmatch(_ID_RE, value))


def _valid_digest(value: Any) -> bool:
    import re

    return isinstance(value, str) and bool(re.fullmatch(_DIGEST_RE, value))


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _read_source(path: Path) -> bytes:
    if path.is_symlink():
        raise OSError("Index source binding must not be a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("Index source binding must be a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, _MAX_SOURCE_BYTES + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > _MAX_SOURCE_BYTES:
                raise ValueError("Index source exceeds the local bound")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _write_new(path: Path, content: bytes) -> None:
    if path.exists() or path.is_symlink():
        raise FileExistsError("Index candidate already exists")
    with tempfile.NamedTemporaryFile(mode="wb", prefix=".index-candidate-", dir=path.parent, delete=False) as stream:
        temporary = Path(stream.name)
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.link(temporary, path, follow_symlinks=False)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


class CatalogIndexRebuildService:
    """Build a verified, inactive local candidate from Catalog and host bindings."""

    def __init__(
        self,
        repository: CatalogQueryRepository,
        *,
        source_bindings: Mapping[str, Path],
        output_root: Path,
        max_chars: int = 1200,
        provider_id: str = "puddingclaw_platform_candidate_text",
        capability: str = "document_rag_query",
    ) -> None:
        self._repository = repository
        self._source_bindings = {str(asset_id): Path(path).expanduser().absolute() for asset_id, path in source_bindings.items()}
        self._root = output_root
        self._max_chars = max_chars
        if not _valid_id(provider_id) or not _valid_id(capability):
            raise ValueError("Index provider identity is invalid")
        self._provider_id = provider_id
        self._capability = capability

    @staticmethod
    def _authorized(principal: Principal, space_id: str) -> bool:
        scopes = set(principal.scopes)
        return (
            principal.tenant_id is None
            and bool({"knowledge.admin", "knowledge:admin"} & scopes)
            and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
        )

    def rebuild(self, *, principal: Principal, correlation: Correlation, request: IndexRebuildRequest) -> QueryResult:
        if not all(
            _valid_id(value)
            for value in (
                request.space_id,
                request.collection_id,
                request.collection_version,
                request.capability,
                request.provider_id,
                request.idempotency_key,
            )
        ):
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Index rebuild identity is invalid")
        if not self._authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Index rebuild requires Admin and Space scope")
        if request.provider_id != self._provider_id or request.capability != self._capability:
            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Index provider identity is not enabled by the host")
        try:
            root = self._root.expanduser().absolute()
            if root.exists() and root.is_symlink():
                raise OSError("Index staging root must not be a symlink")
            root.mkdir(parents=True, exist_ok=True)
            records_path = root / "index-rebuild-records.jsonl"
            lock_path = root / ".index-rebuild.lock"
            if lock_path.is_symlink():
                raise OSError("Index staging lock must not be a symlink")
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    if records_path.exists() and records_path.is_symlink():
                        raise OSError("Index staging records must not be a symlink")
                    records = [
                        json.loads(line)
                        for line in records_path.read_text(encoding="utf-8").splitlines()
                    ] if records_path.exists() else []
                    for record in records:
                        if record.get("idempotency_key") != request.idempotency_key:
                            continue
                        if (
                            record.get("space_id") != request.space_id
                            or record.get("collection_id") != request.collection_id
                            or record.get("provider_id") != request.provider_id
                        ):
                            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "Index idempotency key was reused with a different identity")
                        return self._result(correlation, record)

                    revision_before = self._repository.catalog_revision
                    collection = next(
                        (
                            item
                            for item in self._repository.list_collections(space_id=request.space_id)
                            if item.get("id") == request.collection_id
                            and item.get("version") == request.collection_version
                        ),
                        None,
                    )
                    if collection is None:
                        return _error(correlation, QueryErrorCode.NOT_FOUND, "Collection is not available in the requested Space")
                    assets = self._repository.list_assets(space_id=request.space_id)
                    manifest = build_vector_rebuild_manifest(
                        catalog_revision=revision_before,
                        collection=collection,
                        assets=assets,
                        capability=request.capability,
                        provider_collection_name=request.provider_id,
                    )
                    source_ids = {document.asset_id for document in manifest.documents}
                    if set(self._source_bindings) < source_ids:
                        return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Index source bindings are incomplete")
                    source_bytes = {
                        asset_id: _read_source(self._source_bindings[asset_id])
                        for asset_id in source_ids
                    }
                    chunks = build_text_chunks(
                        manifest=manifest,
                        source_bytes=source_bytes,
                        max_chars=self._max_chars,
                    )
                    revision_after = self._repository.catalog_revision
                    if revision_before != revision_after:
                        return _error(correlation, QueryErrorCode.INTERNAL_ERROR, "Catalog changed during Index rebuild")
                    job_id = "index_rebuild_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24]
                    candidate = root / "candidates" / job_id
                    candidate.mkdir(parents=True, exist_ok=False)
                    _write_new(candidate / "rebuild-manifest.json", (json.dumps(manifest.to_dict(), ensure_ascii=False, sort_keys=True) + "\n").encode())
                    _write_new(candidate / "chunks.json", (json.dumps([chunk.to_dict() for chunk in chunks], ensure_ascii=False, sort_keys=True) + "\n").encode())
                    record = {
                        "status": "candidate_ready",
                        "job_id": job_id,
                        "space_id": request.space_id,
                        "collection_id": request.collection_id,
                        "collection_version": request.collection_version,
                        "capability": request.capability,
                        "provider_id": request.provider_id,
                        "catalog_revision": revision_after,
                        "manifest_digest": manifest.manifest_digest(),
                        "chunk_count": len(chunks),
                        "active": False,
                        "activation_allowed": False,
                        "idempotency_key": request.idempotency_key,
                    }
                    with records_path.open("a", encoding="utf-8") as stream:
                        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                        stream.flush()
                        os.fsync(stream.fileno())
                    return self._result(correlation, record)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except VectorRebuildPlanError:
            return _error(correlation, QueryErrorCode.INDEX_NOT_READY, "Catalog cannot produce a verified Index candidate")
        except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Index rebuild staging is unavailable")

    @staticmethod
    def _result(correlation: Correlation, record: Mapping[str, Any]) -> QueryResult:
        data = {
            key: record[key]
            for key in (
                "job_id",
                "space_id",
                "collection_id",
                "collection_version",
                "capability",
                "provider_id",
                "catalog_revision",
                "manifest_digest",
                "chunk_count",
                "status",
                "active",
                "activation_allowed",
            )
        }
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="Index candidate 已完成校验并 staging，尚未写入或激活 Provider。",
            data={"index": data},
            evidence=(
                Evidence(
                    asset_id=str(record["collection_id"]),
                    resource_uri=f"knowledge://spaces/{record['space_id']}/collections/{record['collection_id']}/index-candidates/{record['job_id']}",
                    revision=str(record["manifest_digest"]),
                    matched_by=("catalog_snapshot", "digest_verified", "inactive_candidate"),
                ),
            ),
            provenance=Provenance(
                space_id=str(record["space_id"]),
                dataset_id=str(record["collection_id"]),
                dataset_version=str(record["collection_version"]),
                capability="knowledge_read",
                catalog_revision=str(record["catalog_revision"]),
            ),
        )


__all__ = ["CatalogIndexRebuildService", "IndexRebuildRequest"]
