"""Portable, deterministic inputs for rebuilding a derived Vector index."""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CHECKPOINT_SIGNATURE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:=|+\-]{0,511}$")


class VectorRebuildPlanError(ValueError):
    """Authoritative inputs cannot produce a safe derived-index plan."""


@dataclass(frozen=True, slots=True)
class VectorRebuildDocument:
    """One Catalog Asset's stable identity in a candidate Vector index."""

    asset_id: str
    source_revision: str
    content_digest: str
    provider_document_id: str

    def __post_init__(self) -> None:
        for value, label in ((self.asset_id, "asset_id"), (self.source_revision, "source_revision")):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise VectorRebuildPlanError(f"vector rebuild {label} is invalid")
        if not isinstance(self.content_digest, str) or not _DIGEST_RE.fullmatch(self.content_digest):
            raise VectorRebuildPlanError("vector rebuild content_digest is invalid")
        if self.provider_document_id != self.asset_id:
            raise VectorRebuildPlanError("vector rebuild provider identity must equal Catalog asset_id")

    def to_dict(self) -> dict[str, str]:
        return {
            "asset_id": self.asset_id,
            "source_revision": self.source_revision,
            "content_digest": self.content_digest,
            "provider_document_id": self.provider_document_id,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> VectorRebuildDocument:
        if set(value) != {"asset_id", "source_revision", "content_digest", "provider_document_id"}:
            raise VectorRebuildPlanError("vector rebuild document fields are invalid")
        return cls(
            asset_id=value["asset_id"],
            source_revision=value["source_revision"],
            content_digest=value["content_digest"],
            provider_document_id=value["provider_document_id"],
        )


@dataclass(frozen=True, slots=True)
class VectorRebuildManifest:
    """Content-addressed candidate input; paths and credentials never cross this boundary."""

    space_id: str
    collection_id: str
    collection_version: str
    capability: str
    catalog_revision: str
    provider_collection_name: str
    chunker_version: str
    documents: tuple[VectorRebuildDocument, ...]

    def __post_init__(self) -> None:
        for value, label in (
            (self.space_id, "space_id"),
            (self.collection_id, "collection_id"),
            (self.collection_version, "collection_version"),
            (self.capability, "capability"),
            (self.provider_collection_name, "provider_collection_name"),
            (self.chunker_version, "chunker_version"),
        ):
            if not isinstance(value, str) or not _ID_RE.fullmatch(value):
                raise VectorRebuildPlanError(f"vector rebuild {label} is invalid")
        if not isinstance(self.catalog_revision, str) or not _DIGEST_RE.fullmatch(self.catalog_revision):
            raise VectorRebuildPlanError("vector rebuild catalog_revision is invalid")
        if not isinstance(self.documents, tuple) or not self.documents:
            raise VectorRebuildPlanError("vector rebuild manifest must contain documents")
        ids = [item.asset_id for item in self.documents]
        if len(ids) != len(set(ids)):
            raise VectorRebuildPlanError("vector rebuild document identities are duplicated")

    def to_dict(self) -> dict[str, object]:
        return {
            "space_id": self.space_id,
            "collection_id": self.collection_id,
            "collection_version": self.collection_version,
            "capability": self.capability,
            "catalog_revision": self.catalog_revision,
            "provider_collection_name": self.provider_collection_name,
            "chunker_version": self.chunker_version,
            "documents": [item.to_dict() for item in self.documents],
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> VectorRebuildManifest:
        required = {
            "space_id",
            "collection_id",
            "collection_version",
            "capability",
            "catalog_revision",
            "provider_collection_name",
            "chunker_version",
            "documents",
        }
        if set(value) != required or not isinstance(value["documents"], list):
            raise VectorRebuildPlanError("vector rebuild manifest fields are invalid")
        documents = value["documents"]
        if any(not isinstance(item, Mapping) for item in documents):
            raise VectorRebuildPlanError("vector rebuild manifest documents are invalid")
        return cls(
            space_id=value["space_id"],
            collection_id=value["collection_id"],
            collection_version=value["collection_version"],
            capability=value["capability"],
            catalog_revision=value["catalog_revision"],
            provider_collection_name=value["provider_collection_name"],
            chunker_version=value["chunker_version"],
            documents=tuple(VectorRebuildDocument.from_dict(item) for item in documents),
        )

    def manifest_digest(self) -> str:
        import json

        encoded = json.dumps(self.to_dict(), ensure_ascii=True, sort_keys=True, separators=(",", ":"))
        return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class VectorRebuildChunk:
    """A deterministic, path-free text row ready for provider embedding."""

    asset_id: str
    chunk_id: str
    ordinal: int
    text: str
    content_digest: str
    source_revision: str

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.asset_id) or not _ID_RE.fullmatch(self.chunk_id):
            raise VectorRebuildPlanError("vector rebuild chunk identity is invalid")
        if type(self.ordinal) is not int or self.ordinal < 1:
            raise VectorRebuildPlanError("vector rebuild chunk ordinal is invalid")
        if not isinstance(self.text, str) or not self.text.strip() or len(self.text) > 2000:
            raise VectorRebuildPlanError("vector rebuild chunk text is invalid")
        if not _DIGEST_RE.fullmatch(self.content_digest) or not _ID_RE.fullmatch(self.source_revision):
            raise VectorRebuildPlanError("vector rebuild chunk provenance is invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "asset_id": self.asset_id,
            "provider_document_id": self.asset_id,
            "chunk_id": self.chunk_id,
            "ordinal": self.ordinal,
            "text": self.text,
            "content_digest": self.content_digest,
            "source_revision": self.source_revision,
        }


@dataclass(frozen=True, slots=True)
class VectorEmbeddingRow:
    """A validated candidate row; only this shape may cross to a Vector writer."""

    chunk: VectorRebuildChunk
    embedding: tuple[float, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.embedding, tuple) or not self.embedding:
            raise VectorRebuildPlanError("vector embedding is empty")
        if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in self.embedding):
            raise VectorRebuildPlanError("vector embedding contains a non-finite value")

    def to_dict(self) -> dict[str, object]:
        return {
            "id": self.chunk.chunk_id,
            "doc_id": self.chunk.asset_id,
            "text": self.chunk.text,
            "embedding": list(self.embedding),
            "content_digest": self.chunk.content_digest,
            "source_revision": self.chunk.source_revision,
        }


