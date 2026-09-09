from __future__ import annotations

import hashlib
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import create_engine

from knowledge_platform.catalog import migrate_to_latest
from knowledge_platform.connector_sync import (
    ConnectorSyncRequest,
    ConnectorSyncWorker,
    LocalConnectorSourceProvider,
    SourceItemSnapshot,
    SqliteConnectorSyncStore,
)


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _catalog(path: Path) -> None:
    engine = create_engine(f"sqlite:///{path}")
    with engine.begin() as connection:
        migrate_to_latest(connection)
    engine.dispose()


def _binding(
    path: Path,
    *,
    space_id: str = "space_local",
    connector_id: str = "connector_local",
    source_item_id: str = "item_local",
    asset_id: str = "asset_local",
    content_digest: str,
) -> None:
    now = datetime.now(timezone.utc).isoformat()
    with sqlite3.connect(path) as connection:
        connection.execute(
            "INSERT INTO knowledge_spaces VALUES (?, ?, ?, ?, ?, ?)",
            (space_id, f"Local {space_id}", "", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_connectors VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (connector_id, space_id, "local", "Local Connector", "ready", "builtin", "", "{}", "{}", None, None, "{}", now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (asset_id, space_id, "document", "Local", "", "text/markdown", "connector", "knowledge://local", content_digest, content_digest, "{}", "{}", now, now),
        )
        connection.execute(
            "INSERT INTO knowledge_source_items VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (source_item_id, space_id, connector_id, source_item_id, None, "document", "Local", None, "[]", content_digest, content_digest, asset_id, "ready", None, None, None, "{}", "{}", now, now),
        )


def _worker(path: Path, source: Path, *, space_id: str = "space_local", connector_id: str = "connector_local", item_id: str = "item_local") -> ConnectorSyncWorker:
    return ConnectorSyncWorker(
        source=LocalConnectorSourceProvider(),
        store=SqliteConnectorSyncStore(database_path=path),
    )


def _request(source: Path, *, space_id: str = "space_local", connector_id: str = "connector_local", item_id: str = "item_local", key: str = "sync-1") -> ConnectorSyncRequest:
    return ConnectorSyncRequest(
        connector_id=connector_id,
        space_id=space_id,
        source_paths={item_id: source},
        idempotency_key=key,
    )


def test_connector_sync_reconciles_explicit_local_binding_and_replays_terminal_result(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# local", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _binding(catalog, content_digest=_digest(source.read_bytes()))

    worker = _worker(catalog, source)
    first = worker.sync(_request(source))
    second = worker.sync(_request(source))

    assert first == second
    assert (first.discovered, first.changed, first.unchanged) == (1, 0, 1)
    with sqlite3.connect(catalog) as connection:
        run = connection.execute(
            "SELECT status, current_step, progress, lease_owner FROM knowledge_sync_runs WHERE id = ?",
            (first.run_id,),
        ).fetchone()
        item = connection.execute(
            "SELECT last_seen_sync_run_id, content_digest FROM knowledge_source_items WHERE id = 'item_local'"
        ).fetchone()
        connector = connection.execute(
            "SELECT last_sync_run_id FROM knowledge_connectors WHERE id = 'connector_local'"
        ).fetchone()
    assert run == ("succeeded", "completed", 100, None)
    assert item == (first.run_id, _digest(b"# local"))
    assert connector == (first.run_id,)


def test_connector_sync_releases_failed_claim_and_rejects_symlink_source(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("local", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _binding(catalog, content_digest=_digest(source.read_bytes()))
    missing = tmp_path / "missing.md"

    with pytest.raises(FileNotFoundError):
        _worker(catalog, source).sync(_request(missing))
    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT COUNT(*) FROM knowledge_sync_runs").fetchone()[0] == 0

    link = tmp_path / "link.md"
    link.symlink_to(source)
    with pytest.raises(OSError, match="symlink"):
        LocalConnectorSourceProvider().read(source_item_id="item_local", path=link)

    source.write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        LocalConnectorSourceProvider(expected_digests={"item_local": _digest(b"local")}).read(
            source_item_id="item_local", path=source
        )


def test_connector_sync_fences_space_connector_and_lease_owner(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("local", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    digest = _digest(source.read_bytes())
    _binding(catalog, content_digest=digest)
    _binding(
        catalog,
        space_id="space_other",
        connector_id="connector_other",
        source_item_id="item_other",
        asset_id="asset_other",
        content_digest=digest,
    )
    store = SqliteConnectorSyncStore(database_path=catalog)

    with pytest.raises(ValueError, match="requested Space"):
        store.claim(connector_id="connector_local", space_id="space_other", idempotency_key="wrong-space")
    first = store.claim(connector_id="connector_local", space_id="space_local", idempotency_key="same-key")
    assert first.acquired
    with pytest.raises(ValueError, match="lease"):
        store.complete(
            run_id=first.run_id,
            owner="connector-worker-forged",
            connector_id="connector_local",
            space_id="space_local",
            snapshots=(SourceItemSnapshot("item_local", digest, len(b"local")),),
        )
    with pytest.raises(ValueError, match="collision"):
        store.claim(connector_id="connector_other", space_id="space_other", idempotency_key="same-key")
    store.release(run_id=first.run_id, owner=first.owner)


def test_connector_sync_rejects_inconsistent_existing_asset_before_mutation(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("local", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    _binding(catalog, content_digest=_digest(b"old"))
    new_digest = _digest(source.read_bytes())
    with sqlite3.connect(catalog) as connection:
        claim = SqliteConnectorSyncStore(database_path=catalog).claim(
            connector_id="connector_local", space_id="space_local", idempotency_key="inconsistent"
        )
        connection.execute("UPDATE knowledge_assets SET content_digest = ? WHERE id = 'asset_local'", (_digest(b"different"),))
    assert claim.acquired
    store = SqliteConnectorSyncStore(database_path=catalog)
    with pytest.raises(ValueError, match="inconsistent"):
        store.complete(
            run_id=claim.run_id,
            owner=claim.owner,
            connector_id="connector_local",
            space_id="space_local",
            snapshots=(SourceItemSnapshot("item_local", new_digest, len(b"local")),),
        )
    with sqlite3.connect(catalog) as connection:
        assert connection.execute("SELECT status FROM knowledge_sync_runs WHERE id = ?", (claim.run_id,)).fetchone() == ("running",)
        assert connection.execute("SELECT content_digest FROM knowledge_source_items WHERE id = 'item_local'").fetchone() == (_digest(b"old"),)
    store.release(run_id=claim.run_id, owner=claim.owner)


def test_connector_sync_reclaims_expired_lease_with_new_owner_and_fences_old_owner(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("local", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog)
    digest = _digest(source.read_bytes())
    _binding(catalog, content_digest=digest)
    store = SqliteConnectorSyncStore(database_path=catalog)
    first = store.claim(connector_id="connector_local", space_id="space_local", idempotency_key="lease-retry")
    assert first.acquired
    with sqlite3.connect(catalog) as connection:
        connection.execute(
            "UPDATE knowledge_sync_runs SET lease_expires_at = ? WHERE id = ?",
            ("2000-01-01T00:00:00+00:00", first.run_id),
        )
    second = store.claim(connector_id="connector_local", space_id="space_local", idempotency_key="lease-retry")
    assert second.acquired and second.owner != first.owner and second.run_id == first.run_id
    with pytest.raises(ValueError, match="lease"):
        store.complete(
            run_id=first.run_id,
            owner=first.owner,
            connector_id="connector_local",
            space_id="space_local",
            snapshots=(SourceItemSnapshot("item_local", digest, len(b"local")),),
        )
    result = store.complete(
        run_id=second.run_id,
        owner=second.owner,
        connector_id="connector_local",
        space_id="space_local",
        snapshots=(SourceItemSnapshot("item_local", digest, len(b"local")),),
    )
    assert result.run_id == first.run_id
