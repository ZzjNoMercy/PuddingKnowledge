from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

from knowledge_platform.catalog.providers import (
    LocalProviderObservationError,
    deployment_manifest_from_provider_handles,
    observe_local_provider_handles,
)


def test_local_provider_handles_use_real_file_digests_and_injected_vector_probe(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    (wiki / "page.md").write_text("page", encoding="utf-8")
    handles = observe_local_provider_handles(
        catalog_path=catalog,
        wiki_root=wiki,
        vector_uri="http://127.0.0.1:19530",
        text_collection="text_collection",
        image_collection="image_collection",
        vector_probe=lambda *_: True,
    )

    assert [handle.kind for handle in handles] == ["blob", "catalog", "vector_index", "wiki_root"]
    assert all(handle.ready for handle in handles)
    assert handles[0].locator_digest.startswith("sha256:")
    manifest = deployment_manifest_from_provider_handles(deployment_revision="local-v1", handles=handles)
    assert {artifact.kind for artifact in manifest.artifacts} == {"catalog", "blob", "vector_index", "wiki_root"}


def test_vector_observation_is_blocked_when_loopback_provider_is_unavailable(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    handles = observe_local_provider_handles(
        catalog_path=catalog,
        wiki_root=wiki,
        vector_uri="http://127.0.0.1:1",
        text_collection="text_collection",
        image_collection="image_collection",
    )
    vector = next(handle for handle in handles if handle.kind == "vector_index")
    assert vector.status == "blocked"
    assert all(handle.kind != "vector_index" or not handle.ready for handle in handles)


def test_vector_observation_rejects_non_loopback_and_bad_collection_ids() -> None:
    handles = observe_local_provider_handles(
        catalog_path=Path("/not-used"),
        wiki_root=Path("/not-used"),
        vector_uri="https://example.com:19530",
        text_collection="text_collection",
        image_collection="image_collection",
    )
    assert next(handle for handle in handles if handle.kind == "vector_index").status == "blocked"
    with pytest.raises(LocalProviderObservationError, match="collection"):
        from knowledge_platform.catalog.providers import _validate_vector_config

        _validate_vector_config("http://127.0.0.1:19530", "../text", "image_collection")


def test_vector_observation_requires_both_collections_and_minimum_schema(monkeypatch) -> None:
    from knowledge_platform.catalog import providers

    class _Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

    class _Client:
        def __init__(self, **_kwargs):
            pass

        def list_collections(self):
            return ["text_collection", "image_collection"]

        def describe_collection(self, *, collection_name):
            fields = [{"name": name} for name in ["id", "doc_id", "text", "embedding"]]
            if collection_name == "image_collection":
                fields = fields[:-1]
            return {"fields": fields}

    monkeypatch.setattr(providers.socket, "create_connection", lambda *_args, **_kwargs: _Connection())
    monkeypatch.setitem(sys.modules, "pymilvus", types.SimpleNamespace(MilvusClient=_Client))

    assert providers._probe_vector_collections(
        vector_uri="http://127.0.0.1:19530",
        text_collection="text_collection",
        image_collection="image_collection",
        probe=None,
    ) is False


def test_manifest_builder_rejects_blocked_provider_handle(tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    wiki = tmp_path / "wiki"
    wiki.mkdir()
    handles = observe_local_provider_handles(
        catalog_path=catalog,
        wiki_root=wiki,
        vector_uri="http://127.0.0.1:1",
        text_collection="text_collection",
        image_collection="image_collection",
    )
    with pytest.raises(LocalProviderObservationError, match="not all observed"):
        deployment_manifest_from_provider_handles(deployment_revision="local-v1", handles=handles)
