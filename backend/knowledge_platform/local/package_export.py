"""Fail-closed, principal-scoped export of immutable Knowledge Packages."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sqlite3
import tempfile
from urllib.parse import quote
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from knowledge_contracts import MAX_BLOB_READ_BYTES, BlobReadRequest, Correlation, Principal
from knowledge_platform.package import (
    CatalogPackageSnapshot,
    KnowledgePackageBuilder,
    PackageBuildError,
    WorkspaceMaterializer,
    export_package_zip,
    validate_package,
)
from knowledge_platform.package.builder import _open_safe_parent_directory


class PackageExportError(ValueError):
    """The requested Package cannot be published completely and safely."""


class CatalogRepository(Protocol):
    def read_package_snapshot(self) -> CatalogPackageSnapshot: ...

    @property
    def catalog_revision(self) -> str: ...


class BlobReader(Protocol):
    async def read(self, request: BlobReadRequest) -> Any: ...


@dataclass(frozen=True, slots=True)
class PackageExportResult:
    output_zip: Path
    package_revision: str
    catalog_revision: str
    workspace_root: Path | None = None


def _scopes(principal: Principal) -> set[str]:
    return {str(value).casefold() for value in getattr(principal, "scopes", ())}


def _space_scope(principal: Principal) -> set[str] | None:
    for name in ("space_ids", "spaces", "space_scope", "allowed_space_ids"):
        value = getattr(principal, name, None)
        if value is not None:
            return {str(item) for item in value}
    scoped = set()
    for value in getattr(principal, "scopes", ()):
        text = str(value)
        for prefix in ("knowledge.space:", "knowledge:space:"):
            if text.startswith(prefix) and text[len(prefix):]:
                scoped.add(text[len(prefix):])
    return scoped or None


def _selected_collections(snapshot: CatalogPackageSnapshot, requested: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, Any]], str]:
    if not requested or any(not isinstance(item, Mapping) for item in requested):
        raise PackageExportError("at least one collection id/version is required")
    if any(set(item) != {"id", "version"} or type(item.get("id")) is not str or type(item.get("version")) is not str for item in requested):
        raise PackageExportError("collection selection must contain exactly string id/version")
    by_key = {(str(item.get("id")), str(item.get("version"))): item for item in snapshot.collections}
    chosen: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for selection in requested:
        key = (str(selection.get("id") or ""), str(selection.get("version") or ""))
        if not key[0] or not key[1] or key in seen:
            raise PackageExportError("collection selection must contain unique id/version pairs")
        collection = by_key.get(key)
        if collection is None:
            raise PackageExportError(f"selected collection is unavailable: {key[0]}@{key[1]}")
        chosen.append(dict(collection))
        seen.add(key)
    spaces = {str(item.get("id")): item for item in snapshot.spaces}
    space_ids = {str(item.get("space_id") or "") for item in chosen}
    if any(space_id not in spaces for space_id in space_ids):
        raise PackageExportError("selected collection references an unavailable Space")
    return chosen, next(iter(space_ids)) if len(space_ids) == 1 else ""


def _record_derivative_assets(assets: Mapping[str, Mapping[str, Any]], base: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Read explicit derivative metadata; never infer a path from a filename."""
    raw = base.get("derivatives", base.get("derivative_assets", ()))
    records: list[dict[str, Any]] = []
    if isinstance(raw, Mapping):
        raw = [dict(value, kind=key) if isinstance(value, Mapping) else {"kind": key, "content_digest": value} for key, value in raw.items()]
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
        return records
    for item in raw:
        if not isinstance(item, Mapping):
            raise PackageExportError(f"asset {base.get('id')} has malformed derivative metadata")
        candidate = dict(item)
        derivative_id = str(candidate.get("id") or f"{base.get('id')}__{candidate.get('kind') or ''}")
        kind = str(candidate.get("kind") or "")
        uri = str(candidate.get("source_uri") or "")
        if not kind or not uri or not candidate.get("content_digest"):
            raise PackageExportError(f"asset {base.get('id')} has incomplete derivative metadata")
        records.append({**base, **candidate, "id": derivative_id, "source_uri": uri, "kind": candidate.get("kind", "derivative"), "space_id": base.get("space_id")})
    return records