def build_vector_rebuild_manifest(
    *,
    catalog_revision: str,
    collection: Mapping[str, object],
    assets: Sequence[Mapping[str, object]],
    capability: str = "document_rag_query",
    provider_collection_name: str = "puddingclaw_platform_candidate_text",
    chunker_version: str = "platform-text-v1",
) -> VectorRebuildManifest:
    """Build stable candidate identities from a Catalog snapshot.

    The function deliberately does not accept a physical path or an existing
    provider row ID.  A new index must be reproducible from the Catalog Asset
    identity and authoritative content digest, not from copied Milvus state.
    """

    if not isinstance(collection, Mapping):
        raise VectorRebuildPlanError("vector rebuild collection is invalid")
    space_id = str(collection.get("space_id") or "")
    collection_id = str(collection.get("id") or "")
    collection_version = str(collection.get("version") or "")
    capabilities = collection.get("capabilities")
    if not isinstance(capabilities, list) or capability not in capabilities:
        raise VectorRebuildPlanError("vector rebuild capability is not declared by Collection")
    collection_asset_ids = collection.get("asset_ids")
    if not isinstance(collection_asset_ids, list) or any(not isinstance(item, str) for item in collection_asset_ids):
        raise VectorRebuildPlanError("vector rebuild Collection asset_ids are invalid")
    selected = set(collection_asset_ids)
    documents: list[VectorRebuildDocument] = []
    seen: set[str] = set()
    for asset in assets:
        asset_id = asset.get("id")
        if not isinstance(asset_id, str) or asset_id not in selected:
            continue
        if asset_id in seen:
            raise VectorRebuildPlanError("vector rebuild Catalog assets are duplicated")
        seen.add(asset_id)
        source_revision = asset.get("revision")
        content_digest = asset.get("content_digest")
        if not isinstance(source_revision, str) or not isinstance(content_digest, str):
            raise VectorRebuildPlanError("vector rebuild Asset revision/digest is missing")
        documents.append(VectorRebuildDocument(asset_id, source_revision, content_digest, asset_id))
    if seen != selected:
        raise VectorRebuildPlanError("vector rebuild Collection references missing Catalog assets")
    return VectorRebuildManifest(
        space_id=space_id,
        collection_id=collection_id,
        collection_version=collection_version,
        capability=capability,
        catalog_revision=catalog_revision,
        provider_collection_name=provider_collection_name,
        chunker_version=chunker_version,
        documents=tuple(sorted(documents, key=lambda item: item.asset_id)),
    )


