"""Isolated local package build/test shadow for the Phase 10 release boundary."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

_FORMAT = "agent-knowledge-platform-phase10-package-build-shadow/v1"
_PASS_STATUS = "PHASE10_PACKAGE_BUILD_SHADOW_PASS_NOT_ACTIVATABLE"
_BLOCKED_STATUS = "PHASE10_PACKAGE_BUILD_SHADOW_BLOCKED"
_SAFE_RELATIVE_PATH = re.compile(r"^(?!/)(?!.*(?:^|/)\.\.(?:/|$))[A-Za-z0-9._*/-]+$")
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]{1,256}$")
_SAFE_PACKAGE_NAME = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)?$")
_SAFE_PACKAGE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,127}$")
_PACKAGE_ROOTS = (
    ("console", "packages/knowledge-platform-console"),
    ("console_contracts", "packages/knowledge-platform-console-contracts"),
    ("deploy_cli", "packages/knowledge-platform-deploy-cli"),
    ("skills", "packages/knowledge-platform-skills"),
)
_COMMANDS = (
    ("console", ("npm", "--offline", "test")),
    ("console", ("npm", "--offline", "run", "build")),
    ("console_contracts", ("npm", "--offline", "test")),
    ("deploy_cli", ("npm", "--offline", "test")),
    ("skills", ("npm", "--offline", "test")),
)
_PACK_COMMANDS = tuple(
    (package, ("npm", "--offline", "pack", "--dry-run", "--json"))
    for package, _relative in _PACKAGE_ROOTS
)
_IGNORED_NAMES = frozenset({"node_modules", "dist", "__pycache__"})
_IGNORED_PREFIXES = (".next-",)


class PackageBuildShadowError(ValueError):
    """Raised when the isolated package shadow cannot preserve its boundary."""


def _safe_relative(value: str) -> str:
    if not isinstance(value, str) or not _SAFE_RELATIVE_PATH.fullmatch(value):
        raise PackageBuildShadowError("package shadow path must be relative")
    return value


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _assert_tree_has_no_symlinks(root: Path) -> None:
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = tuple(current.iterdir())
        except OSError as error:
            raise PackageBuildShadowError("package source tree is unreadable") from error
        for entry in entries:
            if entry.name in _IGNORED_NAMES or any(entry.name.startswith(prefix) for prefix in _IGNORED_PREFIXES):
                continue
            try:
                metadata = entry.lstat()
            except OSError as error:
                raise PackageBuildShadowError("package source metadata is unavailable") from error
            if metadata.st_mode & 0o170000 == 0o120000:
                raise PackageBuildShadowError("package source tree contains a symlink")
            if entry.is_dir():
                pending.append(entry)
            elif not entry.is_file():
                raise PackageBuildShadowError("package source tree contains an unsupported file")


def _tree_digest(root: Path) -> str:
    records: list[tuple[str, str, int]] = []
    pending = [root]
    while pending:
        current = pending.pop()
        for entry in sorted(current.iterdir(), key=lambda item: item.name, reverse=True):
            if entry.name in _IGNORED_NAMES or any(entry.name.startswith(prefix) for prefix in _IGNORED_PREFIXES):
                continue
            relative = entry.relative_to(root).as_posix()
            _safe_relative(relative)
            if entry.is_dir():
                pending.append(entry)
                continue
            if not entry.is_file():
                raise PackageBuildShadowError("staged package tree contains an unsupported file")
            content = entry.read_bytes()
            records.append((relative, _sha256_bytes(content), len(content)))
    encoded = json.dumps(sorted(records), separators=(",", ":")).encode("utf-8")
    return _sha256_bytes(encoded)


@dataclass(frozen=True, slots=True)
class PackageArchiveObservation:
    """Stable package boundary emitted by ``npm pack --dry-run``."""

    package: str
    name: str
    version: str
    files: tuple[tuple[str, int], ...]
    bundled_dependency_count: int

    def __post_init__(self) -> None:
        expected_packages = {name for name, _relative in _PACKAGE_ROOTS}
        if self.package not in expected_packages:
            raise PackageBuildShadowError("package archive package is unknown")
        if not isinstance(self.name, str) or not _SAFE_PACKAGE_NAME.fullmatch(self.name):
            raise PackageBuildShadowError("package archive name is unsafe")
        if not isinstance(self.version, str) or not _SAFE_PACKAGE_VERSION.fullmatch(self.version):
            raise PackageBuildShadowError("package archive version is unsafe")
        paths = tuple(path for path, _size in self.files)
        if not self.files or paths != tuple(sorted(paths)) or len(set(paths)) != len(paths):
            raise PackageBuildShadowError("package archive file list is not stable")
        for path, size in self.files:
            if not isinstance(path, str) or not _SAFE_RELATIVE_PATH.fullmatch(path) or path.startswith("."):
                raise PackageBuildShadowError("package archive file path is unsafe")
            if "node_modules/" in path or path == "node_modules" or "file://" in path:
                raise PackageBuildShadowError("package archive contains a dependency tree")
            if not isinstance(size, int) or size < 0:
                raise PackageBuildShadowError("package archive file size is invalid")
        if not isinstance(self.bundled_dependency_count, int) or self.bundled_dependency_count != 0:
            raise PackageBuildShadowError("package archive contains bundled dependencies")

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "name": self.name,
            "version": self.version,
            "files": [{"path": path, "bytes": size} for path, size in self.files],
            "file_count": len(self.files),
            "bundled_dependency_count": self.bundled_dependency_count,
        }


@dataclass(frozen=True, slots=True)
class PackageCommandResult:
    package: str
    command: tuple[str, ...]
    returncode: int
    stdout_digest: str
    stderr_digest: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "command": list(self.command),
            "returncode": self.returncode,
            "stdout_digest": self.stdout_digest,
            "stderr_digest": self.stderr_digest,
        }


@dataclass(frozen=True, slots=True)
class PackageBuildShadow:
    source_repository: str
    source_revision: str
    package_roots: tuple[tuple[str, str], ...]
    commands: tuple[PackageCommandResult, ...]
    archive_observations: tuple[PackageArchiveObservation, ...]
    replay_consistent: bool
    staged_tree_digest: str
    test_commands_executed: bool = True
    network_contacted: bool = False
    independent_repository_verified: bool = False
    release_artifact_generated: bool = False

    def __post_init__(self) -> None:
        if self.source_repository != "PuddingClaw":
            raise PackageBuildShadowError("source repository is not explicit")
        if not _SAFE_REVISION.fullmatch(self.source_revision):
            raise PackageBuildShadowError("source revision is unsafe")
        if not self.package_roots or len({name for name, _path in self.package_roots}) != len(self.package_roots):
            raise PackageBuildShadowError("package roots are invalid")
        for name, path in self.package_roots:
            if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", name):
                raise PackageBuildShadowError("package name is invalid")
            _safe_relative(path)
        if not _SAFE_RELATIVE_PATH.fullmatch("packages/knowledge-platform-console"):
            raise PackageBuildShadowError("package root policy is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.staged_tree_digest):
            raise PackageBuildShadowError("staged tree digest is invalid")
        if tuple(item.package for item in self.archive_observations) != tuple(name for name, _path in _PACKAGE_ROOTS):
            raise PackageBuildShadowError("package archive observations are incomplete or reordered")
        if self.network_contacted or self.independent_repository_verified or self.release_artifact_generated:
            raise PackageBuildShadowError("package shadow cannot claim release evidence")

    @property
    def status(self) -> str:
        return _PASS_STATUS if self.replay_consistent and all(item.returncode == 0 for item in self.commands) else _BLOCKED_STATUS

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "activation_allowed": False,
            "release_execution_allowed": False,
            "source_repository": self.source_repository,
            "source_revision": self.source_revision,
            "package_roots": [{"name": name, "path": path} for name, path in self.package_roots],
            "commands": [item.to_dict() for item in self.commands],
            "command_count": len(self.commands),
            "all_commands_passed": all(item.returncode == 0 for item in self.commands),
            "archive_observations": [item.to_dict() for item in self.archive_observations],
            "archive_manifest_verified": True,
            "replay_consistent": self.replay_consistent,
            "staged_tree_digest": self.staged_tree_digest,
            "test_commands_executed": self.test_commands_executed,
            "network_contacted": self.network_contacted,
            "independent_repository_verified": self.independent_repository_verified,
            "release_artifact_generated": self.release_artifact_generated,
            "scope": "temporary staged package tree only; not an extracted repository, RC, SBOM, or release artifact",
        }


def _stage_packages(repo_root: Path, stage_root: Path) -> None:
    for _name, relative in _PACKAGE_ROOTS:
        _safe_relative(relative)
        source = repo_root / relative
        destination = stage_root / relative
        if not source.is_dir():
            raise PackageBuildShadowError("Platform package root is missing")
        _assert_tree_has_no_symlinks(source)
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(
            source,
            destination,
            symlinks=False,
            ignore=shutil.ignore_patterns(*_IGNORED_NAMES, *_IGNORED_PREFIXES),
        )


def _parse_package_archive(package: str, package_root: Path, stdout: bytes) -> PackageArchiveObservation:
    try:
        payload = json.loads(stdout.decode("utf-8"))
        if not isinstance(payload, list) or len(payload) != 1 or not isinstance(payload[0], dict):
            raise ValueError("unexpected npm pack result")
        record = payload[0]
        package_json = json.loads((package_root / "package.json").read_text(encoding="utf-8"))
        raw_files = record["files"]
        if not isinstance(raw_files, list):
            raise ValueError("npm pack files field is invalid")
        if any(not isinstance(item, dict) for item in raw_files):
            raise ValueError("npm pack file entry is invalid")
        files = tuple(sorted((item["path"], item["size"]) for item in raw_files))
        if record.get("name") != package_json.get("name") or record.get("version") != package_json.get("version"):
            raise ValueError("npm pack metadata differs from package manifest")
        bundled = record.get("bundled", [])
        if not isinstance(bundled, list):
            raise ValueError("npm pack bundled field is invalid")
        return PackageArchiveObservation(
            package=package,
            name=record["name"],
            version=record["version"],
            files=files,
            bundled_dependency_count=len(bundled),
        )
    except (OSError, UnicodeError, KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise PackageBuildShadowError("npm pack output is not a safe package manifest") from error


def _run_staged_commands(
    stage_root: Path,
) -> tuple[tuple[PackageCommandResult, ...], str, tuple[PackageArchiveObservation, ...]]:
    results: list[PackageCommandResult] = []
    archives: list[PackageArchiveObservation] = []
    environment = {
        **os.environ,
        "CI": "1",
        "npm_config_offline": "true",
        "npm_config_audit": "false",
        "npm_config_fund": "false",
        "npm_config_update_notifier": "false",
    }
    with TemporaryDirectory(prefix="knowledge-platform-npm-cache-") as npm_cache:
        environment["npm_config_cache"] = npm_cache
        for package, command in (*_COMMANDS, *_PACK_COMMANDS):
            cwd = stage_root / dict(_PACKAGE_ROOTS)[package]
            try:
                completed = subprocess.run(
                    command,
                    cwd=cwd,
                    env=environment,
                    capture_output=True,
                    timeout=120,
                    check=False,
                )
            except (OSError, subprocess.SubprocessError) as error:
                results.append(PackageCommandResult(package, command, 125, _sha256_bytes(b""), _sha256_bytes(str(type(error).__name__).encode())))
                continue
            results.append(
                PackageCommandResult(
                    package,
                    command,
                    completed.returncode,
                    _sha256_bytes(completed.stdout),
                    _sha256_bytes(completed.stderr),
                )
            )
            if command[2] == "pack" and completed.returncode == 0:
                archives.append(_parse_package_archive(package, cwd, completed.stdout))
    return tuple(results), _tree_digest(stage_root), tuple(archives)


def build_package_shadow(*, repo_root: Path, source_revision: str) -> PackageBuildShadow:
    repo_root = repo_root.expanduser().resolve()
    with TemporaryDirectory(prefix="knowledge-platform-package-shadow-") as first, TemporaryDirectory(
        prefix="knowledge-platform-package-shadow-replay-"
    ) as second:
        first_root = Path(first)
        second_root = Path(second)
        _stage_packages(repo_root, first_root)
        _stage_packages(repo_root, second_root)
        first_commands, first_digest, first_archives = _run_staged_commands(first_root)
        second_commands, second_digest, second_archives = _run_staged_commands(second_root)
        replay_consistent = (
            tuple((item.package, item.command, item.returncode) for item in first_commands)
            == tuple((item.package, item.command, item.returncode) for item in second_commands)
            and first_digest == second_digest
            and tuple(item.to_dict() for item in first_archives) == tuple(item.to_dict() for item in second_archives)
        )
        return PackageBuildShadow(
            source_repository="PuddingClaw",
            source_revision=source_revision,
            package_roots=_PACKAGE_ROOTS,
            commands=first_commands,
            archive_observations=first_archives,
            replay_consistent=replay_consistent,
            staged_tree_digest=first_digest,
        )


__all__ = [
    "PackageArchiveObservation",
    "PackageBuildShadow",
    "PackageBuildShadowError",
    "PackageCommandResult",
    "build_package_shadow",
]
