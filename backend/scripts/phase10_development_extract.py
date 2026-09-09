"""Seed a development extraction clone without claiming a release.

The default mode only validates a development source snapshot and prints a
plan. ``--apply`` is deliberately explicit: it creates a new local clone,
copies the selected snapshot files, writes a root-file mapping, and makes one
clean commit in that clone. It never commits to the source checkout, signs a
tag, or runs ``git-filter-repo``.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_SNAPSHOT = _ROOT / "artifacts/repository-split/phase10-development-source-snapshot-round1.json"
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]{1,120}$")
_FORMAT = "agent-knowledge-platform-phase10-development-extraction-plan/v1"

ROOT_FILE_MAPPINGS: dict[str, dict[str, dict[str, str | None]]] = {
    "puddingknowledge": {
        "LICENSE": {"source": "LICENSE", "action": "copy-existing-history"},
        "README.md": {
            "source": "packages/knowledge-platform-runtime/README.md",
            "action": "review-and-author-target-root",
        },
        "pyproject.toml": {
            "source": "packages/knowledge-platform-runtime/pyproject.toml",
            "action": "reconcile-and-relocate",
        },
        "uv.lock": {"source": "packages/knowledge-platform-runtime/uv.lock", "action": "reconcile-and-relocate"},
        "package.json": {"source": None, "action": "author-target-root"},
        "CI/release/SBOM": {"source": None, "action": "author-target-root"},
    },
    "puddingharness": {
        "LICENSE": {"source": "LICENSE", "action": "copy-existing-history"},
        "README.md": {"source": "packages/puddingharness-extraction/README.md", "action": "review-and-author-target-root"},
        "pyproject.toml": {"source": "packages/puddingharness-extraction/pyproject.toml", "action": "reconcile-and-relocate"},
        "uv.lock": {"source": "packages/puddingharness-extraction/uv.lock", "action": "reconcile-and-relocate"},
        "package.json": {"source": None, "action": "author-target-root"},
        "CI/release/SBOM": {"source": None, "action": "author-target-root"},
    },
}


class DevelopmentExtractionError(ValueError):
    """The development extraction input or destination is unsafe."""


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git(repo_root: Path, *args: str, check: bool = True) -> str:
    try:
        result = subprocess.run(
            ("git", *args),
            cwd=repo_root,
            check=False,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise DevelopmentExtractionError("Git operation failed") from error
    if check and result.returncode != 0:
        raise DevelopmentExtractionError("Git operation failed")
    return result.stdout.strip()


def _safe_relative(root: Path, relative: str) -> Path:
    if (
        not isinstance(relative, str)
        or not relative
        or relative.startswith("/")
        or "\\" in relative
        or any(ord(character) < 32 or ord(character) == 127 for character in relative)
    ):
        raise DevelopmentExtractionError("snapshot path is not relative")
    if any(part in {"", ".", "..", ".git"} for part in relative.split("/")):
        raise DevelopmentExtractionError("snapshot path contains an unsafe component")
    path = root / relative
    if path.is_symlink():
        raise DevelopmentExtractionError(f"snapshot path is a symlink: {relative}")
    current = root
    for component in relative.split("/"):
        current /= component
        if current.is_symlink():
            raise DevelopmentExtractionError(f"snapshot path has a symlink parent: {relative}")
    return path


def _reject_symlink_ancestors(path: Path) -> Path:
    """Return a lexical absolute path after rejecting every symlink component."""

    candidate = Path(os.path.abspath(str(path)))
    current = Path(candidate.anchor)
    for component in candidate.parts[1:]:
        current /= component
        if current.is_symlink():
            raise DevelopmentExtractionError(f"development output has a symlink ancestor: {current.name}")
    return candidate


def _clone_git_dir(clone_root: Path) -> Path:
    git_entry = clone_root / ".git"
    if not git_entry.is_dir() or git_entry.is_symlink():
        raise DevelopmentExtractionError("existing clone must have a private .git directory")
    common = Path(_git(clone_root, "rev-parse", "--git-common-dir"))
    if not common.is_absolute():
        common = clone_root / common
    common = common.resolve()
    own_git = git_entry.resolve()
    if common != own_git:
        raise DevelopmentExtractionError("existing clone shares a git common-dir with another checkout")
    alternates = own_git / "objects/info/alternates"
    if alternates.exists() and alternates.read_text(encoding="utf-8").strip():
        raise DevelopmentExtractionError("existing clone uses an object alternates file")
    return own_git


def _load_snapshot(snapshot_path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise DevelopmentExtractionError("development source snapshot is unreadable") from error
    if payload.get("format") != "agent-knowledge-platform-phase10-development-source-snapshot/v1":
        raise DevelopmentExtractionError("unsupported development source snapshot")
    if payload.get("status") != "DEVELOPMENT_SOURCE_SNAPSHOT_NOT_A_RELEASE_SOURCE":
        raise DevelopmentExtractionError("snapshot is not development-only")
    if payload.get("execution") != {"git_commit": False, "git_tag": False, "git_filter_repo": False, "files_copied": False}:
        raise DevelopmentExtractionError("snapshot execution claims are unsafe")
    selected = payload.get("selected_files")
    if not isinstance(selected, list) or not selected:
        raise DevelopmentExtractionError("snapshot selected files are missing")
    return payload


def _verify_selected_source(source_root: Path, snapshot: dict[str, Any]) -> list[str]:
    current_revision = _git(source_root, "rev-parse", "HEAD")
    if current_revision != snapshot.get("source_revision"):
        raise DevelopmentExtractionError("source HEAD changed since the snapshot")
    paths: list[str] = []
    for record in snapshot["selected_files"]:
        relative = record.get("path")
        path = _safe_relative(source_root, relative)
        expected_digest = record.get("sha256")
        if expected_digest is None:
            if path.exists():
                raise DevelopmentExtractionError(f"deleted snapshot file reappeared: {relative}")
            paths.append(relative)
            continue
        if not path.is_file():
            raise DevelopmentExtractionError(f"snapshot file is missing: {relative}")
        mode = format(path.stat().st_mode & 0o777, "04o")
        if mode != record.get("mode") or path.stat().st_size != record.get("bytes"):
            raise DevelopmentExtractionError(f"snapshot metadata changed: {relative}")
        if _sha256_file(path) != expected_digest:
            raise DevelopmentExtractionError(f"snapshot digest changed: {relative}")
        paths.append(relative)
    return sorted(paths)


def build_development_extraction_plan(*, source_root: Path, snapshot_path: Path, target: str) -> dict[str, Any]:
    if target not in ROOT_FILE_MAPPINGS:
        raise DevelopmentExtractionError("unsupported development extraction target")
    requested_source_root = source_root.expanduser()
    if requested_source_root.is_symlink():
        raise DevelopmentExtractionError("source root must not be a symlink")
    source_root = requested_source_root.resolve()
    if not source_root.is_dir():
        raise DevelopmentExtractionError("source root must be a real directory")
    snapshot = _load_snapshot(snapshot_path.expanduser().resolve())
    selected = _verify_selected_source(source_root, snapshot)
    mapping = ROOT_FILE_MAPPINGS[target]
    return {
        "format": _FORMAT,
        "status": "DEVELOPMENT_EXTRACTION_PLAN_ONLY",
        "target_repository": target,
        "source_repository": "PuddingClaw",
        "source_revision": snapshot["source_revision"],
        "selected_file_count": len(selected),
        "selected_source_files": selected,
        "root_file_mapping": mapping,
        "operations": [
            "clone source history into a new temporary directory",
            "copy selected snapshot files into the clone",
            "write root-file mapping for target-root reconciliation",
            "make one clean development commit in the clone only",
        ],
        "release_boundary": {
            "signed_tag_created": False,
            "git_filter_repo_run": False,
            "production_migration_allowed": False,
            "source_checkout_modified": False,
        },
    }


def apply_development_extraction(
    *,
    source_root: Path,
    snapshot_path: Path,
    output: Path,
    target: str,
    branch: str,
    existing_clone: bool = False,
) -> dict[str, Any]:
    if not _BRANCH_RE.fullmatch(branch):
        raise DevelopmentExtractionError("development branch name is unsafe")
    plan = build_development_extraction_plan(source_root=source_root, snapshot_path=snapshot_path, target=target)
    requested_source_root = source_root.expanduser()
    if requested_source_root.is_symlink():
        raise DevelopmentExtractionError("source root must not be a symlink")
    source_root = requested_source_root.resolve()
    output = _reject_symlink_ancestors(output.expanduser())
    if output.exists() and not existing_clone:
        raise DevelopmentExtractionError("development extraction output must not already exist")
    if existing_clone and (not output.is_dir() or output.is_symlink()):
        raise DevelopmentExtractionError("existing development clone must be a real directory")
    if output == source_root or source_root in output.parents:
        raise DevelopmentExtractionError("development output must not be inside source checkout")
    snapshot = _load_snapshot(snapshot_path.expanduser().resolve())
    if existing_clone:
        clone_git = _clone_git_dir(output)
        source_git = Path(_git(source_root, "rev-parse", "--git-common-dir"))
        if not source_git.is_absolute():
            source_git = source_root / source_git
        if clone_git == source_git.resolve():
            raise DevelopmentExtractionError("existing clone git directory is the source repository")
        # A --no-checkout clone has no HEAD yet. Checkout is also the explicit
        # revision gate for an already prepared temporary source clone.
        _git(output, "checkout", "-b", branch, snapshot["source_revision"])
    else:
        output.parent.mkdir(parents=True, exist_ok=True)
        _git(source_root.parent, "clone", "--no-local", "--no-hardlinks", str(source_root), str(output))
        _git(output, "switch", "-c", branch, snapshot["source_revision"])
    if _git(output, "rev-parse", "HEAD") != snapshot["source_revision"]:
        raise DevelopmentExtractionError("temporary clone HEAD does not match source snapshot")
    if _git(output, "status", "--porcelain"):
        raise DevelopmentExtractionError("temporary clone must be clean before selected files are applied")
    selected = _verify_selected_source(source_root, snapshot)
    for relative in selected:
        source = _safe_relative(source_root, relative)
        destination = _safe_relative(output, relative)
        destination.parent.mkdir(parents=True, exist_ok=True)
        if source.exists():
            shutil.copy2(source, destination)
        elif destination.exists():
            if destination.is_symlink() or not destination.is_file():
                raise DevelopmentExtractionError(f"cannot propagate selected deletion: {relative}")
            destination.unlink()
        else:
            raise DevelopmentExtractionError(f"selected deletion is absent from clone: {relative}")

    # Re-read source and destination before staging to close the source TOCTOU
    # window. A deleted selected file is propagated explicitly above.
    _verify_selected_source(source_root, snapshot)
    for record in snapshot["selected_files"]:
        relative = record["path"]
        destination = _safe_relative(output, relative)
        if record["sha256"] is None:
            if destination.exists():
                raise DevelopmentExtractionError(f"selected deletion was not propagated: {relative}")
            continue
        if not destination.is_file() or destination.is_symlink():
            raise DevelopmentExtractionError(f"selected destination is missing: {relative}")
        if _sha256_file(destination) != record["sha256"] or destination.stat().st_size != record["bytes"]:
            raise DevelopmentExtractionError(f"selected destination digest changed: {relative}")
        if format(destination.stat().st_mode & 0o777, "04o") != record["mode"]:
            raise DevelopmentExtractionError(f"selected destination mode changed: {relative}")

    mapping_path = output / "DEVELOPMENT_EXTRACTION_ROOT_MAPPING.json"
    mapping_path.write_text(json.dumps(plan["root_file_mapping"], ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    # Explicit reviewed paths can be ignored by the historical checkout's
    # rules (for example newly introduced migration documentation).
    _git(output, "add", "-f", "-A", "--", *selected, mapping_path.name)
    _verify_selected_source(source_root, snapshot)
    for record in snapshot["selected_files"]:
        if record["sha256"] is None:
            continue
        destination = _safe_relative(output, record["path"])
        if _sha256_file(destination) != record["sha256"]:
            raise DevelopmentExtractionError(f"selected destination changed after staging: {record['path']}")
    if _git(output, "diff", "--name-only"):
        raise DevelopmentExtractionError("temporary clone has unstaged changes before commit")
    _git(
        output,
        "-c",
        "user.name=PuddingClaw Development Extraction",
        "-c",
        "user.email=development-extraction@localhost",
        "commit",
        "-m",
        f"chore: seed {target} development extraction",
    )
    if _git(output, "status", "--porcelain"):
        raise DevelopmentExtractionError("development extraction clone is dirty after commit")
    plan["status"] = "DEVELOPMENT_EXTRACTION_COMMITTED_IN_TEMP_CLONE"
    plan["commit"] = _git(output, "rev-parse", "HEAD")
    return plan


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-root", type=Path, default=_ROOT)
    parser.add_argument("--snapshot", type=Path, default=_DEFAULT_SNAPSHOT)
    parser.add_argument("--target", choices=tuple(ROOT_FILE_MAPPINGS), required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--branch", default="codex/phase10-development-extraction")
    parser.add_argument(
        "--existing-clone",
        action="store_true",
        help="Use an existing clean clone (including --no-checkout) at --output instead of cloning it.",
    )
    parser.add_argument("--apply", action="store_true", help="clone and commit in the new output directory")
    args = parser.parse_args()
    if args.apply:
        result = apply_development_extraction(
            source_root=args.source_root,
            snapshot_path=args.snapshot,
            output=args.output,
            target=args.target,
            branch=args.branch,
            existing_clone=args.existing_clone,
        )
    else:
        result = build_development_extraction_plan(
            source_root=args.source_root,
            snapshot_path=args.snapshot,
            target=args.target,
        )
        result["requested_output"] = str(args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
