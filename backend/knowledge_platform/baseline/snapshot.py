"""Capture content-addressed source/test inputs for Golden baselines.

The snapshot is intentionally not a behavior baseline. It records only
repository-relative file names, byte counts, and content digests for the
surfaces named by the Golden registry. Raw source, host paths, and fixture
contents never enter the artifact.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from dataclasses import dataclass
from pathlib import Path

import yaml


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


def _git_revision(repo_root: Path) -> str | None:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


def _git_worktree_clean(repo_root: Path) -> bool | None:
    try:
        result = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (OSError, subprocess.CalledProcessError):
        return None
    return not result.stdout.strip()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@dataclass(frozen=True, slots=True)
class SourceFileSnapshot:
    path: str
    bytes: int
    sha256: str


@dataclass(frozen=True, slots=True)
class CapabilitySourceSnapshot:
    capability_id: str
    source_files: tuple[SourceFileSnapshot, ...]
    test_files: tuple[SourceFileSnapshot, ...]

    @property
    def source_digest(self) -> str:
        return _digest_records(self.source_files)

    @property
    def test_manifest_digest(self) -> str:
        return _digest_records(self.test_files)

    def to_dict(self) -> dict[str, object]:
        return {
            "capability_id": self.capability_id,
            "source_files": [_file_to_dict(item) for item in self.source_files],
            "test_files": [_file_to_dict(item) for item in self.test_files],
            "source_digest": self.source_digest,
            "test_manifest_digest": self.test_manifest_digest,
        }


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    repository_revision: str | None
    worktree_clean: bool | None
    capabilities: tuple[CapabilitySourceSnapshot, ...]

    def to_dict(self) -> dict[str, object]:
        capability_payload = [capability.to_dict() for capability in self.capabilities]
        snapshot_digest = hashlib.sha256(
            json.dumps(capability_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest()
        return {
            "format": "agent-knowledge-platform-source-snapshot/v1",
            "observation_only": True,
            "raw_contents_included": False,
            "repository_revision": self.repository_revision,
            "worktree_clean": self.worktree_clean,
            "snapshot_digest": f"sha256:{snapshot_digest}",
            "capabilities": capability_payload,
        }


def _file_to_dict(item: SourceFileSnapshot) -> dict[str, object]:
    return {"path": item.path, "bytes": item.bytes, "sha256": item.sha256}


def _digest_records(records: tuple[SourceFileSnapshot, ...]) -> str:
    encoded = json.dumps(
        [_file_to_dict(item) for item in records],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _resolve_surface(repo_root: Path, raw_surface: object, *, capability_id: str, kind: str) -> tuple[Path, ...]:
    if not isinstance(raw_surface, str) or not raw_surface.strip():
        raise ValueError(f"{capability_id}.{kind} contains an invalid surface")
    relative = Path(raw_surface.strip())
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{capability_id}.{kind} contains an unsafe surface: {raw_surface!r}")
    root = repo_root.resolve()
    candidate = root / relative
    if candidate.is_symlink():
        raise ValueError(f"{capability_id}.{kind} surface is a symlink: {raw_surface!r}")
    target = candidate.resolve()
    if not target.is_relative_to(root) or not target.exists():
        raise ValueError(f"{capability_id}.{kind} surface does not exist inside repository: {raw_surface!r}")
    if target.is_file():
        return (target,)
    files: list[Path] = []
    for path in sorted(target.rglob("*")):
        if "__pycache__" in path.parts:
            continue
        if path.is_symlink():
            raise ValueError(f"{capability_id}.{kind} surface contains a symlink: {path}")
        if path.is_file():
            files.append(path)
    files = tuple(files)
    if not files:
        raise ValueError(f"{capability_id}.{kind} directory has no files: {raw_surface!r}")
    return files


def _snapshot_files(repo_root: Path, paths: set[Path]) -> tuple[SourceFileSnapshot, ...]:
    snapshots = []
    for path in sorted(paths):
        relative = str(path.relative_to(repo_root))
        snapshots.append(SourceFileSnapshot(relative, path.stat().st_size, _file_digest(path)))
    return tuple(snapshots)


def build_source_snapshot(repo_root: Path, registry_path: Path) -> SourceSnapshot:
    """Build a deterministic snapshot from every mapped Golden capability."""

    document = yaml.load(registry_path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    if not isinstance(document, dict) or document.get("format") != "agent-knowledge-platform-golden-baseline/v1":
        raise ValueError("invalid Golden baseline registry format")
    raw_capabilities = document.get("capabilities")
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise ValueError("Golden baseline registry must contain capabilities")

    capabilities: list[CapabilitySourceSnapshot] = []
    seen_ids: set[str] = set()
    for raw_capability in raw_capabilities:
        if not isinstance(raw_capability, dict):
            raise ValueError("Golden capability must be a mapping")
        capability_id = raw_capability.get("id")
        if not isinstance(capability_id, str) or not capability_id.strip() or capability_id in seen_ids:
            raise ValueError(f"invalid or duplicate capability id: {capability_id!r}")
        seen_ids.add(capability_id)
        source_paths: set[Path] = set()
        for surface in raw_capability.get("source_surfaces", []):
            source_paths.update(_resolve_surface(repo_root, surface, capability_id=capability_id, kind="source_surfaces"))
        test_paths: set[Path] = set()
        for surface in raw_capability.get("test_files", []):
            test_paths.update(_resolve_surface(repo_root, surface, capability_id=capability_id, kind="test_files"))
        if not source_paths or not test_paths:
            raise ValueError(f"{capability_id} must have non-empty source_surfaces and test_files")
        capabilities.append(
            CapabilitySourceSnapshot(
                capability_id,
                _snapshot_files(repo_root, source_paths),
                _snapshot_files(repo_root, test_paths),
            )
        )
    return SourceSnapshot(_git_revision(repo_root), _git_worktree_clean(repo_root), tuple(capabilities))
