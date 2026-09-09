"""Isolated local gbrain projection adapter.

This writes a content-addressed Markdown source bundle only.  It deliberately
does not start gbrain, connect PostgreSQL, or mutate the legacy Claw runtime.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from datetime import UTC, datetime
from pathlib import Path

from .ports import GbrainProjectionRequest, GbrainProjectionResult

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


def _safe_root(path: Path) -> Path:
    candidate = path.expanduser().absolute()
    if not candidate.is_absolute() or len(candidate.parts) < 2:
        raise OSError("gbrain projection root must be absolute")
    cursor = candidate
    while True:
        if any(part in {"", ".", ".."} for part in cursor.parts[1:]) or cursor.is_symlink():
            raise OSError("gbrain projection root contains an unsafe symlink/path")
        if cursor.parent == cursor:
            return candidate
        cursor = cursor.parent


class LocalGbrainProjectionService:
    """Create an immutable local source projection with append-only audit."""

    def __init__(self, *, root: Path) -> None:
        self._root = _safe_root(root)
        self.projection_count = 0

    @property
    def _manifest_path(self) -> Path:
        return self._root / "projection-manifest.jsonl"

    def _append_manifest(self, record: dict[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or self._manifest_path.is_symlink():
            raise OSError("gbrain projection manifest must not be a symlink")
        lock_path = self._root / ".projection-manifest.lock"
        if lock_path.is_symlink():
            raise OSError("gbrain projection lock must not be a symlink")
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

    def _ensure_projection_directory(self, space_id: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        if self._root.is_symlink() or not self._root.is_dir():
            raise OSError("gbrain projection root must be a real directory")
        current = self._root / "sources"
        for directory in (current, current / space_id):
            if directory.is_symlink():
                raise OSError("gbrain projection directory must not be a symlink")
            directory.mkdir(exist_ok=True)
            if not directory.is_dir():
                raise OSError("gbrain projection directory must be a directory")
        return current / space_id

    def project(self, request: GbrainProjectionRequest) -> GbrainProjectionResult:
        if not _ID_RE.fullmatch(request.schema_pack):
            raise ValueError("gbrain schema pack identity is invalid")
        encoded = request.published_markdown.encode("utf-8")
        digest = hashlib.sha256(encoded).hexdigest()
        destination = self._ensure_projection_directory(request.space_id) / f"{request.asset_id}-{digest[:16]}.md"
        if destination.is_symlink():
            raise OSError("gbrain projection destination must not be a symlink")
        if destination.exists():
            if destination.read_bytes() != encoded:
                raise ValueError("gbrain projection content-addressed destination mismatch")
            result = GbrainProjectionResult(
                projection_uri=f"knowledge://spaces/{request.space_id}/gbrain/{request.asset_id}-{digest[:16]}",
                source_uri=request.source_uri,
                published_uri=request.published_uri,
                source_revision=request.source_revision,
                published_digest=request.published_digest,
                bytes=len(encoded),
                schema_pack=request.schema_pack,
            )
            return result
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise FileExistsError("gbrain projection temporary output already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        duplicate = False
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(temporary, destination)
            except FileExistsError:
                if destination.is_symlink() or not destination.is_file() or destination.read_bytes() != encoded:
                    raise ValueError("gbrain projection content-addressed destination mismatch")
                duplicate = True
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
        projection_uri = f"knowledge://spaces/{request.space_id}/gbrain/{request.asset_id}-{digest[:16]}"
        if duplicate:
            return GbrainProjectionResult(
                projection_uri=projection_uri,
                source_uri=request.source_uri,
                published_uri=request.published_uri,
                source_revision=request.source_revision,
                published_digest=request.published_digest,
                bytes=len(encoded),
                schema_pack=request.schema_pack,
            )
        self._append_manifest(
            {
                "status": "projected",
                "projection_uri": projection_uri,
                "source_uri": request.source_uri,
                "published_uri": request.published_uri,
                "source_revision": request.source_revision,
                "published_digest": request.published_digest,
                "projection_digest": "sha256:" + digest,
                "bytes": len(encoded),
                "schema_api": "gbrain-schema-pack-v1",
                "schema_pack": request.schema_pack,
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self.projection_count += 1
        return GbrainProjectionResult(
            projection_uri=projection_uri,
            source_uri=request.source_uri,
            published_uri=request.published_uri,
            source_revision=request.source_revision,
            published_digest=request.published_digest,
            bytes=len(encoded),
            schema_pack=request.schema_pack,
        )