def build_text_chunks(
    *,
    manifest: VectorRebuildManifest,
    source_bytes: Mapping[str, bytes],
    max_chars: int = 1200,
) -> tuple[VectorRebuildChunk, ...]:
    """Chunk every manifest document from bytes whose digest is verified.

    The source map is host-owned and never serialized here.  A missing asset,
    extra source, decode error, digest mismatch, or unsafe control character
    stops the build before any provider write can occur.
    """

    if type(max_chars) is not int or not 200 <= max_chars <= 2000:
        raise VectorRebuildPlanError("vector rebuild max_chars is invalid")
    expected_ids = {item.asset_id for item in manifest.documents}
    if set(source_bytes) != expected_ids:
        raise VectorRebuildPlanError("vector rebuild source map does not match manifest")
    chunks: list[VectorRebuildChunk] = []
    for document in manifest.documents:
        raw = source_bytes[document.asset_id]
        if not isinstance(raw, bytes) or "sha256:" + hashlib.sha256(raw).hexdigest() != document.content_digest:
            raise VectorRebuildPlanError("vector rebuild source digest does not match manifest")
        extracted_from_pdf = False
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError as error:
            if not raw.startswith(b"%PDF-"):
                raise VectorRebuildPlanError("vector rebuild source is not UTF-8 text") from error
            try:
                pdftotext = shutil.which("pdftotext")
                if pdftotext:
                    extracted = subprocess.run(
                        [pdftotext, "-layout", "-", "-"],
                        input=raw,
                        capture_output=True,
                        check=False,
                        timeout=90,
                    )
                    text = extracted.stdout.decode("utf-8", errors="replace") if extracted.returncode == 0 else ""
                else:
                    text = ""
                if not text.strip():
                    from pypdf import PdfReader

                    pages = PdfReader(io.BytesIO(raw)).pages
                    text = "\n\n".join(page.extract_text() or "" for page in pages)
                extracted_from_pdf = True
            except Exception as pdf_error:
                raise VectorRebuildPlanError("vector rebuild PDF extraction failed") from pdf_error
        if extracted_from_pdf:
            text = "".join(character if ord(character) >= 32 or character in "\t\n\r\f" else " " for character in text)
        if any(ord(character) < 32 and character not in "\t\n\r\f" for character in text):
            raise VectorRebuildPlanError("vector rebuild source contains unsafe control characters")
        normalized = text.replace("\r\n", "\n").replace("\r", "\n").replace("\f", "\n\n").strip()
        if not normalized:
            raise VectorRebuildPlanError("vector rebuild source is empty")
        start = 0
        ordinal = 1
        while start < len(normalized):
            end = min(len(normalized), start + max_chars)
            if end < len(normalized):
                boundary = max(normalized.rfind("\n\n", start, end), normalized.rfind("\n", start, end))
                if boundary > start + max_chars // 3:
                    end = boundary
            value = normalized[start:end].strip()
            if value:
                chunks.append(
                    VectorRebuildChunk(
                        asset_id=document.asset_id,
                        chunk_id=f"{document.asset_id}:chunk_{ordinal}",
                        ordinal=ordinal,
                        text=value,
                        content_digest=document.content_digest,
                        source_revision=document.source_revision,
                    )
                )
                ordinal += 1
            start = end
    if not chunks:
        raise VectorRebuildPlanError("vector rebuild produced no chunks")
    return tuple(chunks)


