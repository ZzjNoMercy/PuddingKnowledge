from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path

import pytest

from knowledge_platform.wiki import (
    BoundedWikiContextService,
    DeterministicWikiModelGateway,
    InMemoryWikiCompilationJobStore,
    LocalImmutableRawSnapshotRepository,
    LocalRawSnapshotRepository,
    LocalWikiDraftValidator,
    LocalWikiPublishingService,
    WikiCompilationRequest,
    WikiCompilationWorker,
)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _worker(path: Path, output: Path, *, expected_digest: str | None = None) -> tuple[WikiCompilationWorker, LocalWikiPublishingService]:
    source_uri = "knowledge://spaces/space_local/assets/asset_source"
    publisher = LocalWikiPublishingService(root=output, space_id="space_local")
    worker = WikiCompilationWorker(
        snapshots=LocalRawSnapshotRepository(
            snapshot_id="asset_source",
            source_revision="source-revision-1",
            source_uri=source_uri,
            path=path,
            expected_digest=expected_digest or _digest(path),
        ),
        context=BoundedWikiContextService(),
        model=DeterministicWikiModelGateway({"asset_source": "Local source"}),
        validator=LocalWikiDraftValidator(),
        publisher=publisher,
        jobs=InMemoryWikiCompilationJobStore(),
    )
    return worker, publisher


def _request() -> WikiCompilationRequest:
    return WikiCompilationRequest(
        snapshot_id="asset_source",
        source_revision="source-revision-1",
        source_uri="knowledge://spaces/space_local/assets/asset_source",
        content_digest="sha256:" + "0" * 64,
        idempotency_key="compile-local-1",
    )


def test_local_wiki_compile_is_explicit_bounded_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nA local fact.", encoding="utf-8")
    worker, publisher = _worker(source, tmp_path / "published")
    request = _request()
    request = WikiCompilationRequest(
        snapshot_id=request.snapshot_id,
        source_revision=request.source_revision,
        source_uri=request.source_uri,
        content_digest=_digest(source),
        idempotency_key=request.idempotency_key,
    )

    first = asyncio.run(worker.compile(request))
    second = asyncio.run(worker.compile(request))

    assert first == second
    assert first.resource_uri == "knowledge://spaces/space_local/wiki/asset_source"
    assert publisher.publish_count == 1
    assert (tmp_path / "published" / "wiki" / "asset_source.md").read_text(encoding="utf-8").startswith("# Local source")


def test_local_wiki_source_digest_and_utf8_are_fenced(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_bytes(b"not the expected bytes")
    worker, publisher = _worker(source, tmp_path / "published", expected_digest="sha256:" + "1" * 64)

    with pytest.raises(ValueError, match="digest"):
        asyncio.run(worker.compile(_request()))
    assert publisher.publish_count == 0
    assert not (tmp_path / "published").exists()


def test_local_wiki_source_identity_and_parent_symlink_are_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("content", encoding="utf-8")
    with pytest.raises(ValueError, match="Asset"):
        LocalRawSnapshotRepository(
            snapshot_id="another_asset",
            source_revision="revision-1",
            source_uri="knowledge://spaces/space_local/assets/asset_source",
            path=source,
            expected_digest=_digest(source),
        )
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    linked_source = linked_root / "source.md"
    with pytest.raises(OSError, match="symlink"):
        repository = LocalRawSnapshotRepository(
            snapshot_id="asset_source",
            source_revision="revision-1",
            source_uri="knowledge://spaces/space_local/assets/asset_source",
            path=linked_source,
            expected_digest="sha256:" + "0" * 64,
        )
        asyncio.run(repository.get(snapshot_id="asset_source", source_revision="revision-1"))


def test_local_wiki_publisher_rejects_existing_output_for_new_idempotency_key(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("content", encoding="utf-8")
    worker, publisher = _worker(source, tmp_path / "published")
    request = _request()
    request = WikiCompilationRequest(
        snapshot_id=request.snapshot_id,
        source_revision=request.source_revision,
        source_uri=request.source_uri,
        content_digest=_digest(source),
        idempotency_key=request.idempotency_key,
    )
    asyncio.run(worker.compile(request))
    assert publisher.publish_count == 1
    with pytest.raises(FileExistsError):
        asyncio.run(worker.compile(WikiCompilationRequest(
            snapshot_id=request.snapshot_id,
            source_revision=request.source_revision,
            source_uri=request.source_uri,
            content_digest=request.content_digest,
            idempotency_key="compile-local-2",
        )))


def test_local_raw_snapshot_is_immutable_after_source_changes(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Original\n\nPinned source.", encoding="utf-8")
    repository = LocalImmutableRawSnapshotRepository(
        snapshot_root=tmp_path / "raw",
        snapshot_id="asset_source",
        source_revision="source-revision-1",
        source_uri="knowledge://spaces/space_local/assets/asset_source",
        path=source,
        expected_digest=_digest(source),
    )

    first = asyncio.run(repository.get(snapshot_id="asset_source", source_revision="source-revision-1"))
    source.write_text("# Changed\n\nThis must not enter the queued compilation.", encoding="utf-8")
    second = asyncio.run(repository.get(snapshot_id="asset_source", source_revision="source-revision-1"))

    assert first == second
    assert first.content == "# Original\n\nPinned source."
    manifest = (tmp_path / "raw" / "manifest.jsonl").read_text(encoding="utf-8")
    assert len([json.loads(line) for line in manifest.splitlines() if line]) == 1
    assert str(source) not in manifest


def test_local_wiki_publication_lint_and_retire_are_auditable_and_idempotent(tmp_path: Path) -> None:
    source = tmp_path / "source.md"
    source.write_text("# Source\n\nA local fact.", encoding="utf-8")
    output = tmp_path / "published"
    worker, publisher = _worker(source, output)
    request = _request()
    request = WikiCompilationRequest(
        snapshot_id=request.snapshot_id,
        source_revision=request.source_revision,
        source_uri=request.source_uri,
        content_digest=_digest(source),
        idempotency_key=request.idempotency_key,
    )

    result = asyncio.run(worker.compile(request))
    assert publisher.lint_published(result.resource_uri)["ok"] is True
    retired = publisher.retire(result.resource_uri)
    assert retired["retired"] is True
    assert publisher.retire(result.resource_uri)["already_retired"] is True
    assert not (output / "wiki" / "asset_source.md").exists()
    assert (output / "retired" / "wiki" / "asset_source.md").is_file()
    manifest = (output / "publication-manifest.jsonl").read_text(encoding="utf-8")
    assert '"status": "published"' in manifest
    assert '"status": "retired"' in manifest
