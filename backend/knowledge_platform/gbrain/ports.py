"""Framework-neutral contracts for the optional gbrain projection."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from typing import Protocol

from knowledge_contracts import is_valid_knowledge_uri

_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _identity(uri: str, kind: str) -> tuple[str, str]:
    parts = uri.removeprefix("knowledge://").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != kind or not all(parts):
        raise ValueError(f"gbrain {kind} URI is invalid")
    if not all(_ID_RE.fullmatch(part) for part in (parts[1], parts[3])):
        raise ValueError(f"gbrain {kind} URI contains an invalid identity")
    return parts[1], parts[3]


@dataclass(frozen=True, slots=True)
class GbrainProjectionRequest:
    space_id: str
    asset_id: str
    source_uri: str
    published_uri: str
    source_revision: str
    published_digest: str
    published_markdown: str
    idempotency_key: str
    schema_pack: str = "puddingclaw-wiki"

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.space_id) or not _ID_RE.fullmatch(self.asset_id):
            raise ValueError("gbrain projection identity is invalid")
        source_space, source_asset = _identity(self.source_uri, "assets")
        published_space, published_asset = _identity(self.published_uri, "wiki")
        if (source_space, source_asset) != (self.space_id, self.asset_id) or (published_space, published_asset) != (self.space_id, self.asset_id):
            raise ValueError("gbrain projection URI identity does not match request")
        if not is_valid_knowledge_uri(self.source_uri) or not is_valid_knowledge_uri(self.published_uri):
            raise ValueError("gbrain projection URI is invalid")
        if not self.source_revision.strip() or not self.idempotency_key.strip() or not self.schema_pack.strip():
            raise ValueError("gbrain projection metadata must not be empty")
        if not _DIGEST_RE.fullmatch(self.published_digest):
            raise ValueError("gbrain projection digest is invalid")
        if not self.published_markdown.strip():
            raise ValueError("gbrain projection markdown must not be empty")
        actual = "sha256:" + hashlib.sha256(self.published_markdown.encode("utf-8")).hexdigest()
        if actual != self.published_digest:
            raise ValueError("gbrain projection content digest does not match markdown")


@dataclass(frozen=True, slots=True)
class GbrainProjectionResult:
    projection_uri: str
    source_uri: str
    published_uri: str
    source_revision: str
    published_digest: str
    bytes: int
    schema_pack: str

    def __post_init__(self) -> None:
        if not is_valid_knowledge_uri(self.projection_uri):
            raise ValueError("gbrain projection result URI is invalid")
        if self.bytes <= 0 or not _DIGEST_RE.fullmatch(self.published_digest):
            raise ValueError("gbrain projection result is invalid")


class GbrainProjectionService(Protocol):
    def project(self, request: GbrainProjectionRequest) -> GbrainProjectionResult:
        """Build a deterministic rebuildable projection without requiring gbrain."""