class PackageExportService:
    """Export selected Catalog collections using an already-authorized BlobReader."""

    def __init__(self, repository: CatalogRepository, reader: BlobReader, *, builder: KnowledgePackageBuilder | None = None) -> None:
        self.repository = repository
        self.reader = reader
        self.builder = builder or KnowledgePackageBuilder()

    def _catalog_relations(self, asset_ids: set[str], assets_by_id: Mapping[str, Mapping[str, Any]]) -> set[str]:
        """Resolve only persisted metadata relations whose target is a real same-Space asset."""
        database_path = getattr(self.repository, "_database_path", None)
        if database_path is None:
            return set()
        try:
            connection = sqlite3.connect(f"file:{quote(str(Path(database_path).absolute()), safe='/')}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            table = connection.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='knowledge_assets'").fetchone()
            if table is None:
                connection.close()
                return set()
            columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(knowledge_assets)")}
            if "metadata_json" not in columns:
                connection.close()
                return set()
            rows = connection.execute("SELECT id, space_id, metadata_json FROM knowledge_assets").fetchall()
        except (OSError, sqlite3.Error) as error:
            raise PackageExportError("Catalog asset relation read failed") from error
        finally:
            try:
                connection.close()
            except UnboundLocalError:
                pass
        metadata_by_id: dict[str, dict[str, Any]] = {}
        for row in rows:
            try:
                value = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                raise PackageExportError("Catalog asset metadata is invalid")
            if not isinstance(value, dict):
                raise PackageExportError("Catalog asset metadata is invalid")
            metadata_by_id[str(row["id"])] = value
        result = set(asset_ids)
        queue = list(asset_ids)
        while queue:
            base_id = queue.pop()
            base = assets_by_id.get(base_id)
            if base is None:
                raise PackageExportError(f"Catalog relation references missing asset: {base_id}")
            metadata = metadata_by_id.get(base_id, {})
            for field in ("original_asset_id", "derivatives", "published_asset_ids"):
                if field in metadata:
                    base[field] = metadata[field]
            candidates: list[Any] = []
            for key in ("original_asset_id", "derivative_asset_ids", "published_asset_ids"):
                value = metadata.get(key)
                candidates.extend(value if isinstance(value, list) else [value] if value else [])
            derivatives = metadata.get("derivatives")
            if isinstance(derivatives, dict):
                candidates.extend(value.get("asset_id") if isinstance(value, Mapping) else value for value in derivatives.values())
            for candidate in candidates:
                target_id = str(candidate or "")
                target = assets_by_id.get(target_id)
                if not target:
                    raise PackageExportError(f"Catalog relation references missing asset: {target_id}")
                if str(target.get("space_id")) != str(base.get("space_id")):
                    raise PackageExportError("Catalog relation crosses Space boundary")
                if target_id not in result:
                    result.add(target_id)
                    queue.append(target_id)
        return result

    async def _read(self, *, uri: str, principal: Principal, correlation: Correlation, expected_digest: str) -> bytes:
        request = BlobReadRequest(resource_uri=uri, principal=principal, correlation=correlation, start=0, end=MAX_BLOB_READ_BYTES, expected_digest=expected_digest)
        try:
            result = self.reader(request) if callable(self.reader) and not hasattr(self.reader, "read") else self.reader.read(request)
            result = await result if inspect.isawaitable(result) else result
        except Exception as error:
            raise PackageExportError(f"asset read failed: {uri}") from error
        content = getattr(result, "content", None)
        if content is None and isinstance(result, Mapping):
            content = result.get("content")
        if not isinstance(content, (bytes, bytearray)):
            raise PackageExportError(f"asset read returned no content: {uri}")
        digest = getattr(result, "asset_digest", None) or (result.get("asset_digest") if isinstance(result, Mapping) else None)
        content_digest = getattr(result, "content_digest", None) or (result.get("content_digest") if isinstance(result, Mapping) else None)
        if str(digest or content_digest or "") != expected_digest:
            raise PackageExportError(f"asset digest differs from Catalog: {uri}")
        return bytes(content)

    async def export(self, principal: Principal, correlation: Correlation, *, output_zip: Path, package_id: str, version: str, collections: list[dict[str, Any]], workspace_root: Path | None = None) -> dict[str, Any]:
        if not isinstance(principal, Principal) or principal.tenant_id is not None:
            raise PackageExportError("principal is required")
        scopes = _scopes(principal)
        if not ({"knowledge.admin", "knowledge:admin"} & scopes):
            raise PackageExportError("admin scope is required")
        if not isinstance(output_zip, Path) or output_zip.is_symlink() or output_zip.exists():
            raise PackageExportError("output_zip must be a new local path")
        if output_zip.as_posix().startswith(("http://", "https://", "file://")):
            raise PackageExportError("network and file URI output paths are not allowed")
        snapshot = self.repository.read_package_snapshot()
        chosen, selected_space = _selected_collections(snapshot, collections)
        scope = _space_scope(principal)
        if scope is None or any(str(item.get("space_id")) not in scope for item in chosen):
            raise PackageExportError("principal lacks selected Space scope")
        if selected_space:
            spaces = [dict(item) for item in snapshot.spaces if str(item.get("id")) == selected_space]
        else:
            selected_ids = {str(item.get("space_id")) for item in chosen}
            spaces = [dict(item) for item in snapshot.spaces if str(item.get("id")) in selected_ids]
        selected_ids = {str(asset_id) for collection in chosen for asset_id in collection.get("asset_ids", ())}
        assets_by_id = {str(item.get("id")): dict(item) for item in snapshot.assets}
        selected_ids = self._catalog_relations(selected_ids, assets_by_id) | selected_ids
        assets: list[dict[str, Any]] = []
        for asset_id in sorted(selected_ids):
            asset = assets_by_id.get(asset_id)
            if asset is None:
                raise PackageExportError(f"selected collection references missing asset: {asset_id}")
            if str(asset.get("space_id")) not in {str(item.get("id")) for item in spaces}:
                raise PackageExportError("asset crosses selected Space boundary")
            assets.append(asset)
        # A derivative is a complete package member only when its Catalog relation is explicit.
        semantic_ids = {str(value) for collection in chosen for value in collection.get("semantic_asset_ids", ())}
        semantic_assets = [dict(item) for item in snapshot.semantic_assets if str(item.get("id")) in semantic_ids]
        if len(semantic_assets) != len(semantic_ids):
            raise PackageExportError("selected collection references missing semantic asset")
        selected_asset_ids = {str(item.get("id")) for item in assets}
        collections_payload = []
        for item in chosen:
            ids = [str(value) for value in item.get("asset_ids", ()) if str(value) in selected_asset_ids]
            for base_id in tuple(ids):
                base = assets_by_id.get(base_id)
                if base:
                    ids.extend(sorted(self._catalog_relations({base_id}, assets_by_id) - {base_id}))
            collections_payload.append(dict(item, asset_ids=list(dict.fromkeys(ids))))
        selected_dataset_keys: set[tuple[str, str]] = set()
        selected_source_ids: set[str] = set()
        for item in collections_payload:
            explicit_source_ids = {str(value) for value in item.get("database_source_ids", [])}
            selected_source_ids.update(explicit_source_ids)
            if explicit_source_ids:
                continue
            binding = item.get("provider_bindings", {}).get("database_nl2sql") if isinstance(item.get("provider_bindings"), dict) else None
            dataset_id = binding.get("dataset_id") if isinstance(binding, dict) else item.get("dataset_id")
            if dataset_id:
                selected_dataset_keys.add((str(item.get("space_id")), str(dataset_id)))
        database_sources = [source for source in snapshot.database_sources if str(source.get("id")) in selected_source_ids or (str(source.get("space_id")), str(source.get("dataset_id"))) in selected_dataset_keys]
        source_ids = {str(source.get("id")): source for source in database_sources}
        for collection in collections_payload:
            explicit_ids = [str(value) for value in collection.get("database_source_ids", [])]
            if explicit_ids:
                matching_ids = [source_id for source_id in explicit_ids if source_id in source_ids]
                if set(matching_ids) != set(explicit_ids):
                    raise PackageExportError('database Collection references missing portable evidence')
            else:
                binding = collection.get("provider_bindings", {}).get("database_nl2sql") if isinstance(collection.get("provider_bindings"), dict) else None
                dataset_id = binding.get("dataset_id") if isinstance(binding, dict) else collection.get("dataset_id")
                matching_ids = [source_id for source_id, source in source_ids.items() if str(source.get("space_id")) == str(collection.get("space_id")) and str(source.get("dataset_id")) == str(dataset_id)] if dataset_id else []
            if str(collection.get("kind", "")).casefold() in {"database", "live_database"} or {"database_nl2sql", "database_schema", "database_execute_readonly"} & {str(value) for value in collection.get("capabilities", [])}:
                if not matching_ids:
                    raise PackageExportError(f"database Collection has no matching portable evidence source: {collection.get('id')}")
            collection["database_source_ids"] = sorted(set(matching_ids))
        catalog_revision = str(snapshot.catalog_revision)
        output_zip.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".knowledge-package-export-", dir=str(output_zip.parent)) as staging:
            source_dir = Path(staging) / "sources"
            source_dir.mkdir()
            asset_files: dict[str, Path] = {}
            for asset in assets:
                digest = str(asset.get("content_digest") or "")
                uri = str(asset.get("source_uri") or "")
                if not digest or not uri:
                    raise PackageExportError(f"asset metadata is incomplete: {asset.get('id')}")
                content = await self._read(uri=uri, principal=principal, correlation=correlation, expected_digest=digest)
                asset_id = str(asset.get("id") or "")
                if not re.fullmatch(r"[A-Za-z0-9._:-]{1,160}", asset_id):
                    raise PackageExportError("Catalog asset id is unsafe")
                suffixes = {"text/markdown": ".md", "text/csv": ".csv", "text/tab-separated-values": ".tsv", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx", "image/png": ".png", "image/jpeg": ".jpg"}
                path = source_dir / f"{asset_id}{suffixes.get(str(asset.get('mime_type') or '').casefold(), '')}"
                path.write_bytes(content)
                asset_files[asset_id] = path
            package_root = Path(staging) / "package"
            published = False
            try:
                result = self.builder.build(output_dir=package_root, package_id=package_id, version=version, spaces=spaces, collections=collections_payload, assets=assets, asset_files=asset_files, capabilities=sorted({str(cap) for item in collections_payload for cap in item.get("capabilities", ())}), catalog_revision=catalog_revision, semantic_assets=semantic_assets, database_sources=database_sources, provider_versions=snapshot.provider_versions)
                if str(self.repository.catalog_revision) != catalog_revision:
                    raise PackageExportError("Catalog changed after asset reads")
                staged_zip = Path(staging) / "package.zip"
                export_package_zip(package_root, staged_zip)
                parent_fd = _open_safe_parent_directory(output_zip)
                staging_fd = os.open(staging, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.link(staged_zip.name, output_zip.name, src_dir_fd=staging_fd, dst_dir_fd=parent_fd, follow_symlinks=False)
                    os.unlink(staged_zip.name, dir_fd=staging_fd)
                    published_stat = output_zip.stat()
                finally:
                    os.close(staging_fd)
                    os.close(parent_fd)
                published = True
                workspace = None
                if workspace_root is not None:
                    workspace = WorkspaceMaterializer().materialize(package_root=package_root, workspace_root=workspace_root).workspace_root
                validate_package(package_root)
            except (PackageBuildError, OSError, ValueError) as error:
                if locals().get("published_stat") is not None and output_zip.exists():
                    try:
                        current = output_zip.stat()
                        if (current.st_dev, current.st_ino) == (published_stat.st_dev, published_stat.st_ino):
                            output_zip.unlink()
                    except OSError:
                        pass
                raise PackageExportError(str(error)) from error
        return {"package_revision": result.package_revision, "catalog_revision": catalog_revision, "asset_count": result.asset_count, "file_count": result.file_count}
