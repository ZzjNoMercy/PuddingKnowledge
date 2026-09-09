"""Emit a path-free, read-only preflight for the Golden fixture freeze."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_platform.baseline import fixture_manifest_digest, load_fixture_manifest


def _safe_absolute(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().absolute()
    cursor = candidate
    while True:
        if cursor.is_symlink():
            raise ValueError(f"{label} contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _git_status(repo_root: Path) -> tuple[int, str]:
    result = subprocess.run(
        ["git", "status", "--porcelain", "--untracked-files=all"],
        cwd=repo_root,
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        raise ValueError("Git worktree status is unavailable")
    status_bytes = result.stdout.encode("utf-8")
    return len(result.stdout.splitlines()), f"sha256:{hashlib.sha256(status_bytes).hexdigest()}"


def _source_snapshot(repo_root: Path, manifest: Mapping[str, Any]) -> dict[str, Any]:
    raw_source = manifest.get("source_snapshot")
    if not isinstance(raw_source, Mapping) or not isinstance(raw_source.get("path"), str):
        raise ValueError("Golden source snapshot declaration is invalid")
    relative = Path(raw_source["path"])
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("Golden source snapshot path is invalid")
    path = _safe_absolute(repo_root / relative, label="source snapshot")
    declared_digest = raw_source.get("sha256")
    if declared_digest == "pending":
        return {
            "captured": False,
            "declared_sha256": "pending",
            "actual_sha256": "unavailable",
            "repository_revision": "unavailable",
            "worktree_clean": False,
            "raw_contents_included": False,
            "observation_only": True,
        }
    actual_digest = _sha256_file(path)
    if declared_digest != actual_digest.removeprefix("sha256:"):
        raise ValueError("Golden source snapshot digest does not match manifest")
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Golden source snapshot is invalid") from exc
    if not isinstance(document, Mapping):
        raise ValueError("Golden source snapshot must be an object")
    revision = document.get("repository_revision")
    if not isinstance(revision, str) or len(revision) != 40 or any(character not in "0123456789abcdef" for character in revision):
        raise ValueError("Golden source snapshot repository revision is invalid")
    if document.get("raw_contents_included") is not False or document.get("observation_only") is not True:
        raise ValueError("Golden source snapshot safety flags are invalid")
    return {
        "captured": True,
        "declared_sha256": f"sha256:{declared_digest}",
        "actual_sha256": actual_digest,
        "repository_revision": revision,
        "worktree_clean": document.get("worktree_clean") is True,
        "raw_contents_included": False,
        "observation_only": True,
    }


def build_preflight(*, repo_root: Path, manifest_path: Path) -> dict[str, Any]:
    repo_root = _safe_absolute(repo_root, label="repository")
    manifest_path = _safe_absolute(manifest_path, label="manifest")
    manifest, validation = load_fixture_manifest(manifest_path, repo_root=repo_root)
    source = _source_snapshot(repo_root, manifest)
    dirty_count, status_digest = _git_status(repo_root)
    blockers: list[str] = []
    if manifest["status"] != "frozen":
        blockers.append("manifest_status_not_frozen")
    if validation.incomplete_capabilities:
        blockers.append("incomplete_capabilities")
    if source["captured"] is not True:
        blockers.append("source_snapshot_not_captured")
    elif source["worktree_clean"] is not True:
        blockers.append("source_snapshot_worktree_dirty")
    if dirty_count != 0:
        blockers.append("current_worktree_dirty")
    report = {
        "format": "agent-native-knowledge-platform-golden-freeze-preflight/v1",
        "status": "PHASE0A_GOLDEN_FREEZE_PREFLIGHT_READY_NOT_FROZEN" if not blockers else "PHASE0A_GOLDEN_FREEZE_PREFLIGHT_BLOCKED",
        "activation_allowed": False,
        "execution_allowed": False,
        "manifest": {
            "sha256": fixture_manifest_digest(manifest),
            "status": str(manifest["status"]),
            "frozen": validation.frozen,
            "capability_count": len(validation.capability_ids),
            "incomplete_capability_count": len(validation.incomplete_capabilities),
        },
        "source_snapshot": source,
        "worktree": {"dirty_entry_count": dirty_count, "status_sha256": status_digest},
        "summary": {
            "blocker_count": len(blockers),
            "blockers": blockers,
            "freeze_allowed": not blockers,
        },
        "policy": "Read-only preflight; it never changes the manifest, source snapshot, Git worktree, readiness gate, or runtime behavior.",
    }
    if "/Users/" in json.dumps(report) or "/private/" in json.dumps(report):
        raise ValueError("Golden freeze preflight emitted a non-portable path")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--manifest", type=Path, default=Path("docs/knowledge-platform/golden-fixture-manifest.yaml"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase0a/golden-freeze-preflight.json"))
    args = parser.parse_args()
    root = args.repo_root.resolve()
    try:
        report = build_preflight(
            repo_root=root,
            manifest_path=(root / args.manifest) if not args.manifest.is_absolute() else args.manifest,
        )
        output = _safe_absolute(
            (root / args.output) if not args.output.is_absolute() else args.output,
            label="output",
        )
        if output.exists() and output.is_symlink():
            raise ValueError("output contains a symlink")
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    except (OSError, TypeError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-golden-freeze-preflight/v1", "error": str(exc)}))
        return 1
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
