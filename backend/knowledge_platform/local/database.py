"""Explicit, host-local PostgreSQL/Vanna composition for the local product.

Configuration never discovers credentials or opens a database. Composition
validates Vanna provenance before connecting and only binds the private Catalog.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteStructuredAssetWriter
from knowledge_platform.database import (
    DatabaseCollectionBindingRequest,
    DatabaseCollectionBindingService,
    DatabaseExecuteReadonlyService,
    DatabaseNl2SqlService,
    GatewayVannaProvider,
    InMemoryQueryPlanRepository,
    LocalPostgresDatabaseDatasetResolver,
    LocalPostgresDatabaseSchemaReader,
    LocalPostgresDatabaseSource,
    LocalVannaCollectionGateway,
    PostgresReadonlyDatabaseExecutor,
    PostgresReadonlySqlValidator,
)
from knowledge_platform.database.schema_service import DatabaseSchemaQueryService

_ID = re.compile(r"[A-Za-z0-9._:-]{1,160}")
_DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
_ENV = re.compile(r"KNOWLEDGE_DB_[A-Z0-9_]{1,100}")
DATABASE_SCOPES = ("knowledge.database_nl2sql", "knowledge.database_execute_readonly", "knowledge.database_schema")


def _object(value: Any, fields: set[str]) -> dict:
    if not isinstance(value, dict) or set(value) != fields:
        raise ValueError("database configuration fields are invalid")
    return value


def _text(value: Any, pattern: re.Pattern = _ID) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise ValueError("database configuration value is invalid")
    return value


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate database configuration field")
        result[key] = value
    return result


@dataclass(frozen=True, repr=False)
class DatabaseConfig:
    collection_id: str
    source: LocalPostgresDatabaseSource
    password_env: str | None
    collection_root: Path | None
    collection_name: str
    package_revision: str
    input_digest: str
    package_source_id: str | None = None
    collection_version: str | None = None


def load_database_config(path: Path) -> DatabaseConfig:
    path = path.expanduser().absolute()
    if any(p.is_symlink() for p in (path, *path.parents)) or not path.is_file():
        raise ValueError("database configuration must be a regular non-symlink file")
    with path.open("rb") as stream:
        raw = stream.read(65537)
    if len(raw) > 65536:
        raise ValueError("database configuration exceeds size limit")
    value = json.loads(raw, object_pairs_hook=_unique_object)
    value = _object(value, {"format", "collection_id", "source", "vanna"}
        | ({"collection_version"} if isinstance(value, dict) and value.get("format") == "knowledge-local-database/v2" else set()))
    if value["format"] not in {"knowledge-local-database/v1", "knowledge-local-database/v2"}:
        raise ValueError("database configuration format is unsupported")
    source = _object(value["source"], {"dataset_id", "host", "port", "database", "username", "allowed_tables", "password_env"})
    packaged = value["format"] == "knowledge-local-database/v2"
    vanna = _object(value["vanna"], ({"package_source_id", "package_revision", "input_digest"} if packaged
        else {"root", "collection_name", "package_revision", "input_digest"}))
    tables = source["allowed_tables"]
    if not isinstance(tables, list) or not 1 <= len(tables) <= 100 or any(not isinstance(t, str) for t in tables):
        raise ValueError("database table allowlist is invalid")
    password_env = source["password_env"]
    if password_env is not None:
        _text(password_env, _ENV)
    root = vanna.get("root")
    if not packaged and (not isinstance(root, str) or not Path(root).is_absolute()):
        raise ValueError("Vanna root must be an explicit absolute path")
    input_digest = _text(vanna["input_digest"], _DIGEST)
    return DatabaseConfig(
        collection_id=_text(value["collection_id"]),
        source=LocalPostgresDatabaseSource(
            dataset_id=_text(source["dataset_id"]), space_id="space_kb_default",
            host=source["host"], port=source["port"], database=_text(source["database"]),
            username=_text(source["username"]), allowed_tables=tuple(tables),
            dataset_version="local-postgres-v1", deployment_revision="local-unactivated-v1",
            semantic_context_hash=input_digest,
        ),
        password_env=password_env, collection_root=Path(root) if root is not None else None,
        collection_name=_text(vanna["collection_name"]) if not packaged else "package_evidence",
        package_source_id=_text(vanna["package_source_id"]) if packaged else None,
        collection_version=_text(value["collection_version"]) if packaged else None,
        package_revision=_text(vanna["package_revision"], _DIGEST), input_digest=input_digest,
    )


def build_database_services(config: DatabaseConfig, catalog_path: Path, *, publisher=None) -> dict[str, Any]:
    from dataclasses import replace

    if config.package_source_id is not None:
        repository = SqliteCatalogQueryRepository(catalog_path)
        collection = next((c for c in repository.list_collections(space_id=config.source.space_id)
                           if c.get("id") == config.collection_id
                           and (config.collection_version is None or c.get("version") == config.collection_version)), None)
        if publisher is None or collection is None:
            raise ValueError("Package database binding requires a published Collection")
        from knowledge_platform.local.package_database import PublishedDatabaseGateway
        gateway = PublishedDatabaseGateway(publisher, source_id=config.package_source_id,
            package_revision=config.package_revision, input_digest=config.input_digest,
            dataset_id=config.source.dataset_id, space_id=config.source.space_id,
            collection_id=config.collection_id, collection_version=collection['version'])
    else:
        gateway = LocalVannaCollectionGateway(
            config.collection_root, expected_collection_name=config.collection_name,
            expected_package_revision=config.package_revision, expected_input_digest=config.input_digest,
        )
    try:
        return _bind_database_services(config, catalog_path, gateway)
    except BaseException:
        if config.package_source_id is not None:
            gateway.close()
        raise


def _bind_database_services(config, catalog_path, gateway):
    from dataclasses import replace
    password = ""
    if config.password_env is not None:
        if config.password_env not in os.environ:
            raise ValueError("explicit database password environment variable is missing")
        password = os.environ[config.password_env]
    source = replace(config.source, password=password)
    repository = SqliteCatalogQueryRepository(catalog_path)
    collection = next((c for c in repository.list_collections(space_id=source.space_id)
                       if c.get("id") == config.collection_id
                           and (config.collection_version is None or c.get("version") == config.collection_version)), None)
    if collection is None:
        raise ValueError("configured database Collection does not exist")
    datasets = LocalPostgresDatabaseDatasetResolver([source])
    if config.package_source_id is not None:
        from knowledge_platform.local.package_database import PublishedDatabaseResolver
        datasets = PublishedDatabaseResolver(datasets, gateway)
    principal = Principal(subject_id="knowledge-local-binding", scopes=("knowledge.processing", f"knowledge.space:{source.space_id}"))
    bound = DatabaseCollectionBindingService(datasets=datasets, writer=SqliteStructuredAssetWriter(catalog_path)).bind(
        principal=principal, correlation=Correlation("knowledge-local-database-binding"),
        request=DatabaseCollectionBindingRequest(collection_id=config.collection_id,
                                                collection_version=collection["version"],
                                                space_id=source.space_id, dataset_id=source.dataset_id),
    )
    if bound.status != "ok":
        raise ValueError("local database Collection binding is unavailable")
    binding = datasets.resolve(dataset_id=source.dataset_id, space_id=source.space_id)
    if binding is None:
        raise ValueError("local database source is unavailable")
    plans = InMemoryQueryPlanRepository()
    validator = PostgresReadonlySqlValidator()
    key = (source.space_id, source.dataset_id)
    return {
        **({"_database_close": gateway.close} if config.package_source_id is not None else {}),
        "database_nl2sql": DatabaseNl2SqlService(datasets=datasets, provider=GatewayVannaProvider(gateway, version="local-vanna-examples-v1"), validator=validator, plans=plans),
        "database_execute": DatabaseExecuteReadonlyService(datasets=datasets, plans=plans, validator=validator,
            executor=PostgresReadonlyDatabaseExecutor({key: source}, validator=validator, source_revisions={key: binding.source_revision})),
        "database_schema": DatabaseSchemaQueryService(datasets=datasets, reader=LocalPostgresDatabaseSchemaReader({key: source})),
    }
