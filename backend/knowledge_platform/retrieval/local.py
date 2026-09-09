"""Local-development providers with explicit, approved Asset path bindings."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Mapping
from contextlib import contextmanager
from pathlib import Path

from knowledge_contracts import BlobReadRequest, BlobReadResult, CitationCandidate
from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
from knowledge_platform.catalog.query import CatalogQueryRepository

from .ports import RetrievalProviderError

_EXTERNAL_URL_RE = re.compile(r"https?://[^\s)]+", re.IGNORECASE)
_FILE_URI_RE = re.compile(r"file://[^\s)]+", re.IGNORECASE)
_ABSOLUTE_PATH_RE = re.compile(
    r"(?i)(?:(?<![A-Za-z0-9_])/(?:Users|private|tmp|var|home|etc|opt|usr|root|mnt|Applications|System|Volumes)/[^\s)]+|(?<![A-Za-z0-9_])[A-Za-z]:[\\/][^\s)]+|\\\\[^\s)]+)",
)


def _portable_quote(value: str) -> str:
    """Keep useful lexical context while removing non-portable references."""

    value = _EXTERNAL_URL_RE.sub("[external-link]", value)
    value = _FILE_URI_RE.sub("[local-reference]", value)
    return _ABSOLUTE_PATH_RE.sub("[local-reference]", value)[:1200]


def _asset_from_uri(resource_uri: str) -> tuple[str, str]:
    parts = resource_uri.removeprefix("knowledge://").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "assets":
        raise RetrievalProviderError("Asset URI shape is invalid")
    return parts[1], parts[3]


def _safe_path(path: Path) -> Path:
    path = path.expanduser().absolute()
    if any(component in {"", ".", ".."} for component in path.parts[1:]):
        raise RetrievalProviderError("Asset path contains an unsafe component")
    cursor = path.parent
    while True:
        if cursor.is_symlink():
            raise RetrievalProviderError("Asset path contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    if path.is_symlink() or not path.is_file():
        raise RetrievalProviderError("Asset path is not a regular file")
    return path


@contextmanager
def _open_regular_file(path: Path):
    """Open a bound file through directory descriptors to close the TOCTOU gap."""

    candidate = _safe_path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    current_fd = os.open(os.sep, os.O_RDONLY | directory)
    descriptor: int | None = None
    try:
        for component in candidate.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(candidate.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise RetrievalProviderError("Asset path is not a regular file")
        yield descriptor
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(current_fd)


def _read_file(path: Path, *, start: int = 0, limit: int | None = None) -> tuple[bytes, str]:
    with _open_regular_file(path) as descriptor:
        size = os.fstat(descriptor).st_size
        full_digest = hashlib.sha256()
        offset = 0
        while True:
            chunk = os.pread(descriptor, 1024 * 1024, offset)
            if not chunk:
                break
            full_digest.update(chunk)
            offset += len(chunk)
        length = max(0, min(size - start, limit if limit is not None else size))
        content = os.pread(descriptor, length, start) if length else b""
        return content, f"sha256:{full_digest.hexdigest()}"


class LocalFilesystemBlobReader:
    """Read only explicitly bound local files; no Catalog path inference occurs."""

    def __init__(self, asset_paths: Mapping[str, Path]) -> None:
        self._asset_paths = {str(asset_id): Path(path) for asset_id, path in asset_paths.items()}

    async def read(self, request: BlobReadRequest) -> BlobReadResult:
        _space_id, asset_id = _asset_from_uri(request.resource_uri)
        path = self._asset_paths.get(asset_id)
        if path is None:
            raise RetrievalProviderError("Asset has no approved local path binding")
        limit = request.end - request.start if request.end is not None else MAX_BLOB_READ_BYTES
        content, asset_digest = _read_file(path, start=request.start, limit=limit)
        return BlobReadResult(
            resource_uri=request.resource_uri,
            content=content,
            content_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            start=request.start,
            end=request.start + len(content),
            asset_digest=asset_digest,
        )


class LocalFilesystemQueryResultBlobReader:
    """Read QueryResult artifacts only through an explicit URI-to-file map."""

    def __init__(self, artifact_paths: Mapping[str, Path]) -> None:
        self._artifact_paths = {str(resource_uri): Path(path) for resource_uri, path in artifact_paths.items()}

    async def read(self, request: BlobReadRequest) -> BlobReadResult:
        path = self._artifact_paths.get(request.resource_uri)
        if path is None:
            raise RetrievalProviderError("QueryResult artifact has no approved local path binding")
        limit = request.end - request.start if request.end is not None else MAX_BLOB_READ_BYTES
        content, artifact_digest = _read_file(path, start=request.start, limit=limit)
        return BlobReadResult(
            resource_uri=request.resource_uri,
            content=content,
            content_digest=f"sha256:{hashlib.sha256(content).hexdigest()}",
            start=request.start,
            end=request.start + len(content),
            asset_digest=artifact_digest,
        )


class LocalDocumentRetrievalProvider:
    """Deterministic lexical provider for local fixtures and approved files."""

    def __init__(self, *, catalog: CatalogQueryRepository, asset_paths: Mapping[str, Path]) -> None:
        self._catalog = catalog
        self._asset_paths = {str(asset_id): Path(path) for asset_id, path in asset_paths.items()}

    async def search(
        self, *, query: str, space_id: str | None, limit: int
    ) -> tuple[CitationCandidate, ...]:
        query_folded = query.casefold()
        candidates: list[CitationCandidate] = []
        for asset in self._catalog.list_assets(space_id=space_id):
            asset_id = str(asset.get("id") or "")
            path = self._asset_paths.get(asset_id)
            if path is None:
                continue
            content, asset_digest = _read_file(path, limit=2 * 1024 * 1024)
            expected_digest = str(asset.get("content_digest") or asset.get("revision") or "")
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", expected_digest) or expected_digest != asset_digest:
                raise RetrievalProviderError("Asset file digest does not match Catalog binding")
            try:
                decoded = content.decode("utf-8")
            except UnicodeDecodeError:
                decoded = ""
            if decoded and any(ord(character) < 32 and character not in "\t\n\r" for character in decoded):
                decoded = ""
            text = decoded
            haystack = "\n".join((str(asset.get("title") or ""), str(asset.get("description") or ""), text))
            position = haystack.casefold().find(query_folded)
            if position < 0:
                continue
            quote = _portable_quote(haystack[max(0, position - 120) : position + len(query) + 108])
            candidates.append(
                CitationCandidate(
                    asset_id=asset_id,
                    resource_uri=str(asset.get("source_uri") or ""),
                    quote=quote,
                    locator={"chunk_id": f"local:{position}"},
                    score=1.0,
                )
            )
            if len(candidates) >= limit:
                break
        return tuple(candidates)


class LocalPublishedWikiProvider(LocalDocumentRetrievalProvider):
    """The local v1 Wiki read provider; only published Wiki Assets are indexed.

    The path map is still host-owned and explicit.  Filtering by the Catalog
    kind prevents a caller from accidentally presenting an ordinary document
    binding as Published Wiki just because its bytes happen to be Markdown.
    """

    async def search(
        self, *, query: str, space_id: str | None, limit: int
    ) -> tuple[CitationCandidate, ...]:
        original_assets = self._catalog.list_assets(space_id=space_id)
        wiki_assets = [
            asset for asset in original_assets if str(asset.get("kind") or "") == "wiki_page"
        ]
        if not wiki_assets:
            return ()
        # Keep the inherited digest, path, and bounded-text checks while
        # ensuring its Catalog view contains only Published Wiki Assets.
        class _WikiCatalog:
            def __init__(self, assets: list[Mapping[str, object]]) -> None:
                self._assets = assets

            def list_assets(self, *, space_id: str | None = None):
                return [
                    asset for asset in self._assets
                    if space_id is None or str(asset.get("space_id") or "") == space_id
                ]

        return await LocalDocumentRetrievalProvider(
            catalog=_WikiCatalog(wiki_assets),
            asset_paths=self._asset_paths,
        ).search(query=query, space_id=space_id, limit=limit)
