from __future__ import annotations

import asyncio

import pytest

from knowledge_platform.wiki import (
    RawSnapshot,
    WikiCompilationClaim,
    WikiCompilationError,
    WikiCompilationRequest,
    WikiCompilationWorker,
    WikiDraft,
    WikiValidationResult,
)


class FakeAdapters:
    def __init__(self, *, valid: bool = True, draft_identity: tuple[str, str] | None = None) -> None:
        self.calls: list[str] = []
        self.valid = valid
        self.draft_identity = draft_identity
        self.published = False
        self.completed: dict[str, str] = {}
        self.claimed: set[str] = set()
        self.snapshot = RawSnapshot(
            snapshot_id="snapshot-1",
            source_revision="revision-1",
            source_uri="knowledge://raw/snapshot-1",
            content="Source content",
            content_digest="sha256:source",
        )

    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        self.calls.append("snapshot")
        return self.snapshot

    async def build_context(self, snapshot: RawSnapshot) -> str:
        self.calls.append("context")
        return snapshot.content

    async def generate(self, *, context: str, snapshot: RawSnapshot) -> WikiDraft:
        self.calls.append("model")
        snapshot_id, revision = self.draft_identity or (snapshot.snapshot_id, snapshot.source_revision)
        return WikiDraft(
            path="topics/example.md",
            title="Example",
            markdown="# Example\n\nGrounded content.",
            source_snapshot_id=snapshot_id,
            source_revision=revision,
        )

    async def validate(self, draft: WikiDraft, *, snapshot: RawSnapshot) -> WikiValidationResult:
        self.calls.append("validate")
        return WikiValidationResult(
            valid=self.valid,
            errors=() if self.valid else ("missing citation",),
            receipt_id="receipt-1" if self.valid else "",
        )

    async def publish(self, draft, *, snapshot: RawSnapshot, idempotency_key: str) -> str:
        self.calls.append("publish")
        self.published = True
        return "knowledge://wiki/topics/example"

    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        self.calls.append("claim")
        if idempotency_key in self.completed:
            return WikiCompilationClaim(False, self.completed[idempotency_key])
        if idempotency_key in self.claimed:
            return WikiCompilationClaim(False)
        self.claimed.add(idempotency_key)
        return WikiCompilationClaim(True)

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        self.calls.append("complete")
        self.completed[idempotency_key] = resource_uri

    async def release(self, *, idempotency_key: str) -> None:
        self.calls.append("release")
        self.claimed.discard(idempotency_key)


def make_worker(adapters: FakeAdapters) -> WikiCompilationWorker:
    return WikiCompilationWorker(
        snapshots=adapters,
        context=adapters,
        model=adapters,
        validator=adapters,
        publisher=adapters,
        jobs=adapters,
    )


def request() -> WikiCompilationRequest:
    return WikiCompilationRequest(
        snapshot_id="snapshot-1",
        source_revision="revision-1",
        source_uri="knowledge://raw/snapshot-1",
        content_digest="sha256:source",
        idempotency_key="compile-1",
    )


def test_compiler_runs_in_dependency_order_and_publishes_only_after_validation() -> None:
    adapters = FakeAdapters()

    result = asyncio.run(make_worker(adapters).compile(request()))

    assert adapters.calls == ["claim", "snapshot", "context", "model", "validate", "publish", "complete"]
    assert result.resource_uri == "knowledge://wiki/topics/example"
    assert adapters.published is True


def test_invalid_draft_stops_before_publish() -> None:
    adapters = FakeAdapters(valid=False)

    with pytest.raises(WikiCompilationError, match="validation failed"):
        asyncio.run(make_worker(adapters).compile(request()))

    assert adapters.calls == ["claim", "snapshot", "context", "model", "validate", "release"]
    assert adapters.published is False


def test_draft_source_identity_is_fenced_before_validation() -> None:
    adapters = FakeAdapters(draft_identity=("other-snapshot", "revision-1"))

    with pytest.raises(WikiCompilationError, match="source identity"):
        asyncio.run(make_worker(adapters).compile(request()))

    assert adapters.calls == ["claim", "snapshot", "context", "model", "release"]
    assert adapters.published is False


def test_snapshot_source_identity_is_fenced_against_request() -> None:
    adapters = FakeAdapters()
    adapters.snapshot = RawSnapshot(
        snapshot_id="other-snapshot",
        source_revision="revision-1",
        source_uri="knowledge://raw/other-snapshot",
        content="Source content",
        content_digest="sha256:source",
    )

    with pytest.raises(WikiCompilationError, match="snapshot identity"):
        asyncio.run(make_worker(adapters).compile(request()))

    assert adapters.calls == ["claim", "snapshot", "release"]


def test_relative_wiki_path_rejects_absolute_and_traversal_paths() -> None:
    with pytest.raises(ValueError):
        WikiDraft("/topics/example.md", "Example", "# Example", "snapshot-1", "revision-1")
    with pytest.raises(ValueError):
        WikiDraft("topics/../example.md", "Example", "# Example", "snapshot-1", "revision-1")
    for path in (r"..\outside\secret.md", r"C:\outside\secret.md", r"C:/outside/secret.md", r"\\server\share\secret.md"):
        with pytest.raises(ValueError):
            WikiDraft(path, "Example", "# Example", "snapshot-1", "revision-1")


def test_snapshot_source_uri_rejects_traversal() -> None:
    with pytest.raises(ValueError, match="knowledge"):
        RawSnapshot("snapshot-1", "revision-1", "knowledge://../etc/passwd", "content", "sha256:content")
    with pytest.raises(ValueError, match="knowledge"):
        WikiCompilationRequest(
            "snapshot-1",
            "revision-1",
            "knowledge://../etc/passwd",
            "sha256:content",
            "compile-1",
        )
    with pytest.raises(ValueError, match="resource_uri"):
        from knowledge_platform.wiki.compiler import WikiCompilationResult

        WikiCompilationResult("snapshot-1", "revision-1", "file:///etc/passwd")


def test_snapshot_read_failure_releases_compilation_claim() -> None:
    class FailingSnapshotAdapters(FakeAdapters):
        async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
            self.calls.append("snapshot")
            raise RuntimeError("snapshot backend unavailable")

    adapters = FailingSnapshotAdapters()
    with pytest.raises(RuntimeError, match="unavailable"):
        asyncio.run(make_worker(adapters).compile(request()))
    assert adapters.calls == ["claim", "snapshot", "release"]


def test_completed_idempotency_key_returns_existing_resource_without_republishing() -> None:
    adapters = FakeAdapters()
    worker = make_worker(adapters)

    first = asyncio.run(worker.compile(request()))
    adapters.calls.clear()
    second = asyncio.run(worker.compile(request()))

    assert second == first
    assert adapters.calls == ["claim"]
    assert adapters.published is True
