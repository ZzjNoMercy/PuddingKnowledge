"""Build a development-only source snapshot for a future history extraction.

This module records selected source file digests and the current dirty Git
state.  It deliberately does not commit, tag, invoke ``git-filter-repo`` or
copy source files.  The resulting manifest is evidence for a temporary
extraction clone, not a release manifest.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
import stat
import subprocess
from collections.abc import Iterable
from pathlib import Path
from typing import Any

_FORMAT = "agent-knowledge-platform-phase10-development-source-snapshot/v1"
_SECRET_FILE_NAMES = frozenset(
    {
        ".npmrc",
        "secret.json",
        "secrets.json",
        "credential.json",
        "credentials.json",
        "password.json",
        "passwords.json",
        "service-account.json",
    }
)
_CACHE_PARTS = frozenset(
    {
        ".git",
        ".next",
        ".next-dev-3001",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".parcel-cache",
        "__pycache__",
        "node_modules",
        ".venv",
        "venv",
        "dist",
        "build",
        "coverage",
        "out",
    }
)
_CACHE_SUFFIXES = (".pyc", ".pyo", ".tsbuildinfo", ".log", ".swp", ".tgz", ".whl", ".zip")
_SECRET_SUFFIXES = (".pem", ".key", ".p12", ".pfx")
_DATABASE_SUFFIXES = (".db", ".db-shm", ".db-wal", ".sqlite", ".sqlite3", ".sqlite-shm", ".sqlite-wal")

# These are pathspecs, not a claim that every file under them belongs in a
# final repository.  Generated/cache/secret/database files are filtered below.
DEFAULT_SELECTED_SOURCE_PATHS: tuple[str, ...] = (
    "README.md",
    "LICENSE",
    "docs/agent-native-knowledge-platform-spec.md",
    "docs/knowledge-platform/**",
    "docs/knowledge-platform-progress.md",
    "backend/pyproject.toml",
    "backend/uv.lock",
    "backend/knowledge_platform/**",
    "backend/knowledge_contracts/**",
    "backend/resources/gbrain-schema-packs/**",
    "backend/skills/github-monitor/scripts/store_kb.py",
    "backend/utils/database_leases.py",
    "backend/scripts/phase*.py",
    "backend/tests/test_knowledge_platform_*.py",
    "backend/tests/test_phase8_local_*.py",
    "backend/tests/conftest.py",
    "backend/tests/test_phase9_*.py",
    "backend/tests/test_phase10_*.py",
    "packages/knowledge-platform-console/**",
    "packages/knowledge-platform-console-contracts/**",
    "packages/knowledge-platform-deploy-cli/**",
    "packages/knowledge-platform-runtime/**",
    "packages/knowledge-platform-skills/**",
    "packages/knowledge-platform-web/**",
    "packages/puddingharness-extraction/**",
    "frontend/src/app/app-control/page.tsx",
    "frontend/src/lib/settingsApi.ts",
    "frontend/src/types/electron.d.ts",
    "frontend/src/components/citations/PortableEvidenceCard.tsx",
    "frontend/src/components/citations/portableEvidence.ts",
    "frontend/src/components/citations/PortableEvidenceCard.test.ts",
    "frontend/src/lib/resourceUrl.test.ts",
    "scripts/start-knowledge-local.sh",
)


class DevelopmentSourceSnapshotError(ValueError):
    """The development snapshot input is unsafe or cannot be read."""


def _validate_path(path: str, *, label: str) -> None:
    if (
        not isinstance(path, str)
        or not path
        or path.startswith("/")
        or "\\" in path
        or path.endswith("/")
        or any(part == ".." for part in path.split("/"))
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
    ):
        raise DevelopmentSourceSnapshotError(f"{label} contains an unsafe relative path")


def _sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_stat_token(path: Path) -> tuple[int, int, int, int]:
    info = path.stat()
    return (info.st_ino, info.st_size, info.st_mtime_ns, stat.S_IMODE(info.st_mode))


def _stable_file_digest(path: Path) -> tuple[str, tuple[int, int, int, int]]:
    before = _file_stat_token(path)
    digest = _sha256_file(path)
    after = _file_stat_token(path)
    if before != after:
        raise DevelopmentSourceSnapshotError(f"source file mutated during snapshot: {path.name}")
    return digest, after


def _git(repo_root: Path, *args: str, check: bool = True) -> bytes:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            check=False,
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DevelopmentSourceSnapshotError("Git source state is unavailable") from error
    if check and result.returncode != 0:
        raise DevelopmentSourceSnapshotError("Git source state is unavailable")
    return result.stdout


def _git_revision(repo_root: Path) -> str:
    revision = _git(repo_root, "rev-parse", "HEAD").decode("utf-8", "replace").strip()
    if not re.fullmatch(r"[0-9a-f]{40}", revision):
        raise DevelopmentSourceSnapshotError("Git HEAD is not a full revision")
    return revision


def _git_state_token(repo_root: Path) -> tuple[str, str, str, str]:
    """Return content-free Git state evidence used for mutation detection."""

    revision = _git_revision(repo_root)
    status = _git(repo_root, "status", "--porcelain", "-z", "--untracked-files=all")
    diff = _git(repo_root, "diff", "--raw", "-z", "HEAD")
    untracked = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    return revision, _sha256_bytes(status), _sha256_bytes(diff), _sha256_bytes(untracked)


def _tracked_paths(repo_root: Path) -> set[str]:
    output = _git(repo_root, "ls-files", "-z")
    paths = {value.decode("utf-8", "surrogateescape") for value in output.split(b"\0") if value}
    for path in paths:
        _validate_path(path, label="tracked path")
    return paths


def _dirty_tracked_paths(repo_root: Path) -> set[str]:
    output = _git(repo_root, "diff", "--name-only", "-z", "HEAD")
    paths = {value.decode("utf-8", "surrogateescape") for value in output.split(b"\0") if value}
    for path in paths:
        _validate_path(path, label="dirty tracked path")
    return paths


def _untracked_paths(repo_root: Path) -> set[str]:
    output = _git(repo_root, "ls-files", "--others", "--exclude-standard", "-z")
    paths = {value.decode("utf-8", "surrogateescape") for value in output.split(b"\0") if value}
    for path in paths:
        _validate_path(path, label="untracked path")
    return paths


def _has_symlink_component(repo_root: Path, relative_path: str) -> bool:
    current = repo_root
    for component in relative_path.split("/"):
        current /= component
        if current.is_symlink():
            return True
    return False


def _matches(path: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(path, pattern) for pattern in patterns)


def _exclusion_reason(path: str) -> str | None:
    parts = set(path.split("/"))
    lowered = path.lower()
    if parts & _CACHE_PARTS:
        return "cache-or-environment-directory"
    name = path.rsplit("/", 1)[-1].lower()
    if name == ".env" or name.startswith(".env.") or name in _SECRET_FILE_NAMES or name.endswith(_SECRET_SUFFIXES):
        return "secret-or-credential-path"
    if lowered.startswith("private/") or "/pytest-" in lowered:
        return "test-artifact-path"
    if name.endswith(_CACHE_SUFFIXES):
        return "cache-or-generated-file"
    if name.endswith(_DATABASE_SUFFIXES) or "/artifacts/" in f"/{lowered}/" or lowered.startswith("artifacts/"):
        return "artifact-or-database-file"
    if name in {"staging-evidence.json", "harness-frontend-artifact-manifest.json"}:
        return "generated-staging-evidence"
    if name in {".ds_store"}:
        return "cache-or-generated-file"
    return None


def _candidate_paths(repo_root: Path, patterns: tuple[str, ...], tracked: set[str]) -> tuple[str, ...]:
    candidates = {path for path in tracked if _matches(path, patterns)}
    for pattern in patterns:
        if pattern.endswith("/**"):
            root = repo_root / pattern[:-3]
            if root.exists() and not root.is_symlink():
                for item in root.rglob("*"):
                    if item.is_file() or item.is_symlink():
                        candidates.add(item.relative_to(repo_root).as_posix())
        else:
            for item in repo_root.glob(pattern):
                if item.is_file() or item.is_symlink():
                    candidates.add(item.relative_to(repo_root).as_posix())
    return tuple(sorted(candidates))


def _file_record(repo_root: Path, path: str, *, tracked: bool, dirty: bool, diff: bytes | None) -> dict[str, Any]:
    absolute = repo_root / path
    if absolute.is_symlink() or _has_symlink_component(repo_root, path):
        raise DevelopmentSourceSnapshotError(f"selected source contains a symlink: {path}")
    if not absolute.is_file():
        return {
            "path": path,
            "tracked": tracked,
            "status": "deleted",
            "mode": None,
            "bytes": None,
            "sha256": None,
            "dirty_diff_sha256": _sha256_bytes(diff or b"") if dirty else None,
            "dirty_diff_bytes": len(diff or b"") if dirty else 0,
        }
    digest, stat_token = _stable_file_digest(absolute)
    record: dict[str, Any] = {
        "path": path,
        "tracked": tracked,
        "status": "dirty" if dirty else "tracked" if tracked else "untracked",
        "mode": format(stat_token[3], "04o"),
        "bytes": stat_token[1],
        "sha256": digest,
    }
    if dirty:
        record["dirty_diff_sha256"] = _sha256_bytes(diff or b"")
        record["dirty_diff_bytes"] = len(diff or b"")
    return record


def build_development_source_snapshot(
    *,
    repo_root: Path,
    selected_paths: Iterable[str] = DEFAULT_SELECTED_SOURCE_PATHS,
) -> dict[str, Any]:
    """Return a path-free, development-only source snapshot manifest."""

    requested_root = repo_root.expanduser()
    if requested_root.is_symlink():
        raise DevelopmentSourceSnapshotError("repository root must not be a symlink")
    repo_root = requested_root.resolve()
    if not repo_root.is_dir():
        raise DevelopmentSourceSnapshotError("repository root is not a directory")
    patterns = tuple(selected_paths)
    if not patterns:
        raise DevelopmentSourceSnapshotError("selected source paths must not be empty")
    for pattern in patterns:
        _validate_path(pattern, label="selected source path")

    initial_git_state = _git_state_token(repo_root)
    revision = initial_git_state[0]
    tracked_paths = _tracked_paths(repo_root)
    dirty_tracked_paths = _dirty_tracked_paths(repo_root)
    untracked_paths = _untracked_paths(repo_root)
    candidates = _candidate_paths(repo_root, patterns, tracked_paths)
    candidate_set = set(candidates)
    selected: list[dict[str, Any]] = []
    dirty_tracked: list[dict[str, Any]] = []
    untracked: list[dict[str, Any]] = []
    excluded: list[dict[str, str]] = []
    for path in candidates:
        reason = _exclusion_reason(path)
        if reason:
            excluded.append({"path": path, "reason": reason})
            continue
        tracked = path in tracked_paths
        absolute = repo_root / path
        if absolute.is_symlink() or _has_symlink_component(repo_root, path):
            excluded.append({"path": path, "reason": "symlink"})
            continue
        diff = _git(repo_root, "diff", "--binary", "HEAD", "--", path, check=True) if tracked else b""
        dirty = tracked and bool(diff)
        record = _file_record(repo_root, path, tracked=tracked, dirty=dirty, diff=diff if dirty else None)
        selected.append(record)
        if dirty:
            dirty_tracked.append(
                {
                    "path": path,
                    "selected": True,
                    "mode": record["mode"],
                    "dirty_diff_sha256": record["dirty_diff_sha256"],
                    "dirty_diff_bytes": record["dirty_diff_bytes"],
                }
            )
        elif not tracked:
            untracked.append(
                {
                    "path": path,
                    "selected": True,
                    "mode": record["mode"],
                    "bytes": record["bytes"],
                    "sha256": record["sha256"],
                }
            )

    # Record every dirty tracked/untracked path in the checkout, not only the
    # selected extraction candidates. This makes the dirty snapshot useful for
    # deciding what must be committed to a temporary source clone, while the
    # selected_files section remains the explicit extraction input set.
    for path in sorted(dirty_tracked_paths - candidate_set):
        reason = _exclusion_reason(path)
        if reason:
            excluded.append({"path": path, "reason": reason})
            continue
        absolute = repo_root / path
        if absolute.is_symlink() or _has_symlink_component(repo_root, path):
            excluded.append({"path": path, "reason": "symlink"})
            continue
        diff = _git(repo_root, "diff", "--binary", "HEAD", "--", path, check=True)
        record = _file_record(repo_root, path, tracked=True, dirty=True, diff=diff)
        dirty_tracked.append(
            {
                "path": path,
                "selected": False,
                "mode": record["mode"],
                "dirty_diff_sha256": record["dirty_diff_sha256"],
                "dirty_diff_bytes": record["dirty_diff_bytes"],
            }
        )

    for path in sorted(untracked_paths - candidate_set):
        reason = _exclusion_reason(path)
        if reason:
            excluded.append({"path": path, "reason": reason})
            continue
        absolute = repo_root / path
        if absolute.is_symlink() or _has_symlink_component(repo_root, path):
            excluded.append({"path": path, "reason": "symlink"})
            continue
        record = _file_record(repo_root, path, tracked=False, dirty=False, diff=None)
        untracked.append(
            {
                "path": path,
                "selected": False,
                "mode": record["mode"],
                "bytes": record["bytes"],
                "sha256": record["sha256"],
            }
        )

    missing_anchors = [pattern for pattern in patterns if not any(_matches(path, patterns=(pattern,)) for path in candidates)]
    selected.sort(key=lambda item: item["path"])
    dirty_tracked.sort(key=lambda item: item["path"])
    untracked.sort(key=lambda item: item["path"])
    excluded.sort(key=lambda item: (item["path"], item["reason"]))
    if _git_state_token(repo_root) != initial_git_state:
        raise DevelopmentSourceSnapshotError("source Git state mutated during snapshot")

    # A Git status token cannot see an untracked file's content changing while
    # we scan. Re-read every included file digest before publishing the result.
    for record in [*selected, *untracked]:
        path = record["path"]
        if record["sha256"] is None:
            continue
        absolute = repo_root / path
        if not absolute.is_file() or absolute.is_symlink() or _has_symlink_component(repo_root, path):
            raise DevelopmentSourceSnapshotError("source file set mutated during snapshot")
        digest, stat_token = _stable_file_digest(absolute)
        if (digest, stat_token[1], format(stat_token[3], "04o")) != (
            record["sha256"],
            record["bytes"],
            record["mode"],
        ):
            raise DevelopmentSourceSnapshotError("source file digest changed during snapshot")
    excluded_counts: dict[str, int] = {}
    excluded_samples: list[dict[str, str]] = []
    samples_by_reason: dict[str, int] = {}
    for item in excluded:
        reason = item["reason"]
        excluded_counts[reason] = excluded_counts.get(reason, 0) + 1
        if samples_by_reason.get(reason, 0) < 12:
            excluded_samples.append(item)
            samples_by_reason[reason] = samples_by_reason.get(reason, 0) + 1
    payload: dict[str, Any] = {
        "format": _FORMAT,
        "status": "DEVELOPMENT_SOURCE_SNAPSHOT_NOT_A_RELEASE_SOURCE",
        "source_repository": "PuddingClaw",
        "source_revision": revision,
        "source_worktree_clean": not bool(_git(repo_root, "status", "--porcelain", "--untracked-files=all")),
        "selected_source_paths": list(patterns),
        "selected_files": selected,
        "dirty_tracked_diff": dirty_tracked,
        "untracked_files": untracked,
        "excluded_paths": excluded_samples,
        "excluded_counts": dict(sorted(excluded_counts.items())),
        "missing_selected_paths": sorted(missing_anchors),
        "execution": {"git_commit": False, "git_tag": False, "git_filter_repo": False, "files_copied": False},
    }
    payload["snapshot_digest"] = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return payload


__all__ = ["DEFAULT_SELECTED_SOURCE_PATHS", "DevelopmentSourceSnapshotError", "build_development_source_snapshot"]
