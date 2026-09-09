"""Local source provider for Connector Sync rehearsal."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from pathlib import Path

from knowledge_platform.wiki.local import _MAX_SOURCE_BYTES, _open_source, _safe_absolute_path

from .ports import SourceItemSnapshot

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")


class LocalConnectorSourceProvider:
    """Read only caller-bound regular files; no connector URL or credential lookup."""

    def __init__(self, *, expected_digests: Mapping[str, str] | None = None) -> None:
        self._expected_digests = {str(item_id): str(digest) for item_id, digest in (expected_digests or {}).items()}

    def read(self, *, source_item_id: str, path: Path) -> SourceItemSnapshot:
        if not _ID_RE.fullmatch(source_item_id):
            raise ValueError("Connector source item identity is invalid")
        candidate = _safe_absolute_path(path)
        with _open_source(candidate, max_bytes=_MAX_SOURCE_BYTES) as (stream, size):
            digest = hashlib.sha256()
            read_bytes = 0
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
                read_bytes += len(chunk)
            if read_bytes != size:
                raise OSError("Connector source changed while being read")
        content_digest = "sha256:" + digest.hexdigest()
        expected_digest = self._expected_digests.get(source_item_id)
        if expected_digest is not None and content_digest != expected_digest:
            raise ValueError("Connector source digest does not match bound Catalog revision")
        return SourceItemSnapshot(
            source_item_id=source_item_id,
            content_digest=content_digest,
            bytes=read_bytes,
        )
