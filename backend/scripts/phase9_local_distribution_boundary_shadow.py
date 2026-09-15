"""Validate the Phase 9 distribution boundary without moving or deleting files."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from stat import S_ISLNK, S_ISREG
from typing import Any

from knowledge_platform.distribution import build_phase9_boundary_manifest

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_CONSOLE_SURFACE_IDS = (
    "knowledge",
    "analytics",
    "oauth",
    "imports",
    "sources",
    "schema",
    "results",
    "notifications",
)


def _relative_files(repo_root: Path) -> set[str]:
    return {
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts
    }


def _read_console_surface_manifest(repo_root: Path) -> dict[str, Any]:
    manifest_path = repo_root / "packages/knowledge-platform-console/dist/manifest.json"
    if not manifest_path.is_file() or manifest_path.is_symlink():
        raise ValueError("independent Console build manifest is missing")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("independent Console build manifest is unreadable") from error
    if not isinstance(manifest, dict):
        raise ValueError("independent Console build manifest is invalid")
    if manifest.get("format") != "agent-knowledge-platform-console-dist/v1":
        raise ValueError("independent Console build manifest format is unsupported")
    if manifest.get("status") != "built_not_deployed" or manifest.get("activation_allowed") is not False:
        raise ValueError("independent Console build manifest is activatable")
    surfaces = manifest.get("surfaces")
    if not isinstance(surfaces, list) or tuple(
        item.get("id") for item in surfaces if isinstance(item, dict)
    ) != _CONSOLE_SURFACE_IDS:
        raise ValueError("independent Console surface coverage is incomplete")
    for item in surfaces:
        if not isinstance(item, dict) or not isinstance(item.get("contract"), str):
            raise ValueError("independent Console surface contract is invalid")
        if item.get("activation_allowed") is not False:
            raise ValueError("independent Console surface is activatable")
    files = manifest.get("files")
    if not isinstance(files, dict) or set(files) != {
        "index.html",
        "app.mjs",
        "bitable.mjs",
        "local-boundary.mjs",
        "display-boundary.mjs",
        "contracts.mjs",
    }:
        raise ValueError("independent Console build file set is incomplete")
    for name, digest in files.items():
        if not isinstance(digest, str) or len(digest) != 71 or not digest.startswith("sha256:"):
            raise ValueError("independent Console build file digest is invalid")
        if any(character not in "0123456789abcdef" for character in digest[7:]):
            raise ValueError("independent Console build file digest is invalid")
        file_path = manifest_path.parent / name
        try:
            metadata = file_path.lstat()
        except OSError as error:
            raise ValueError("independent Console build file is missing") from error
        if not S_ISREG(metadata.st_mode) or S_ISLNK(metadata.st_mode):
            raise ValueError("independent Console build file is not a regular file")
        actual = "sha256:" + hashlib.sha256(file_path.read_bytes()).hexdigest()
        if actual != digest:
            raise ValueError("independent Console build file digest mismatch")
    try:
        actual_entries = {entry.name for entry in manifest_path.parent.iterdir() if entry.name != "manifest.json"}
    except OSError as error:
        raise ValueError("independent Console build directory is unreadable") from error
    if actual_entries != set(files):
        raise ValueError("independent Console build file set does not match manifest")
    return {
        "status": "verified",
        "surface_ids": list(_CONSOLE_SURFACE_IDS),
        "surface_contracts": [item["contract"] for item in surfaces],
        "file_count": len(files),
    }


def run_shadow(*, repo_root: Path = _ROOT, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-distribution-boundary-shadow/v1",
        "status": "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_BLOCKED",
        "activation_allowed": False,
        "source_paths_emitted": False,
        "files_moved": False,
        "files_deleted": False,
        "source_repository": "PuddingClaw",
    }
    try:
        manifest = build_phase9_boundary_manifest()
        existing_paths = _relative_files(repo_root)
        missing = manifest.validate_paths(existing_paths)
        scope_patterns = tuple(sorted({
            rule.path
            if "*" not in rule.path
            else rule.path.split("*", 1)[0].rstrip("/") + "/**"
            for rule in manifest.rules
        }))
        coverage = manifest.audit_scoped_paths(existing_paths, scope_patterns)
        console_surface = _read_console_surface_manifest(repo_root)
        result.update(
            {
                "status": (
                    "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_PASS_NOT_ACTIVATABLE"
                    if not missing
                    else "PHASE9_DISTRIBUTION_BOUNDARY_INVENTORY_BLOCKED_MISSING_ANCHOR"
                ),
                "manifest": manifest.to_dict(),
                "rule_count": len(manifest.rules),
                "platform_rule_count": sum(rule.owner == "platform" for rule in manifest.rules),
                "harness_rule_count": sum(rule.owner == "harness" for rule in manifest.rules),
                "manual_rule_count": sum(rule.owner == "shared" for rule in manifest.rules),
                "missing_anchors": list(missing),
                "coverage": {
                    "scoped_file_count": coverage["scoped_file_count"],
                    "unclassified_count": len(coverage["unclassified_paths"]),
                    "ambiguous_count": len(coverage["ambiguous_paths"]),
                    "unclassified_paths": list(coverage["unclassified_paths"]),
                    "ambiguous_paths": list(coverage["ambiguous_paths"]),
                },
                "console_surface": console_surface,
            }
        )
    except Exception as error:  # report a stable machine-readable failure
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase9-local-distribution-boundary-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(repo_root=args.repo_root, output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
