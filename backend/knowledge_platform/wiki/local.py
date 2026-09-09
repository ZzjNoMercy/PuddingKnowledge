"""Explicit local adapters for the Phase 7 Wiki compilation shadow.

These adapters are intentionally small and deterministic.  A caller must bind
the source Asset to a concrete local file; Catalog metadata is never used to
discover a path.  The adapters are suitable for an isolated rehearsal only,
not for the active runtime or for production connector credentials.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import BinaryIO

from knowledge_contracts import is_valid_knowledge_uri

from .ports import (
    RawSnapshot,
    WikiCompilationClaim,
    WikiDraft,
    WikiValidationResult,
)

_MAX_SOURCE_BYTES = 512 * 1024 * 1024
_MAX_CONTEXT_CHARS = 96_000
_MAX_MARKDOWN_CHARS = 256_000
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_UNSAFE_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def _safe_absolute_path(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_absolute() or len(candidate.parts) < 2:
        raise OSError("Wiki path must be absolute")
    cursor = candidate
    while True:
        if any(component in {"", ".", ".."} for component in cursor.parts[1:]):
            raise OSError("Wiki path contains an unsafe component")
        if cursor.is_symlink():
            raise OSError("Wiki source path contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _safe_output_root(path: Path) -> Path:
    return _safe_absolute_path(path)


@contextmanager
def _open_source(path: Path, *, max_bytes: int) -> Iterator[tuple[BinaryIO, int]]:
    """Open a caller-bound file through directory fds, refusing parent links."""

    candidate = _safe_absolute_path(path)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    current_fd = os.open(os.sep, os.O_RDONLY | directory)
    stream: BinaryIO | None = None
    descriptor: int | None = None
    try:
        for component in candidate.parts[1:-1]:
            next_fd = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        descriptor = os.open(candidate.parts[-1], os.O_RDONLY | nofollow, dir_fd=current_fd)
        stream = os.fdopen(descriptor, "rb")
        descriptor = None
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("Wiki source is not a regular file")
        if metadata.st_size > max_bytes:
            raise OSError("Wiki source exceeds the local size bound")
        yield stream, metadata.st_size
    finally:
        if stream is not None:
            stream.close()
        if descriptor is not None:
            os.close(descriptor)
        os.close(current_fd)


def _read_verified_text(path: Path, *, expected_digest: str) -> tuple[str, str, int]:
    content, actual, size = _read_verified_bytes(path, expected_digest=expected_digest)
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise ValueError("Wiki source is not UTF-8 text") from error
    if not text.strip() or _UNSAFE_CONTROL_RE.search(text):
        raise ValueError("Wiki source text is invalid")
    return text, actual, size


def _read_verified_bytes(path: Path, *, expected_digest: str) -> tuple[bytes, str, int]:
    if not _DIGEST_RE.fullmatch(expected_digest):
        raise ValueError("Wiki source digest is invalid")
    with _open_source(path, max_bytes=_MAX_SOURCE_BYTES) as (stream, size):
        digest = hashlib.sha256()
        chunks: list[bytes] = []
        while True:
            chunk = stream.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            chunks.append(chunk)
        actual = f"sha256:{digest.hexdigest()}"
        if actual != expected_digest:
            raise ValueError("Wiki source bytes do not match the Catalog digest")
        return b"".join(chunks), actual, size


def _source_identity(source_uri: str) -> tuple[str, str]:
    parts = source_uri.removeprefix("knowledge://").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "assets" or not all(parts):
        raise ValueError("Wiki source URI must identify one Asset")
    if not all(_ID_RE.fullmatch(part) for part in (parts[1], parts[3])):
        raise ValueError("Wiki source URI contains an invalid identity")
    return parts[1], parts[3]


class LocalRawSnapshotRepository:
    """Read one caller-bound UTF-8 Asset as an immutable Raw Snapshot."""

    def __init__(
        self,
        *,
        snapshot_id: str,
        source_revision: str,
        source_uri: str,
        path: Path,
        expected_digest: str,
    ) -> None:
        if not _ID_RE.fullmatch(snapshot_id) or not source_revision.strip():
            raise ValueError("Raw Snapshot identity is invalid")
        if not is_valid_knowledge_uri(source_uri):
            raise ValueError("Raw Snapshot source URI is invalid")
        _source_space, source_asset_id = _source_identity(source_uri)
        if source_asset_id != snapshot_id:
            raise ValueError("Raw Snapshot source Asset does not match snapshot identity")
        self._snapshot_id = snapshot_id
        self._source_revision = source_revision
        self._source_uri = source_uri
        self._path = path
        self._expected_digest = expected_digest

    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        if (snapshot_id, source_revision) != (self._snapshot_id, self._source_revision):
            raise LookupError("Raw Snapshot is unavailable")
        content, digest, _size = _read_verified_text(self._path, expected_digest=self._expected_digest)
        return RawSnapshot(
            snapshot_id=self._snapshot_id,
            source_revision=self._source_revision,
            source_uri=self._source_uri,
            content=content,
            content_digest=digest,
        )


class LocalImmutableRawSnapshotRepository(LocalRawSnapshotRepository):
    """Materialize one explicit Asset file as an immutable local Raw Snapshot.

    The source path is an input binding only.  The worker reads the snapshot
    copy after the first call, so later source-file changes cannot silently
    change a queued compilation.  The snapshot manifest records identity and
    digests only; it never records the physical source path or source text.
    """

    def __init__(
        self,
        *,
        snapshot_root: Path,
        snapshot_id: str,
        source_revision: str,
        source_uri: str,
        path: Path,
        expected_digest: str,
    ) -> None:
        super().__init__(
            snapshot_id=snapshot_id,
            source_revision=source_revision,
            source_uri=source_uri,
            path=path,
            expected_digest=expected_digest,
        )
        self._snapshot_root = _safe_output_root(snapshot_root)
        self._snapshot_path = self._snapshot_root / f"{snapshot_id}-{expected_digest.removeprefix('sha256:')[:16]}.md"
        self._manifest_path = self._snapshot_root / "manifest.jsonl"

    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        if (snapshot_id, source_revision) != (self._snapshot_id, self._source_revision):
            raise LookupError("Raw Snapshot is unavailable")
        self._snapshot_root.mkdir(parents=True, exist_ok=True)
        if self._snapshot_path.is_symlink():
            raise OSError("Raw Snapshot path must not be a symlink")
        if not self._snapshot_path.exists():
            content, digest, size = _read_verified_bytes(self._path, expected_digest=self._expected_digest)
            temporary = self._snapshot_path.with_name(f".{self._snapshot_path.name}.tmp")
            if temporary.exists() or temporary.is_symlink():
                raise FileExistsError("Raw Snapshot temporary output already exists")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    descriptor = -1
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.link(temporary, self._snapshot_path)
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary.exists() or temporary.is_symlink():
                    temporary.unlink()
            record = {
                "snapshot_id": self._snapshot_id,
                "source_revision": self._source_revision,
                "source_uri": self._source_uri,
                "content_digest": digest,
                "bytes": size,
                "created_at": datetime.now(UTC).isoformat(),
            }
            if self._manifest_path.is_symlink():
                raise OSError("Raw Snapshot manifest path must not be a symlink")
            self._manifest_path.write_text(json.dumps(record, sort_keys=True) + "\n", encoding="utf-8")
        content, digest, _size = _read_verified_text(self._snapshot_path, expected_digest=self._expected_digest)
        return RawSnapshot(
            snapshot_id=self._snapshot_id,
            source_revision=self._source_revision,
            source_uri=self._source_uri,
            content=content,
            content_digest=digest,
        )


class BoundedWikiContextService:
    """Build a bounded context without interpreting source text as commands."""

    async def build_context(self, snapshot: RawSnapshot) -> str:
        return snapshot.content[:_MAX_CONTEXT_CHARS]


class DeterministicWikiModelGateway:
    """Local model stand-in used only to exercise the application boundary."""

    def __init__(self, titles: Mapping[str, str]) -> None:
        self._titles = {str(key): str(value).strip() for key, value in titles.items()}

    async def generate(self, *, context: str, snapshot: RawSnapshot) -> WikiDraft:
        title = self._titles.get(snapshot.snapshot_id) or "Compiled Wiki"
        title = title[:240].strip() or "Compiled Wiki"
        return WikiDraft(
            path=f"wiki/{snapshot.snapshot_id}.md",
            title=title,
            markdown=f"# {title}\n\n{context[:_MAX_MARKDOWN_CHARS - len(title) - 8]}",
            source_snapshot_id=snapshot.snapshot_id,
            source_revision=snapshot.source_revision,
        )


class LocalWikiDraftValidator:
    """Validate the generated draft and return a deterministic receipt."""

    async def validate(self, draft: WikiDraft, *, snapshot: RawSnapshot) -> WikiValidationResult:
        if (draft.source_snapshot_id, draft.source_revision) != (snapshot.snapshot_id, snapshot.source_revision):
            return WikiValidationResult(valid=False, errors=("source identity mismatch",))
        if len(draft.markdown) > _MAX_MARKDOWN_CHARS or not draft.markdown.lstrip().startswith("# "):
            return WikiValidationResult(valid=False, errors=("markdown shape is invalid",))
        receipt = "receipt_" + hashlib.sha256(
            f"{snapshot.content_digest}\0{draft.path}\0{draft.markdown}".encode()
        ).hexdigest()[:48]
        return WikiValidationResult(valid=True, receipt_id=receipt)


class LocalWikiPublishingService:
    """Publish into an isolated directory with no overwrite and no path output."""

    def __init__(self, *, root: Path, space_id: str) -> None:
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("Wiki space identity is invalid")
        self._root = root.expanduser().absolute()
        self._space_id = space_id
        self.publish_count = 0

    @property
    def _manifest_path(self) -> Path:
        return self._root / "publication-manifest.jsonl"

    def _append_manifest(self, record: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or self._manifest_path.is_symlink():
            raise OSError("Wiki publication manifest path must not be a symlink")
        lock_path = self._root / ".publication-manifest.lock"
        if lock_path.is_symlink():
            raise OSError("Wiki publication manifest lock path must not be a symlink")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                existing = self._manifest_path.read_text(encoding="utf-8") if self._manifest_path.is_file() else ""
                self._manifest_path.write_text(
                    existing + json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    @staticmethod
    def _resource_identity(resource_uri: str) -> tuple[str, str]:
        parts = resource_uri.removeprefix("knowledge://").split("/")
        if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "wiki" or not all(parts):
            raise ValueError("Wiki resource URI is invalid")
        if not all(_ID_RE.fullmatch(part) for part in (parts[1], parts[3])):
            raise ValueError("Wiki resource URI contains an invalid identity")
        return parts[1], parts[3]

    def _manifest_records(self) -> list[dict[str, object]]:
        if not self._manifest_path.is_file():
            return []
        if self._root.is_symlink() or self._manifest_path.is_symlink():
            raise OSError("Wiki publication manifest path must not be a symlink")
        records: list[dict[str, object]] = []
        for line in self._manifest_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                value = json.loads(line)
                if not isinstance(value, dict):
                    raise ValueError("Wiki publication manifest record must be an object")
                records.append(value)
        return records

    async def publish(self, draft, *, snapshot: RawSnapshot, idempotency_key: str) -> str:
        del idempotency_key
        if _source_identity(snapshot.source_uri)[0] != self._space_id:
            raise ValueError("Wiki source Space does not match publisher")
        relative = Path(draft.draft.path)
        if relative.is_absolute() or any(part in {"", ".", ".."} for part in relative.parts):
            raise ValueError("Wiki draft path is not portable")
        root = _safe_output_root(self._root)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        for parent in (destination.parent,):
            if parent.is_symlink():
                raise OSError("Wiki publish directory must not be a symlink")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Wiki resource already exists")
        temporary = destination.with_name(f".{destination.name}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise FileExistsError("Wiki temporary output already exists")
        temporary.write_text(draft.draft.markdown, encoding="utf-8")
        # ``os.replace`` would silently overwrite a concurrently published
        # resource.  A hard-link create gives this shadow the desired
        # no-overwrite commit on the same temporary filesystem.
        os.link(temporary, destination)
        temporary.unlink()
        resource_uri = f"knowledge://spaces/{self._space_id}/wiki/{snapshot.snapshot_id}"
        self._append_manifest(
            {
                "status": "published",
                "resource_uri": resource_uri,
                "snapshot_id": snapshot.snapshot_id,
                "source_revision": snapshot.source_revision,
                "source_digest": snapshot.content_digest,
                "published_digest": "sha256:" + hashlib.sha256(draft.draft.markdown.encode("utf-8")).hexdigest(),
                "published_bytes": len(draft.draft.markdown.encode("utf-8")),
                "validation_receipt": draft.receipt_id,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self.publish_count += 1
        return resource_uri

    def lint_published(self, resource_uri: str) -> dict[str, object]:
        """Check one published resource against its append-only publication record."""

        space_id, asset_id = self._resource_identity(resource_uri)
        if space_id != self._space_id:
            raise ValueError("Wiki resource Space does not match publisher")
        records = [item for item in self._manifest_records() if item.get("resource_uri") == resource_uri]
        if not records:
            raise LookupError("Wiki publication record is unavailable")
        latest = records[-1]
        destination = self._root / "wiki" / f"{asset_id}.md"
        if latest.get("status") != "published" or not destination.is_file() or destination.is_symlink():
            return {"ok": False, "status": str(latest.get("status") or "unknown")}
        content = destination.read_bytes()
        digest = "sha256:" + hashlib.sha256(content).hexdigest()
        expected = str(latest.get("published_digest") or "")
        return {"ok": digest == expected, "status": "published", "content_digest": digest}

    def retire(self, resource_uri: str) -> dict[str, object]:
        """Retire a local published page without leaving an active file behind."""

        space_id, asset_id = self._resource_identity(resource_uri)
        if space_id != self._space_id:
            raise ValueError("Wiki resource Space does not match publisher")
        records = [item for item in self._manifest_records() if item.get("resource_uri") == resource_uri]
        if not records:
            raise LookupError("Wiki publication record is unavailable")
        latest = records[-1]
        if latest.get("status") == "retired":
            return {"retired": True, "already_retired": True, "resource_uri": resource_uri}
        if latest.get("status") != "published":
            raise ValueError("Wiki resource is not published")
        source = self._root / "wiki" / f"{asset_id}.md"
        archive = self._root / "retired" / "wiki" / f"{asset_id}.md"
        if not source.is_file() or source.is_symlink():
            raise FileNotFoundError("published Wiki resource is unavailable")
        archive.parent.mkdir(parents=True, exist_ok=True)
        if archive.exists() or archive.is_symlink():
            raise FileExistsError("retired Wiki resource already exists")
        os.link(source, archive)
        source.unlink()
        self._append_manifest(
            {
                "status": "retired",
                "resource_uri": resource_uri,
                "snapshot_id": latest.get("snapshot_id", ""),
                "source_revision": latest.get("source_revision", ""),
                "source_digest": latest.get("source_digest", ""),
                "published_digest": latest.get("published_digest", ""),
                "validation_receipt": latest.get("validation_receipt", ""),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        return {"retired": True, "already_retired": False, "resource_uri": resource_uri}


class InMemoryWikiCompilationJobStore:
    """Single-process claim store for deterministic shadow idempotency."""

    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self._completed: dict[str, str] = {}

    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        if idempotency_key in self._completed:
            return WikiCompilationClaim(False, self._completed[idempotency_key])
        if idempotency_key in self._claimed:
            return WikiCompilationClaim(False)
        self._claimed.add(idempotency_key)
        return WikiCompilationClaim(True)

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        self._claimed.discard(idempotency_key)
        self._completed[idempotency_key] = resource_uri

    async def release(self, *, idempotency_key: str) -> None:
        self._claimed.discard(idempotency_key)
