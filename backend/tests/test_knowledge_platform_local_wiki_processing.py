from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import types
from pathlib import Path

import pytest

from knowledge_platform.local.wiki import build_wiki_services, load_wiki_config
from knowledge_platform.wiki.compiler import WikiCompilationError, WikiCompilationRequest
from knowledge_platform.wiki.ports import WikiDraft


def _catalog(path: Path, source: Path | None = None) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE seed (id INTEGER PRIMARY KEY)")
    if source is not None:
        digest = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
        connection.execute("""CREATE TABLE knowledge_assets (
            id TEXT PRIMARY KEY, space_id TEXT, kind TEXT, title TEXT,
            description TEXT, mime_type TEXT, source_type TEXT, source_uri TEXT,
            revision TEXT, content_digest TEXT, permissions_json TEXT,
            metadata_json TEXT, created_at TEXT, updated_at TEXT
        )""")
        connection.execute(
            "INSERT INTO knowledge_assets VALUES (?, ?, 'document', 'Source', '', 'text/markdown', 'local', ?, ?, ?, '{}', '{}', '', '')",
            ("asset_source", "space_kb_default", "knowledge://spaces/space_kb_default/assets/asset_source", digest, digest),
        )
    connection.commit()
    connection.close()


def _config(source: Path) -> dict:
    return {
        "version": 1,
        "space_id": "space_kb_default",
        "assets": {"asset_source": str(source)},
        "model": {"endpoint": "http://127.0.0.1:9999/v1/chat/completions", "model": "test-model"},
    }


class _FakeModel:
    def __init__(self, config: dict) -> None:
        self.config = config

    async def generate(self, *, context: str, snapshot):
        return WikiDraft(
            path="wiki/asset_source.md",
            title="Compiled",
            markdown="# Compiled\n\n" + context,
            source_snapshot_id=snapshot.snapshot_id,
            source_revision=snapshot.source_revision,
        )


def _install_fake_model(monkeypatch: pytest.MonkeyPatch) -> None:
    module = types.ModuleType("knowledge_platform.local.wiki_model")
    module.HttpWikiModelGateway = _FakeModel
    monkeypatch.setitem(sys.modules, "knowledge_platform.local.wiki_model", module)


def test_config_requires_independent_space_and_absolute_sources(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    config_path = tmp_path / "wiki.json"
    config_path.write_text(json.dumps(_config(source)), encoding="utf-8")
    loaded = load_wiki_config(config_path)
    assert loaded["space_id"] == "space_kb_default"
    assert loaded["assets"]["asset_source"]["path"] == str(source)

    invalid = dict(_config(source), space_id="other-space")
    config_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="Space"):
        load_wiki_config(config_path)

    invalid = _config(source)
    invalid["assets"] = {"asset_source": "relative.md"}
    config_path.write_text(json.dumps(invalid), encoding="utf-8")
    with pytest.raises(ValueError, match="absolute"):
        load_wiki_config(config_path)


