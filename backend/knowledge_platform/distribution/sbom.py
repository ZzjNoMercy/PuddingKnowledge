"""Deterministic, non-releaseable source inventory for Phase 10.

This is deliberately smaller than a generated SBOM.  It inventories the
Platform-owned source and distribution assets in the current checkout without
resolving or downloading dependencies.  The result is useful as a replayable
preflight input, but it must never be presented as an independent release
SBOM or as proof that a target repository can build on its own.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

_FORMAT = "agent-knowledge-platform-phase10-source-inventory/v1"
_STATUS = "PHASE10_SOURCE_INVENTORY_PREFLIGHT_NOT_RELEASEABLE"
_SAFE_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._*/-]+$")
_DEFAULT_ROOTS = (
    ("knowledge_contracts", "backend/knowledge_contracts"),
    ("knowledge_platform", "backend/knowledge_platform"),
    ("console", "packages/knowledge-platform-console"),
    ("console_contracts", "packages/knowledge-platform-console-contracts"),
    ("deploy_cli", "packages/knowledge-platform-deploy-cli"),
    ("skills", "packages/knowledge-platform-skills"),
)
_IGNORED_DIRECTORY_NAMES = frozenset({"__pycache__", "node_modules"})
_IGNORED_DIRECTORY_PREFIXES = (".next-",)
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]{1,256}$")


class SourceInventoryError(ValueError):
    """Raised when the source inventory cannot safely identify its inputs."""


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_RELATIVE_PATH.fullmatch(value):
        raise SourceInventoryError("inventory path must be relative")
    return value


def _digest_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                size += len(chunk)
                digest.update(chunk)
    except (OSError, ValueError) as error:
        raise SourceInventoryError("inventory file is unreadable") from error
    return f"sha256:{digest.hexdigest()}", size


@dataclass(frozen=True, slots=True)
class InventoryFile:
    path: str
    digest: str
    bytes: int

    def __post_init__(self) -> None:
        _relative_path(self.path)
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.digest):
            raise SourceInventoryError("inventory file digest is invalid")
        if not isinstance(self.bytes, int) or self.bytes < 0:
            raise SourceInventoryError("inventory file size is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {"path": self.path, "digest": self.digest, "bytes": self.bytes}


@dataclass(frozen=True, slots=True)
class SourceInventory:
    source_repository: str
    source_revision: str
    roots: tuple[tuple[str, str], ...]
    files: tuple[InventoryFile, ...]
    dependency_resolution: str = "not_attempted"
    network_contacted: bool = False
    independent_repository_verified: bool = False
    artifact_generated: bool = False
    sbom_generated: bool = False

    def __post_init__(self) -> None:
        if self.source_repository != "PuddingClaw":
            raise SourceInventoryError("source repository is not explicit")
        if not isinstance(self.source_revision, str) or not _SAFE_REVISION.fullmatch(self.source_revision):
            raise SourceInventoryError("source revision is unsafe")
        if self.dependency_resolution != "not_attempted":
            raise SourceInventoryError("source inventory cannot claim dependency resolution")
        if self.network_contacted or self.independent_repository_verified or self.artifact_generated or self.sbom_generated:
            raise SourceInventoryError("source inventory cannot claim release evidence")
        if not self.roots or len({name for name, _path in self.roots}) != len(self.roots):
            raise SourceInventoryError("inventory roots are invalid")
        for name, path in self.roots:
            if not name or not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise SourceInventoryError("inventory root name is invalid")
            _relative_path(path)
        if tuple(item.path for item in self.files) != tuple(sorted(item.path for item in self.files)):
            raise SourceInventoryError("inventory files must be sorted")
        if len({item.path for item in self.files}) != len(self.files):
            raise SourceInventoryError("inventory files must be unique")

    @property
    def inventory_digest(self) -> str:
        payload = {
            "roots": list(self.roots),
            "files": [item.to_dict() for item in self.files],
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        return f"sha256:{hashlib.sha256(encoded).hexdigest()}"

    @property
    def status(self) -> str:
        return _STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "execution_allowed": False,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "roots": [{"name": name, "path": path} for name, path in self.roots],
            "files": [item.to_dict() for item in self.files],
            "file_count": len(self.files),
            "bytes_total": sum(item.bytes for item in self.files),
            "inventory_digest": self.inventory_digest,
            "dependency_resolution": self.dependency_resolution,
            "network_contacted": self.network_contacted,
            "independent_repository_verified": self.independent_repository_verified,
            "artifact_generated": self.artifact_generated,
            "sbom_generated": self.sbom_generated,
            "scope": "same-checkout source inventory only; dependency resolution and release proof are pending",
        }


def _walk_files(root: Path, relative_root: str) -> tuple[InventoryFile, ...]:
    result: list[InventoryFile] = []
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name, reverse=True)
        except OSError as error:
            raise SourceInventoryError("inventory directory is unreadable") from error
        for entry in entries:
            if entry.name in _IGNORED_DIRECTORY_NAMES:
                continue
            if entry.is_dir() and any(entry.name.startswith(prefix) for prefix in _IGNORED_DIRECTORY_PREFIXES):
                continue
            relative = f"{relative_root}/{entry.relative_to(root).as_posix()}" if entry != root else relative_root
            _relative_path(relative)
            try:
                metadata = entry.lstat()
            except OSError as error:
                raise SourceInventoryError("inventory entry metadata is unavailable") from error
            if metadata.st_mode & 0o170000 == 0o120000:
                raise SourceInventoryError("inventory does not permit symlink assets")
            if entry.is_dir():
                pending.append(entry)
            elif entry.is_file():
                digest, size = _digest_file(entry)
                result.append(InventoryFile(relative, digest, size))
            else:
                raise SourceInventoryError("inventory contains an unsupported file type")
    return tuple(sorted(result, key=lambda item: item.path))


def build_source_inventory(
    *,
    repo_root: Path,
    source_revision: str,
    roots: tuple[tuple[str, str], ...] = _DEFAULT_ROOTS,
) -> SourceInventory:
    repo_root = repo_root.expanduser().resolve()
    files: list[InventoryFile] = []
    for _name, relative_root in roots:
        _relative_path(relative_root)
        root = repo_root / relative_root
        try:
            metadata = root.lstat()
        except OSError as error:
            raise SourceInventoryError("inventory root is unavailable") from error
        if metadata.st_mode & 0o170000 == 0o120000 or not root.is_dir():
            raise SourceInventoryError("inventory root must be a regular directory")
        files.extend(_walk_files(root, relative_root))
    return SourceInventory(
        source_repository="PuddingClaw",
        source_revision=source_revision,
        roots=tuple(roots),
        files=tuple(sorted(files, key=lambda item: item.path)),
    )


__all__ = ["InventoryFile", "SourceInventory", "SourceInventoryError", "build_source_inventory"]
