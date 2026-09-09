"""Explicit local CSV/TSV bindings for owned Table/Authoring/Processing services."""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import (
    SqliteCatalogQueryRepository,
    SqliteLogicalDatasetProcessingJobStore,
    SqliteStructuredAssetWriter,
)
from knowledge_platform.structured import (
    LocalStructuredFileProvider,
    LogicalDatasetAuthoringService,
    LogicalDatasetProcessingService,
    LogicalDatasetProcessingWorker,
    TableQueryService,
)

STRUCTURED_SCOPES = ('knowledge.table_query', 'knowledge.admin', 'knowledge.processing')
_ID = re.compile(r'^[A-Za-z0-9._-]{1,160}$')


def load_structured_config(path: Path) -> dict[str, Path]:
    """Host paths are accepted only from this explicit configuration, never HTTP."""
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or set(value) != {'version', 'space_id', 'assets'}:
        raise ValueError('Invalid structured configuration fields')
    if type(value['version']) is not int or value['version'] != 1 or value['space_id'] != 'space_kb_default':
        raise ValueError('Unsupported structured configuration version or Space')
    assets = value['assets']
    if not isinstance(assets, dict) or not assets:
        raise ValueError('Explicit structured asset bindings are required')
    result = {}
    for asset_id, file in assets.items():
        if not _ID.fullmatch(asset_id) or not isinstance(file, str) or not Path(file).is_absolute():
            raise ValueError('Invalid structured asset binding')
        result[asset_id] = Path(file)
    return result


class CatalogProcessingBindings:
    """Resolve a current logical definition against the configured source allowlist."""
    def __init__(self, catalog: SqliteCatalogQueryRepository, paths: dict[str, Path]):
        self.catalog, self.paths = catalog, dict(paths)

    def resolve(self, *, dataset_id: str, space_id: str) -> dict[str, Path] | None:
        dataset = self.catalog.get_structured_asset(asset_id=dataset_id)
        if not dataset or dataset.get('space_id') != space_id or space_id != 'space_kb_default':
            return None
        logical = dataset.get('logical_dataset')
        sources = logical.get('source_asset_ids') if isinstance(logical, dict) else None
        if not isinstance(sources, list) or not sources or any(source not in self.paths for source in sources):
            return None
        for source_id in sources:
            source = self.catalog.get_structured_asset(asset_id=source_id)
            if not source or source.get('space_id') != space_id:
                return None
        return {source: self.paths[source] for source in sources}


class ConfiguredSourceCatalog:
    """Authoring can consume only the host-configured sources in this local Space."""
    def __init__(self, catalog, asset_ids):
        self.catalog, self.asset_ids = catalog, frozenset(asset_ids)

    @property
    def catalog_revision(self):
        return self.catalog.catalog_revision

    def get_structured_asset(self, *, asset_id):
        if asset_id not in self.asset_ids:
            return None
        asset = self.catalog.get_structured_asset(asset_id=asset_id)
        return asset if asset and asset.get('space_id') == 'space_kb_default' else None


class CatalogTableProvider:
    """Build a read-only local provider from the current published dataset definition."""
    def __init__(self, catalog, paths, uris):
        self.catalog, self.paths, self.uris = catalog, dict(paths), dict(uris)

    async def query(self, *, query, asset_id, space_id, limit, semantic_context):
        paths, uris, logical = dict(self.paths), dict(self.uris), {}
        if asset_id and asset_id not in paths:
            dataset = self.catalog.get_structured_asset(asset_id=asset_id)
            bindings = CatalogProcessingBindings(self.catalog, paths).resolve(
                dataset_id=asset_id, space_id=space_id or '')
            if dataset and bindings:
                logical[asset_id] = tuple(bindings.items())
                uris[asset_id] = dataset['source_uri']
        provider = LocalStructuredFileProvider(asset_paths=paths, asset_uris=uris,
                                                logical_asset_sources=logical)
        return await provider.query(query=query, asset_id=asset_id, space_id=space_id,
                                    limit=limit, semantic_context=semantic_context)


def build_structured_services(paths: dict[str, Path], catalog_path: Path) -> dict[str, Any]:
    """Bind existing ready sources; write only new definitions/jobs to the owned Catalog."""
    catalog = SqliteCatalogQueryRepository(catalog_path)
    uris = {}
    for asset_id, path in paths.items():
        asset = catalog.get_structured_asset(asset_id=asset_id)
        if (not asset or asset.get('space_id') != 'space_kb_default'
                or asset.get('reference_status') not in {'ready', 'verified', 'active'}
                or asset.get('source_type') == 'logical_concat'):
            raise ValueError('Structured source is not an approved local Catalog Asset')
        profile = LocalStructuredFileProvider(asset_paths={}, asset_uris={}).inspect_source(path=path)
        if profile.content_digest != asset.get('content_digest'):
            raise ValueError('Structured source bytes differ from the Catalog')
        uris[asset_id] = asset['source_uri']
    provider = LocalStructuredFileProvider(asset_paths=paths, asset_uris=uris)
    writer = SqliteStructuredAssetWriter(catalog_path)
    processing = LogicalDatasetProcessingService(catalog=catalog, profiler=provider, publisher=writer)
    return {
        'table_query': TableQueryService(catalog=catalog, provider=CatalogTableProvider(catalog, paths, uris)),
        'structured_authoring': LogicalDatasetAuthoringService(
            catalog=ConfiguredSourceCatalog(catalog, paths), writer=writer),
        'structured_processing': processing,
        'processing_bindings': CatalogProcessingBindings(catalog, paths),
        'processing_worker': LogicalDatasetProcessingWorker(service=processing,
            jobs=SqliteLogicalDatasetProcessingJobStore(database_path=catalog_path)),
    }
