"""Explicit local adapters for Web Capture processing rehearsal."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from datetime import UTC, datetime
from pathlib import Path

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_platform.wiki.local import (
    _ID_RE,
    _safe_output_root,
)
from knowledge_platform.wiki.ports import RawSnapshot

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class LocalCapturePublishingService:
    """Publish captured Markdown to an isolated directory without overwrite."""

    def __init__(self, *, root: Path, space_id: str) -> None:
        if not _ID_RE.fullmatch(space_id):
            raise ValueError("Capture Space identity is invalid")
        self._root = root.expanduser().absolute()
        self._space_id = space_id
        self.publish_count = 0

    @property
    def _manifest_path(self) -> Path:
        return self._root / "capture-publication-manifest.jsonl"

    async def publish(self, *, snapshot: RawSnapshot, idempotency_key: str) -> str:
        del idempotency_key
        parts = snapshot.source_uri.removeprefix("knowledge://").split("/")
        if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "assets" or parts[1] != self._space_id:
            raise ValueError("Capture source Space or Asset is invalid")
        if parts[3] != snapshot.snapshot_id:
            raise ValueError("Capture source Asset does not match snapshot identity")
        if not _DIGEST_RE.fullmatch(snapshot.content_digest):
            raise ValueError("Capture source digest is invalid")
        root = _safe_output_root(self._root)
        root.mkdir(parents=True, exist_ok=True)
        destination = root / "captures" / f"{snapshot.snapshot_id}.md"
        destination.parent.mkdir(parents=True, exist_ok=True)
        if root.is_symlink() or destination.parent.is_symlink():
            raise OSError("Capture output directory must not be a symlink")
        if destination.exists() or destination.is_symlink():
            raise FileExistsError("Capture resource already exists")
        temporary = destination.with_name(f".{destination.name}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise FileExistsError("Capture temporary output already exists")
        content = snapshot.content.encode("utf-8")
        temporary.write_bytes(content)
        os.link(temporary, destination)
        temporary.unlink()
        resource_uri = f"knowledge://spaces/{self._space_id}/captures/{snapshot.snapshot_id}/content"
        record = {
            "status": "published",
            "resource_uri": resource_uri,
            "asset_id": snapshot.snapshot_id,
            "source_revision": snapshot.source_revision,
            "source_digest": snapshot.content_digest,
            "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
            "updated_at": datetime.now(UTC).isoformat(),
        }
        if self._manifest_path.is_symlink():
            raise OSError("Capture publication manifest must not be a symlink")
        lock_path = self._root / ".capture-publication-manifest.lock"
        if lock_path.is_symlink():
            raise OSError("Capture publication manifest lock must not be a symlink")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                existing = self._manifest_path.read_text(encoding="utf-8") if self._manifest_path.is_file() else ""
                self._manifest_path.write_text(
                    existing + json.dumps(record, sort_keys=True) + "\n",
                    encoding="utf-8",
                )
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        self.publish_count += 1
        return resource_uri

    def lint(self, resource_uri: str) -> dict[str, object]:
        expected = f"knowledge://spaces/{self._space_id}/captures/"
        if not is_valid_knowledge_uri(resource_uri) or not resource_uri.startswith(expected) or not resource_uri.endswith("/content"):
            raise ValueError("Capture resource URI is invalid")
        asset_id = resource_uri[len(expected) : -len("/content")]
        if not _ID_RE.fullmatch(asset_id):
            raise ValueError("Capture resource identity is invalid")
        if self._manifest_path.is_symlink():
            raise OSError("Capture publication manifest must not be a symlink")
        destination = self._root / "captures" / f"{asset_id}.md"
        if not destination.is_file() or destination.is_symlink():
            return {"ok": False, "status": "missing"}
        content = destination.read_bytes()
        return {
            "ok": True,
            "status": "published",
            "content_digest": "sha256:" + hashlib.sha256(content).hexdigest(),
            "bytes": len(content),
        }