def test_compile_persists_publication_and_restarts_from_dynamic_reader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_model(monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("bounded source", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, source)
    services = build_wiki_services(_config(source), catalog, tmp_path / "state")
    revision = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = services.wiki_compilation
    compilation = WikiCompilationRequest(
        snapshot_id="asset_source",
        source_revision=revision,
        source_uri="knowledge://spaces/space_kb_default/assets/asset_source",
        content_digest=revision,
        idempotency_key="compile-once",
    )
    first = asyncio.run(request.compile(compilation))
    assert first.resource_uri in services.published_bindings()
    assert services.read_published(first.resource_uri).startswith(b"# Compiled")

    restarted = build_wiki_services(_config(source), catalog, tmp_path / "state")
    second = asyncio.run(restarted.wiki_compilation.compile(compilation))
    assert second == first
    assert restarted.read_published(first.resource_uri) == services.read_published(first.resource_uri)


def test_same_key_cannot_be_reused_for_another_source_fingerprint(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_model(monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, source)
    services = build_wiki_services(_config(source), catalog, tmp_path / "state")
    revision = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    good = WikiCompilationRequest(
        "asset_source", revision, "knowledge://spaces/space_kb_default/assets/asset_source", revision, "same-key"
    )
    asyncio.run(services.wiki_compilation.compile(good))
    changed = WikiCompilationRequest(
        "asset_source", "different-revision", good.source_uri, revision, "same-key"
    )
    with pytest.raises(ValueError, match="another request"):
        asyncio.run(services.wiki_compilation.compile(changed))


def test_source_digest_or_revision_mismatch_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _install_fake_model(monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, source)
    services = build_wiki_services(_config(source), catalog, tmp_path / "state")
    with pytest.raises(ValueError, match="revision"):
        asyncio.run(
            services.wiki_compilation.compile(
                WikiCompilationRequest(
                    "asset_source", "rev-wrong", "knowledge://spaces/space_kb_default/assets/asset_source", "sha256:" + "0" * 64, "bad"
                )
            )
        )


def test_concurrent_claim_does_not_release_another_invocation_and_cancel_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, source)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingModel(_FakeModel):
        calls = 0

        async def generate(self, *, context: str, snapshot):
            type(self).calls += 1
            if type(self).calls in (1, 2):
                started.set()
                await release.wait()
            return await super().generate(context=context, snapshot=snapshot)

    module = types.ModuleType("knowledge_platform.local.wiki_model")
    module.HttpWikiModelGateway = BlockingModel
    monkeypatch.setitem(sys.modules, "knowledge_platform.local.wiki_model", module)
    services = build_wiki_services(_config(source), catalog, tmp_path / "state")
    revision = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    request = WikiCompilationRequest(
        "asset_source", revision, "knowledge://spaces/space_kb_default/assets/asset_source", revision, "concurrent"
    )

    async def scenario() -> None:
        first = asyncio.create_task(services.wiki_compilation.compile(request))
        await started.wait()
        with pytest.raises(WikiCompilationError, match="already in progress"):
            await services.wiki_compilation.compile(request)
        release.set()
        await first
        assert BlockingModel.calls == 1

        # A cancelled owner releases only its own claim.  The retained
        # fingerprint row permits the exact request to retry.
        started.clear()
        release.clear()
        cancelled = asyncio.create_task(services.wiki_compilation.compile(
            WikiCompilationRequest(
                "asset_source", revision, "knowledge://spaces/space_kb_default/assets/asset_source", revision, "cancelled"
            )
        ))
        await started.wait()
        cancelled.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled
        retry = await services.wiki_compilation.compile(
            WikiCompilationRequest(
                "asset_source", revision, "knowledge://spaces/space_kb_default/assets/asset_source", revision, "cancelled"
            )
        )
        assert retry.resource_uri

    asyncio.run(scenario())


def test_process_death_releases_flock_and_running_request_can_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _install_fake_model(monkeypatch)
    source = tmp_path / "source.md"
    source.write_text("source", encoding="utf-8")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, source)
    state = tmp_path / "state"
    revision = "sha256:" + hashlib.sha256(source.read_bytes()).hexdigest()
    child = """
import asyncio, os, sys
from pathlib import Path
from knowledge_platform.local.wiki import _PersistentWikiStore
from knowledge_platform.wiki.compiler import WikiCompilationRequest
async def main():
    store = _PersistentWikiStore(database_path=Path(sys.argv[1]), state_root=Path(sys.argv[2]), space_id='space_kb_default')
    request = WikiCompilationRequest('asset_source', sys.argv[3], 'knowledge://spaces/space_kb_default/assets/asset_source', sys.argv[3], 'crash-key')
    claim = await store.claim(request=request)
    assert claim.acquired
    os._exit(0)
asyncio.run(main())
"""
    env = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1]))
    result = subprocess.run(
        [sys.executable, "-c", child, str(catalog), str(state), revision], env=env, check=False
    )
    assert result.returncode == 0
    services = build_wiki_services(_config(source), catalog, state)
    from knowledge_platform.wiki.compiler import WikiCompilationRequest

    replay = asyncio.run(
        services.wiki_compilation.compile(
            WikiCompilationRequest(
                "asset_source", revision, "knowledge://spaces/space_kb_default/assets/asset_source", revision, "crash-key"
            )
        )
    )
    assert replay.resource_uri in services.published_bindings()


def test_failed_request_keeps_its_idempotency_fingerprint(tmp_path, monkeypatch):
    _install_fake_model(monkeypatch)
    source = tmp_path / 'source.md'
    source.write_text('source')
    catalog = tmp_path / 'catalog.sqlite3'
    _catalog(catalog, source)
    services = build_wiki_services(_config(source), catalog, tmp_path / 'state')
    digest = 'sha256:' + hashlib.sha256(source.read_bytes()).hexdigest()
    uri = 'knowledge://spaces/space_kb_default/assets/asset_source'
    invalid = WikiCompilationRequest('asset_source', 'incorrect', uri, digest, 'failed-key')
    with pytest.raises(ValueError):
        asyncio.run(services.wiki_compilation.compile(invalid))
    with pytest.raises(ValueError, match='another request'):
        asyncio.run(services.wiki_compilation.compile(WikiCompilationRequest('asset_source', digest, uri, digest, 'failed-key')))
