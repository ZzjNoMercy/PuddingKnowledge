"""Table-query bindings for immutable, locally published Knowledge Packages."""

from __future__ import annotations

import hashlib
import json
import asyncio
import tempfile
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from knowledge_platform.structured import LocalStructuredFileProvider, StructuredQueryProviderError


_MIME_SUFFIXES = {
    "text/csv": ".csv",
    "text/tab-separated-values": ".tsv",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.ms-excel": ".xls",
}
_TABLE_KINDS = {"table", "spreadsheet", "structured_asset", "structured"}
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _metadata(asset: Mapping[str, Any]) -> dict[str, Any]:
    value = asset.get("metadata", asset.get("metadata_json", {}))
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            value = {}
    return dict(value) if isinstance(value, Mapping) else {}


def _package_uri(asset: Mapping[str, Any]) -> str:
    space_id, asset_id = str(asset.get("space_id") or ""), str(asset.get("id") or "")
    return f"knowledge://spaces/{space_id}/structured-assets/{asset_id}/source"


def _mime_suffix(mime_type: str, package_path: str) -> str | None:
    suffix = _MIME_SUFFIXES.get(mime_type.casefold())
    if suffix is not None:
        return suffix
    candidate = Path(package_path).suffix.casefold()
    return candidate if candidate in {".csv", ".tsv", ".xlsx", ".xls"} else None


class PackageTableCatalog:
    """Expose published Package table Assets through the structured Catalog port."""

    def __init__(self, repository: Any, publisher: Any):
        self.repository = repository
        self.publisher = publisher

    @property
    def catalog_revision(self) -> str:
        return str(self.repository.catalog_revision)

    def _published_metadata(self, asset_id: str) -> dict[str, Any]:
        # The normal read repository may intentionally omit metadata_json.  The
        # publisher owns the Package publication table, so reading that row is
        # the authoritative way to recover package_path/sheet_name.
        connect = getattr(self.publisher, "_connect", None)
        if connect is None:
            return {}
        try:
            with connect() as db:
                row = db.execute(
                    "SELECT metadata_json FROM knowledge_assets WHERE id=? AND source_type='package'",
                    (asset_id,),
                ).fetchone()
            if row is None:
                return {}
            raw = row["metadata_json"] if hasattr(row, "keys") else row[0]
            return _metadata({"metadata_json": raw})
        except Exception as error:
            raise StructuredQueryProviderError("published Package metadata is unavailable") from error

    def _asset(self, asset: Mapping[str, Any]) -> dict[str, Any] | None:
        if str(asset.get("source_type") or "") != "package":
            return None
        kind = str(asset.get("kind") or "")
        mime_type = str(asset.get("mime_type") or asset.get("mimetype") or "")
        metadata = _metadata(asset)
        if not metadata:
            metadata = self._published_metadata(str(asset.get("id") or ""))
        package_path = str(metadata.get("package_path") or "")
        suffix = _mime_suffix(mime_type, package_path)
        if kind not in _TABLE_KINDS and suffix is None:
            return None
        if suffix is None:
            return None
        asset_id, space_id = str(asset.get("id") or ""), str(asset.get("space_id") or "")
        digest = str(asset.get("content_digest") or "")
        if not asset_id or not space_id or not digest.startswith("sha256:"):
            return None
        result = dict(asset)
        result.update(
            {
                "id": asset_id,
                "space_id": space_id,
                "kind": "structured_asset",
                "source_type": "package",
                "source_uri": _package_uri(asset),
                "content_digest": digest,
                "mime_type": mime_type,
                "file_name": str(asset.get("title") or package_path or asset_id),
                "sheet_name": metadata.get("sheet_name"),
                "package_path": package_path,
                "metadata": metadata,
                "reference_status": "ready",
                "profile_status": "ready",
                "capabilities": ["table_query"],
            }
        )
        return result

    def list_structured_assets(self, *, space_id: str | None = None) -> list[dict[str, Any]]:
        if hasattr(self.repository, "list_structured_assets"):
            raw = self.repository.list_structured_assets(space_id=space_id)
            result = []
            for asset in raw:
                if not isinstance(asset, Mapping):
                    continue
                verified = self.get_structured_asset(asset_id=str(asset.get("id") or ""))
                if verified is not None:
                    result.append(verified)
            return result
        else:
            raw = self.repository.list_assets(space_id=space_id)
            result = []
            for asset in raw:
                if isinstance(asset, Mapping):
                    verified = self.get_structured_asset(asset_id=str(asset.get("id") or ""))
                    if verified is not None:
                        result.append(verified)
            return result

    def get_structured_asset(self, *, asset_id: str) -> dict[str, Any] | None:
        # When the canonical structured table exists, it is the binding
        # authority.  Verify it still points at the same published Package
        # Asset before exposing it to TableQueryService.
        structured = None
        getter = getattr(self.repository, "get_structured_asset", None)
        if getter is not None:
            structured = getter(asset_id=asset_id)
            if not isinstance(structured, Mapping) or str(structured.get("source_type") or "") != "package":
                return None
        if isinstance(structured, Mapping) and str(structured.get("source_type") or "") == "package":
            asset = None
            asset_getter = getattr(self.repository, "get_asset", None)
            if asset_getter is not None:
                asset = asset_getter(asset_id=asset_id)
            if not isinstance(asset, Mapping):
                return None
            projected = self._asset(asset)
            if projected is None or projected["content_digest"] != str(structured.get("content_digest") or ""):
                return None
            metadata = projected["metadata"]
            expected_uri = _package_uri(projected)
            if (
                str(structured.get("source_uri") or "") != expected_uri
                or str(structured.get("space_id") or "") != projected["space_id"]
                or not _DIGEST_RE.fullmatch(str(metadata.get("package_revision") or ""))
                or structured.get("sheet_name") != metadata.get("sheet_name")
            ):
                return None
            projected.update({key: value for key, value in structured.items() if key not in {"kind", "source_type", "source_uri"}})
            projected["kind"] = "structured_asset"
            projected["source_type"] = "package"
            projected["source_uri"] = _package_uri(projected)
            projected["capabilities"] = [str(item) for item in (structured.get("capabilities") or ["table_query"])]
            return projected
        if getter is not None:
            return None
        if hasattr(self.repository, "get_asset"):
            asset = self.repository.get_asset(asset_id=asset_id)
        else:
            asset = next((item for item in self.repository.list_assets() if str(item.get("id")) == asset_id), None)
        return self._asset(asset) if isinstance(asset, Mapping) else None


