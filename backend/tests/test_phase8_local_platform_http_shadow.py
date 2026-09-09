from __future__ import annotations

import os
import sqlite3
from pathlib import Path

import pytest

import knowledge_platform.local.catalog as wiki_shadow
from knowledge_contracts import Correlation, Evidence, QueryResult
from scripts.phase6_local_wiki_query_shadow import _materialize_catalog, _safe_pages, _snapshot_catalog
from scripts.phase8_local_platform_http_shadow import _collection_summary, _http_summary, _mcp_collection_summary


class _Response:
    status_code = 200

    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload


def test_http_summary_keeps_routing_and_evidence_but_drops_query_content() -> None:
    result = QueryResult(
        status="ok",
        trace_id=Correlation("trace").trace_id,
        data={"query": "private query", "count": 1, "limit": 1, "routing": {"fusion": False}},
        evidence=(
            Evidence(
                asset_id="wiki_asset",
                resource_uri="knowledge://spaces/space_1/assets/wiki_asset",
                quote="private Wiki excerpt",
                matched_by=("local",),
            ),
        ),
    )

    summary = _http_summary(_Response(result.to_dict()))

    assert summary["http_status"] == 200
    assert summary["data"]["count"] == 1
    assert "private query" not in str(summary)
    assert "private Wiki excerpt" not in str(summary)
    assert summary["evidence"][0]["asset_id"] == "wiki_asset"


def test_http_summary_handles_non_json_response() -> None:
    class _InvalidResponse:
        status_code = 502

        @staticmethod
        def json():
            raise ValueError("not json")

    assert _http_summary(_InvalidResponse()) == {"http_status": 502, "status": "invalid_json"}


def test_collection_shadow_summaries_keep_only_canonical_discovery_facts() -> None:
    response = _Response({"status": "ok", "data": {"collections": [{"id": "collection_1"}]}})
    assert _collection_summary(response) == {"http_status": 200, "status": "ok", "collection_count": 1}

    uri = "knowledge://spaces/space_1/collections/collection_1"
    listed = _Response({"result": {"resources": [{"uri": uri}]}})
    assert _mcp_collection_summary(listed, collection_uri=uri)["collection_uri_advertised"] is True
    read = _Response({"result": {"structuredContent": {"status": "ok"}, "contents": [{"uri": uri, "text": "redacted"}]}})
    assert _mcp_collection_summary(read, collection_uri=uri) == {
        "http_status": 200,
        "status": "ok",
        "canonical_uri_read": True,
    }


def test_materialize_catalog_normalizes_relative_wiki_root(monkeypatch, tmp_path: Path) -> None:
    root = Path(__file__).parents[2]
    monkeypatch.chdir(root)
    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "page.md").write_text("# Local page\n\nagent boundary\n", encoding="utf-8")

    relative_wiki_root = Path(os.path.relpath(wiki_root, root))
    materialized = _materialize_catalog(
        Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"),
        tmp_path / "catalog.sqlite3",
        relative_wiki_root,
    )

    assert materialized["pages"] == 1
    assert all(path.is_absolute() for path in materialized["file_bindings"].values())


def test_catalog_snapshot_rejects_symlink_source(tmp_path: Path) -> None:
    resolved_tmp_path = tmp_path.resolve()
    source = resolved_tmp_path / "catalog.sqlite3"
    sqlite3.connect(source).close()
    link = resolved_tmp_path / "catalog-link.sqlite3"
    link.symlink_to(source)

    with pytest.raises(OSError, match="symlink"):
        _snapshot_catalog(link, resolved_tmp_path / "snapshot.sqlite3")


def test_catalog_snapshot_rejects_source_swap_between_check_and_connect(monkeypatch, tmp_path: Path) -> None:
    resolved_tmp_path = tmp_path.resolve()
    source = resolved_tmp_path / "catalog.sqlite3"
    alternate = resolved_tmp_path / "alternate.sqlite3"
    sqlite3.connect(source).close()
    sqlite3.connect(alternate).close()
    real_connect = wiki_shadow.sqlite3.connect
    calls = 0

    def connect(database, *args, **kwargs):
        nonlocal calls
        calls += 1
        connection = real_connect(database, *args, **kwargs)
        if calls == 1:
            source.unlink()
            source.symlink_to(alternate)
        return connection

    monkeypatch.setattr(wiki_shadow.sqlite3, "connect", connect)
    with pytest.raises(OSError, match="changed"):
        _snapshot_catalog(source, resolved_tmp_path / "snapshot.sqlite3")


def test_catalog_snapshot_includes_committed_wal_pages(tmp_path: Path) -> None:
    resolved_tmp_path = tmp_path.resolve()
    source = resolved_tmp_path / "catalog.sqlite3"
    connection = sqlite3.connect(source)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA wal_autocheckpoint=0")
        connection.execute("CREATE TABLE marker (value TEXT NOT NULL)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        connection.execute("INSERT INTO marker(value) VALUES ('wal-only-version')")
        connection.commit()
        assert (resolved_tmp_path / "catalog.sqlite3-wal").is_file()

        target = resolved_tmp_path / "snapshot.sqlite3"
        _snapshot_catalog(source, target)
        with sqlite3.connect(target) as snapshot:
            assert snapshot.execute("SELECT value FROM marker").fetchone()[0] == "wal-only-version"
    finally:
        connection.close()


def test_safe_pages_rejects_symlinked_root_ancestor(tmp_path: Path) -> None:
    resolved_tmp_path = tmp_path.resolve()
    real_parent = resolved_tmp_path / "real-parent"
    real_root = real_parent / "wiki"
    real_root.mkdir(parents=True)
    (real_root / "page.md").write_text("# Page\n", encoding="utf-8")
    linked_parent = resolved_tmp_path / "linked-parent"
    linked_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(OSError, match="symlink"):
        _safe_pages(linked_parent / "wiki")
