from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import create_engine

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, migrate_to_latest
from knowledge_platform.catalog.connector_queries import CatalogConnectorQueryService


def _digest(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode()).hexdigest()


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO knowledge_spaces VALUES (?, ?, ?, ?, ?, ?)",
            ("space_local", "Local", "", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_connectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "connector_local", "space_local", "web_capture", "Local Web Capture", "ready", "builtin",
                "ref://vault/secret", '{"token":"must-not-leak"}', '{"interval_minutes":0}', None, None, "{}", now, now,
            ),
        )
        digest = _digest("asset")
        connection.execute(
            "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            ("asset_local", "space_local", "document", "Local", "", "text/plain", "connector", "knowledge://local/asset", digest, digest, "{}", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_source_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "item_local", "space_local", "connector_local", "external-secret-looking-id", None, "web_page", "Local Item",
                "https://example.invalid/private", "[\"private/path\"]", digest, digest, "asset_local", "ready", None, None,
                "sync_local", '{"raw":"must-not-leak"}', '{"principal":"must-not-leak"}', now, now,
            ),
        )


def _principal(*, tenant_id: str | None = None) -> Principal:
    return Principal(
        subject_id="console-admin",
        tenant_id=tenant_id,
        scopes=("knowledge.admin", "knowledge.space:space_local"),
    )


def test_connector_discovery_is_path_free_and_secret_free(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _catalog(database)
    service = CatalogConnectorQueryService(SqliteCatalogQueryRepository(database))

    result = service.list_connectors(
        principal=_principal(), correlation=Correlation("connectors"), space_id="space_local"
    ).to_dict()
    assert result["status"] == "ok"
    assert result["data"]["connectors"][0]["connector_key"] == "web_capture"
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in ("credential_ref", "config_json", "token", "private/path"):
        assert forbidden not in encoded


def test_source_item_discovery_is_bound_to_connector_and_redacted(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _catalog(database)
    service = CatalogConnectorQueryService(SqliteCatalogQueryRepository(database))

    result = service.list_source_items(
        principal=_principal(),
        correlation=Correlation("source-items"),
        space_id="space_local",
        connector_id="connector_local",
    ).to_dict()
    assert result["status"] == "ok"
    assert result["data"]["source_items"][0]["asset_id"] == "asset_local"
    encoded = json.dumps(result, ensure_ascii=False)
    for forbidden in ("external-secret-looking-id", "source_url", "path_json", "metadata_json", "private/path"):
        assert forbidden not in encoded


def test_connector_discovery_requires_admin_space_scope_and_rejects_unknown_connector(tmp_path: Path) -> None:
    database = tmp_path / "catalog.sqlite3"
    _catalog(database)
    service = CatalogConnectorQueryService(SqliteCatalogQueryRepository(database))

    denied = service.list_connectors(
        principal=Principal(subject_id="viewer", scopes=("knowledge.list", "knowledge.space:space_local")),
        correlation=Correlation("denied"),
        space_id="space_local",
    ).to_dict()
    assert denied["error"]["code"] == "permission_denied"
    tenant_denied = service.list_connectors(
        principal=_principal(tenant_id="tenant-a"), correlation=Correlation("tenant"), space_id="space_local"
    ).to_dict()
    assert tenant_denied["error"]["code"] == "permission_denied"
    missing = service.list_source_items(
        principal=_principal(), correlation=Correlation("missing"), space_id="space_local", connector_id="connector_other"
    ).to_dict()
    assert missing["error"]["code"] == "not_found"


def test_connector_discovery_fails_closed_when_catalog_revision_changes() -> None:
    class _ChangingRepository:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def catalog_revision(self) -> str:
            self.calls += 1
            return _digest(str(self.calls))

        def list_connectors(self, *, space_id: str) -> list[dict[str, str]]:
            return [{"id": "connector_local", "space_id": space_id}]

        def list_source_items(self, *, space_id: str, connector_id: str | None = None) -> list[dict[str, str]]:
            return []

    service = CatalogConnectorQueryService(_ChangingRepository())
    result = service.list_connectors(
        principal=_principal(), correlation=Correlation("changed"), space_id="space_local"
    ).to_dict()
    assert result["error"]["code"] == "internal_error"