class PackageTableProvider:
    """Query only immutable published Package objects, never caller paths."""

    def __init__(self, publisher: Any, repository: PackageTableCatalog | Any):
        self.publisher = publisher
        self.catalog = repository if isinstance(repository, PackageTableCatalog) else PackageTableCatalog(repository, publisher)

    async def query(
        self,
        *,
        query: str,
        asset_id: str | None,
        space_id: str | None,
        limit: int,
        semantic_context: object | None,
    ) -> Sequence[Any]:
        assets = [self.catalog.get_structured_asset(asset_id=asset_id)] if asset_id else self.catalog.list_structured_assets(space_id=space_id)
        assets = [asset for asset in assets if asset is not None]
        if space_id is not None:
            assets = [asset for asset in assets if asset["space_id"] == space_id]
        if asset_id is not None and not assets:
            return ()
        paths: dict[str, Path] = {}
        uris: dict[str, str] = {}
        sheets: dict[str, str | int | None] = {}
        temporary: list[Path] = []
        try:
            for asset in assets:
                current = self.catalog.get_structured_asset(asset_id=asset["id"])
                if current is None or current["content_digest"] != asset["content_digest"]:
                    raise StructuredQueryProviderError("Package table binding changed")
                content = self.publisher.read_published(asset["id"])
                digest = "sha256:" + hashlib.sha256(content).hexdigest()
                if digest != current["content_digest"]:
                    raise StructuredQueryProviderError("published Package table digest mismatch")
                suffix = _mime_suffix(str(current.get("mime_type") or ""), str(current.get("package_path") or ""))
                if suffix is None:
                    continue
                # LocalStructuredFileProvider opens every path component with
                # O_NOFOLLOW; the macOS default temp directory is commonly
                # reached through /var, a symlink.  Use the durable private
                # temp root so the existing safe opener can verify it.
                with tempfile.NamedTemporaryFile(
                    prefix="knowledge-package-table-", suffix=suffix,
                    dir=Path(tempfile.gettempdir()).resolve(), delete=False,
                ) as stream:
                    stream.write(content)
                    path = Path(stream.name)
                temporary.append(path)
                paths[current["id"]] = path
                uris[current["id"]] = current["source_uri"]
                sheets[current["id"]] = current.get("sheet_name")
            if not paths:
                return ()
            provider = LocalStructuredFileProvider(asset_paths=paths, asset_uris=uris, asset_sheets=sheets)
            task = asyncio.create_task(provider.query(query=query, asset_id=asset_id, space_id=space_id, limit=limit, semantic_context=semantic_context))
            try:
                return await asyncio.shield(task)
            except asyncio.CancelledError:
                await task
                raise
        finally:
            for path in temporary:
                try:
                    path.unlink()
                except FileNotFoundError:
                    pass