def build_embedded_rows(
    *,
    chunks: Sequence[VectorRebuildChunk],
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    dimension: int,
    batch_size: int = 32,
) -> tuple[VectorEmbeddingRow, ...]:
    """Embed verified chunks in bounded batches without performing provider I/O here."""

    if not chunks or any(not isinstance(chunk, VectorRebuildChunk) for chunk in chunks):
        raise VectorRebuildPlanError("vector embedding chunks are invalid")
    if type(dimension) is not int or not 1 <= dimension <= 16_384:
        raise VectorRebuildPlanError("vector embedding dimension is invalid")
    if type(batch_size) is not int or not 1 <= batch_size <= 256:
        raise VectorRebuildPlanError("vector embedding batch_size is invalid")
    rows: list[VectorEmbeddingRow] = []
    for start in range(0, len(chunks), batch_size):
        batch = chunks[start : start + batch_size]
        try:
            vectors = list(embed([chunk.text for chunk in batch]))
        except Exception as error:
            raise VectorRebuildPlanError("vector embedding provider failed") from error
        if len(vectors) != len(batch):
            raise VectorRebuildPlanError("vector embedding count does not match chunks")
        for chunk, vector in zip(batch, vectors, strict=True):
            if not isinstance(vector, Sequence) or len(vector) != dimension:
                raise VectorRebuildPlanError("vector embedding dimension does not match manifest")
            if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in vector):
                raise VectorRebuildPlanError("vector embedding contains a non-finite value")
            rows.append(VectorEmbeddingRow(chunk=chunk, embedding=tuple(float(value) for value in vector)))
    return tuple(rows)


