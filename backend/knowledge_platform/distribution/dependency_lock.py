"""Read-only dependency-lock split preflight for the Phase 10 boundary.

The current checkout has one PuddingClaw Python project and one uv lock.  This
module checks that the existing declarations are represented in that lock,
then explicitly records the still-missing target lock boundary.  It never
regenerates a lock, downloads packages, or infers a dependency split from
package names alone.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import tomllib

_FORMAT = "agent-knowledge-platform-phase10-dependency-lock-preflight/v1"
_STATUS = "PHASE10_DEPENDENCY_LOCK_PREFLIGHT_BLOCKED"
_SAFE_NAME = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
_REQUIREMENT_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9_.-]*)")
_TARGET_LOCKFILES = ("puddingknowledge/uv.lock", "puddingharness/uv.lock")


class DependencyLockError(ValueError):
    """Raised when dependency-lock evidence cannot be read safely."""


def _normalize_name(value: str) -> str:
    name = value.strip().lower().replace("_", "-").replace(".", "-")
    if not _SAFE_NAME.fullmatch(name):
        raise DependencyLockError("dependency name is unsafe")
    return name


def _requirement_names(values: list[str]) -> tuple[str, ...]:
    names: set[str] = set()
    for value in values:
        if not isinstance(value, str):
            raise DependencyLockError("dependency declaration is not a string")
        match = _REQUIREMENT_NAME.match(value)
        if match is None:
            raise DependencyLockError("dependency declaration has no package name")
        names.add(_normalize_name(match.group(1)))
    return tuple(sorted(names))


def _read_toml(path: Path) -> dict[str, Any]:
    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, tomllib.TOMLDecodeError) as error:
        raise DependencyLockError("dependency metadata cannot be parsed") from error


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
    except OSError as error:
        raise DependencyLockError("dependency file cannot be hashed") from error
    return "sha256:" + digest.hexdigest()


def _declared_dependencies(metadata: dict[str, Any]) -> tuple[str, ...]:
    project = metadata.get("project")
    if not isinstance(project, dict) or not isinstance(project.get("dependencies"), list):
        raise DependencyLockError("project dependency declarations are missing")
    values = list(project["dependencies"])
    optional = project.get("optional-dependencies", {})
    if not isinstance(optional, dict):
        raise DependencyLockError("optional dependency declarations are invalid")
    for declarations in optional.values():
        if not isinstance(declarations, list):
            raise DependencyLockError("optional dependency group is invalid")
        values.extend(declarations)
    groups = metadata.get("dependency-groups", {})
    if not isinstance(groups, dict):
        raise DependencyLockError("dependency groups are invalid")
    for declarations in groups.values():
        if not isinstance(declarations, list):
            raise DependencyLockError("dependency group is invalid")
        values.extend(declarations)
    return _requirement_names(values)


def _locked_packages(lock_metadata: dict[str, Any]) -> tuple[str, ...]:
    packages = lock_metadata.get("package")
    if not isinstance(packages, list):
        raise DependencyLockError("uv lock package records are missing")
    names: set[str] = set()
    for package in packages:
        if not isinstance(package, dict) or not isinstance(package.get("name"), str):
            raise DependencyLockError("uv lock package record is invalid")
        names.add(_normalize_name(package["name"]))
    return tuple(sorted(names))


def _requirements_names(path: Path) -> tuple[str, ...]:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise DependencyLockError("requirements file cannot be read") from error
    values: list[str] = []
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", "-", "--")):
            continue
        values.append(stripped)
    return _requirement_names(values)


@dataclass(frozen=True, slots=True)
class DependencyLockPreflight:
    source_project: str
    source_project_version: str
    declared_dependencies: tuple[str, ...]
    locked_packages: tuple[str, ...]
    missing_declared_from_uv_lock: tuple[str, ...]
    requirements_packages: tuple[str, ...]
    missing_requirements_from_uv_lock: tuple[str, ...]
    source_lock_digest: str
    requirements_digest: str
    target_lockfiles: tuple[tuple[str, bool], ...]
    mixed_project_dependency_graph: bool
    replay_consistent: bool
    network_contacted: bool = False
    lock_regenerated: bool = False
    independent_repository_verified: bool = False

    def __post_init__(self) -> None:
        if self.source_project != "puddingclaw-backend":
            raise DependencyLockError("source project is not explicit")
        if not self.source_project_version or any(ord(character) < 32 for character in self.source_project_version):
            raise DependencyLockError("source project version is invalid")
        for values in (
            self.declared_dependencies,
            self.locked_packages,
            self.missing_declared_from_uv_lock,
            self.requirements_packages,
            self.missing_requirements_from_uv_lock,
        ):
            if tuple(values) != tuple(sorted(set(values))):
                raise DependencyLockError("dependency names must be sorted and unique")
        for digest in (self.source_lock_digest, self.requirements_digest):
            if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
                raise DependencyLockError("dependency file digest is invalid")
        if tuple(name for name, _present in self.target_lockfiles) != _TARGET_LOCKFILES:
            raise DependencyLockError("target lockfile inventory is incomplete")
        if self.network_contacted or self.lock_regenerated or self.independent_repository_verified:
            raise DependencyLockError("lock preflight cannot claim mutation or release evidence")

    @property
    def status(self) -> str:
        return _STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "execution_allowed": False,
            "source_project": self.source_project,
            "source_project_version": self.source_project_version,
            "declared_dependencies": list(self.declared_dependencies),
            "declared_dependency_count": len(self.declared_dependencies),
            "locked_packages": list(self.locked_packages),
            "locked_package_count": len(self.locked_packages),
            "missing_declared_from_uv_lock": list(self.missing_declared_from_uv_lock),
            "requirements_packages": list(self.requirements_packages),
            "requirements_package_count": len(self.requirements_packages),
            "missing_requirements_from_uv_lock": list(self.missing_requirements_from_uv_lock),
            "source_lock_digest": self.source_lock_digest,
            "requirements_digest": self.requirements_digest,
            "target_lockfiles": [{"path": path, "present": present} for path, present in self.target_lockfiles],
            "target_lockfiles_present": all(present for _path, present in self.target_lockfiles),
            "mixed_project_dependency_graph": self.mixed_project_dependency_graph,
            "replay_consistent": self.replay_consistent,
            "network_contacted": self.network_contacted,
            "lock_regenerated": self.lock_regenerated,
            "independent_repository_verified": self.independent_repository_verified,
            "scope": "same-checkout declaration/lock consistency only; target dependency split and independent lock generation are pending",
        }


def _build_once(repo_root: Path) -> DependencyLockPreflight:
    pyproject = repo_root / "backend/pyproject.toml"
    uv_lock = repo_root / "backend/uv.lock"
    requirements = repo_root / "backend/requirements.txt"
    metadata = _read_toml(pyproject)
    lock_metadata = _read_toml(uv_lock)
    project = metadata["project"]
    declared = _declared_dependencies(metadata)
    locked = _locked_packages(lock_metadata)
    requirements_names = _requirements_names(requirements)
    target_lockfiles = tuple((path, (repo_root / path).is_file()) for path in _TARGET_LOCKFILES)
    optional = project.get("optional-dependencies", {})
    mixed = isinstance(optional, dict) and "knowledge" in optional and "analytics" in optional
    return DependencyLockPreflight(
        source_project=str(project.get("name") or ""),
        source_project_version=str(project.get("version") or ""),
        declared_dependencies=declared,
        locked_packages=locked,
        missing_declared_from_uv_lock=tuple(sorted(set(declared) - set(locked))),
        requirements_packages=requirements_names,
        missing_requirements_from_uv_lock=tuple(sorted(set(requirements_names) - set(locked))),
        source_lock_digest=_sha256(uv_lock),
        requirements_digest=_sha256(requirements),
        target_lockfiles=target_lockfiles,
        mixed_project_dependency_graph=mixed,
        replay_consistent=True,
    )


def build_dependency_lock_preflight(*, repo_root: Path) -> DependencyLockPreflight:
    repo_root = repo_root.expanduser().resolve()
    first = _build_once(repo_root)
    second = _build_once(repo_root)
    if first.to_dict() != second.to_dict():
        return replace(first, replay_consistent=False)
    return first


def stable_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


__all__ = ["DependencyLockError", "DependencyLockPreflight", "build_dependency_lock_preflight", "stable_digest"]