def build_embedded_rows_checkpointed(
    *,
    chunks: Sequence[VectorRebuildChunk],
    embed: Callable[[Sequence[str]], Sequence[Sequence[float]]],
    dimension: int,
    batch_size: int,
    checkpoint_path: Path,
    manifest_digest: str,
    provider_signature: str,
) -> tuple[VectorEmbeddingRow, ...]:
    """Resume bounded embedding batches from an atomically written local checkpoint.

    The checkpoint contains only chunk identity/provenance and vectors.  It is
    never sufficient for activation by itself: all chunks still need to be
    verified and the caller must perform the normal candidate write checks.
    """

    if not chunks or any(not isinstance(chunk, VectorRebuildChunk) for chunk in chunks):
        raise VectorRebuildPlanError("vector embedding chunks are invalid")
    if type(dimension) is not int or not 1 <= dimension <= 16_384:
        raise VectorRebuildPlanError("vector embedding dimension is invalid")
    if type(batch_size) is not int or not 1 <= batch_size <= 256:
        raise VectorRebuildPlanError("vector embedding batch_size is invalid")
    if not isinstance(checkpoint_path, Path):
        raise VectorRebuildPlanError("vector embedding checkpoint path is invalid")
    if not isinstance(manifest_digest, str) or not _DIGEST_RE.fullmatch(manifest_digest):
        raise VectorRebuildPlanError("vector embedding checkpoint manifest digest is invalid")
    if (
        not isinstance(provider_signature, str)
        or not provider_signature.strip()
        or not _CHECKPOINT_SIGNATURE_RE.fullmatch(provider_signature)
    ):
        raise VectorRebuildPlanError("vector embedding checkpoint provider signature is invalid")

    checkpoint_path = checkpoint_path.expanduser().absolute()
    current = checkpoint_path
    while current != current.parent:
        if current.is_symlink():
            raise VectorRebuildPlanError("vector embedding checkpoint path contains a symlink")
        current = current.parent
    checkpoint_path.parent.mkdir(parents=True, exist_ok=True)
    if checkpoint_path.exists() and (checkpoint_path.is_symlink() or not checkpoint_path.is_file()):
        raise VectorRebuildPlanError("vector embedding checkpoint is not a regular file")

    chunks_by_id = {chunk.chunk_id: chunk for chunk in chunks}
    completed: dict[str, tuple[float, ...]] = {}
    if checkpoint_path.exists():
        try:
            payload = json.loads(checkpoint_path.read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or set(payload) != {
                "format",
                "manifest_digest",
                "provider_signature",
                "dimension",
                "rows",
            }:
                raise ValueError("checkpoint fields are invalid")
            if (
                payload["format"] != "agent-knowledge-platform-vector-embedding-checkpoint/v1"
                or payload["manifest_digest"] != manifest_digest
                or payload["provider_signature"] != provider_signature
                or type(payload["dimension"]) is not int
                or payload["dimension"] != dimension
                or not isinstance(payload["rows"], list)
                or len(payload["rows"]) > len(chunks)
            ):
                raise ValueError("checkpoint identity is invalid")
            for raw_row in payload["rows"]:
                if not isinstance(raw_row, dict) or set(raw_row) != {
                    "chunk_id",
                    "asset_id",
                    "source_revision",
                    "content_digest",
                    "embedding",
                }:
                    raise ValueError("checkpoint row fields are invalid")
                chunk_id = raw_row["chunk_id"]
                chunk = chunks_by_id.get(chunk_id)
                vector = raw_row["embedding"]
                if (
                    chunk is None
                    or raw_row["asset_id"] != chunk.asset_id
                    or raw_row["source_revision"] != chunk.source_revision
                    or raw_row["content_digest"] != chunk.content_digest
                    or not isinstance(vector, list)
                    or len(vector) != dimension
                    or any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in vector)
                    or chunk_id in completed
                ):
                    raise ValueError("checkpoint row provenance or vector is invalid")
                completed[chunk_id] = tuple(float(value) for value in vector)
        except Exception as error:
            raise VectorRebuildPlanError("vector embedding checkpoint cannot be reused") from error

    def write_checkpoint() -> None:
        rows = [
            {
                "chunk_id": chunk.chunk_id,
                "asset_id": chunk.asset_id,
                "source_revision": chunk.source_revision,
                "content_digest": chunk.content_digest,
                "embedding": list(completed[chunk.chunk_id]),
            }
            for chunk in chunks
            if chunk.chunk_id in completed
        ]
        payload = {
            "format": "agent-knowledge-platform-vector-embedding-checkpoint/v1",
            "manifest_digest": manifest_digest,
            "provider_signature": provider_signature,
            "dimension": dimension,
            "rows": rows,
        }
        temporary = checkpoint_path.with_name(checkpoint_path.name + ".tmp")
        if temporary.is_symlink() or (temporary.exists() and not temporary.is_file()):
            raise VectorRebuildPlanError("vector embedding checkpoint temporary path is unsafe")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")
        temporary.replace(checkpoint_path)

    missing = [chunk for chunk in chunks if chunk.chunk_id not in completed]
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        try:
            vectors = list(embed([chunk.text for chunk in batch]))
        except Exception as error:
            raise VectorRebuildPlanError("vector embedding provider failed") from error
        if len(vectors) != len(batch):
            raise VectorRebuildPlanError("vector embedding count does not match chunks")
        for chunk, vector in zip(batch, vectors, strict=True):
            if not isinstance(vector, Sequence) or len(vector) != dimension:
                raise VectorRebuildPlanError("vector embedding dimension does not match checkpoint")
            if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in vector):
                raise VectorRebuildPlanError("vector embedding contains a non-finite value")
            completed[chunk.chunk_id] = tuple(float(value) for value in vector)
        write_checkpoint()

    return tuple(VectorEmbeddingRow(chunk=chunk, embedding=completed[chunk.chunk_id]) for chunk in chunks)


__all__ = [
    "VectorRebuildDocument",
    "VectorRebuildChunk",
    "VectorEmbeddingRow",
    "VectorRebuildManifest",
    "VectorRebuildPlanError",
    "build_text_chunks",
    "build_embedded_rows",
    "build_embedded_rows_checkpointed",
    "build_vector_rebuild_manifest",
]
