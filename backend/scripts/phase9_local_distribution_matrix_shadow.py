"""Validate the Phase 9 local Console and distribution surfaces as one shadow.

This is an acceptance matrix for the local migration boundary.  It executes
only offline package tests/builds in temporary staging trees and read-only
ownership/asset checks in the current worktree.  It never extracts a
repository, starts Docker, moves files, or changes activation state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator

from knowledge_platform.distribution import build_package_shadow, build_phase9_boundary_manifest
from knowledge_platform.distribution.mixed_surface import run_mixed_surface_probes

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase9-local-distribution-matrix-shadow-report.json"
_SCHEMA = _ROOT / "docs/knowledge-platform/phase9-local-distribution-matrix.schema.json"
_PASS_STATUS = "PHASE9_LOCAL_DISTRIBUTION_MATRIX_PASS_NOT_ACTIVATABLE"
_BLOCKED_STATUS = "PHASE9_LOCAL_DISTRIBUTION_MATRIX_BLOCKED"


def _sha256(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _assert_no_symlink_components(path: Path) -> Path:
    """Reject a report path that reaches an existing symlink component."""

    absolute = path.expanduser()
    if not absolute.is_absolute():
        absolute = Path.cwd() / absolute
    current = Path(absolute.anchor)
    for component in absolute.parts[1:]:
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            continue
        except OSError as error:
            raise ValueError("matrix output path is unavailable") from error
        if metadata.st_mode & 0o170000 == 0o120000:
            raise ValueError("matrix output path contains a symlink")
    return absolute


def _relative_files(repo_root: Path) -> set[str]:
    return {
        path.relative_to(repo_root).as_posix()
        for path in repo_root.rglob("*")
        if path.is_file() and ".git" not in path.parts and "node_modules" not in path.parts
    }


def _read_regular(repo_root: Path, relative: str) -> str:
    path = repo_root / relative
    try:
        metadata = path.lstat()
    except OSError as error:
        raise ValueError("distribution asset is unavailable") from error
    if not path.is_file() or metadata.st_mode & 0o170000 == 0o120000:
        raise ValueError("distribution asset is not a regular file")
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as error:
        raise ValueError("distribution asset is unreadable") from error


def _check_result(check_id: str, *, verified: bool, **details: Any) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "status": "verified" if verified else "blocked",
        **details,
    }


def _console_check(repo_root: Path) -> dict[str, Any]:
    from scripts.phase9_local_distribution_boundary_shadow import _read_console_surface_manifest

    observation = _read_console_surface_manifest(repo_root)
    return _check_result(
        "independent_console_surface",
        verified=observation["status"] == "verified" and observation["surface_ids"] == [
            "knowledge",
            "analytics",
            "oauth",
            "imports",
            "sources",
            "schema",
            "results",
            "notifications",
        ],
        surface_count=len(observation["surface_ids"]),
        file_count=observation["file_count"],
    )


def _frontend_renderer_check(repo_root: Path) -> dict[str, Any]:
    renderer = _read_regular(repo_root, "frontend/src/components/citations/PortableEvidenceCard.tsx")
    panel = _read_regular(repo_root, "frontend/src/components/citations/SourcesPanel.tsx")
    markers = (
        ("portable_renderer", "export default function PortableEvidenceCard" in renderer),
        ("platform_evidence_route", "PortableEvidenceCard" in panel),
        ("dom_injection_guard", "innerHTML" not in renderer and "innerHTML" not in panel),
    )
    return _check_result(
        "harness_evidence_renderer_boundary",
        verified=all(value for _name, value in markers),
        marker_count=sum(value for _name, value in markers),
        marker_total=len(markers),
    )


def _infrastructure_asset_check(repo_root: Path) -> dict[str, Any]:
    compose = _read_regular(repo_root, "packages/knowledge-platform-deploy-cli/assets/compose.platform.yml")
    supervisor = _read_regular(repo_root, "packages/knowledge-platform-deploy-cli/assets/platform-infra.sh")
    compose_markers = (
        "name: puddingknowledge" in compose,
        "puddingknowledge-api" in compose,
        "puddingknowledge-worker" in compose,
        "puddingknowledge-console" in compose,
        "PUDDINGKNOWLEDGE_HOME" in compose,
        "PuddingClaw" not in compose and "puddingclaw" not in compose,
    )
    supervisor_markers = (
        'PROJECT_NAME="puddingknowledge"' in supervisor,
        "PUDDINGKNOWLEDGE_HOME" in supervisor,
        "docker compose" in supervisor,
        "start-local-infra.sh" not in supervisor and "PuddingClaw" not in supervisor,
    )
    supervisor_path = repo_root / "packages/knowledge-platform-deploy-cli/assets/platform-infra.sh"
    syntax = subprocess.run(
        ("bash", "-n", str(supervisor_path)),
        cwd=repo_root,
        capture_output=True,
        timeout=10,
        check=False,
    )
    return _check_result(
        "platform_infrastructure_assets",
        verified=all(compose_markers) and all(supervisor_markers) and syntax.returncode == 0,
        compose_marker_count=sum(compose_markers),
        compose_marker_total=len(compose_markers),
        supervisor_marker_count=sum(supervisor_markers),
        supervisor_marker_total=len(supervisor_markers),
        shell_syntax_returncode=syntax.returncode,
    )


def _package_check(repo_root: Path) -> dict[str, Any]:
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo_root,
            capture_output=True,
            text=True,
            timeout=10,
            check=True,
        )
        source_revision = completed.stdout.strip()
        shadow = build_package_shadow(repo_root=repo_root, source_revision=source_revision)
    except (OSError, subprocess.SubprocessError, ValueError) as error:
        return _check_result(
            "offline_platform_package_replay",
            verified=False,
            error_type=type(error).__name__,
            command_count=0,
            archive_count=0,
            all_commands_passed=False,
            replay_consistent=False,
        )
    return _check_result(
        "offline_platform_package_replay",
        verified=(
            shadow.status == "PHASE10_PACKAGE_BUILD_SHADOW_PASS_NOT_ACTIVATABLE"
            and shadow.replay_consistent
            and all(item.returncode == 0 for item in shadow.commands)
            and len(shadow.archive_observations) == 4
        ),
        command_count=len(shadow.commands),
        archive_count=len(shadow.archive_observations),
        all_commands_passed=all(item.returncode == 0 for item in shadow.commands),
        replay_consistent=shadow.replay_consistent,
        staged_tree_digest=shadow.staged_tree_digest,
    )


def run_shadow(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, Any]:
    repo_root = repo_root.expanduser().resolve()
    output_path = _assert_no_symlink_components(output_path)
    checks: list[dict[str, Any]] = []
    try:
        boundary = build_phase9_boundary_manifest()
        missing = boundary.validate_paths(_relative_files(repo_root))
        scoped = boundary.audit_scoped_paths(_relative_files(repo_root), (
            "backend/knowledge_platform/**",
            "backend/knowledge_contracts/**",
            "packages/knowledge-platform-*/**",
            "frontend/src/app/knowledge/**",
            "frontend/src/app/analytics/**",
            "frontend/src/lib/api.ts",
            "frontend/src/components/citations/SourcesPanel.tsx",
            "electron/**",
            "docker-compose.infra.yml",
            "scripts/start-local-infra.sh",
            "scripts/start-knowledge-local.sh",
        ))
        checks.append(_check_result(
            "distribution_boundary_inventory",
            verified=not missing and not scoped["unclassified_paths"] and not scoped["ambiguous_paths"],
            missing_anchor_count=len(missing),
            unclassified_count=len(scoped["unclassified_paths"]),
            ambiguous_count=len(scoped["ambiguous_paths"]),
        ))
        checks.append(_console_check(repo_root))
        mixed = run_mixed_surface_probes(repo_root=repo_root)
        checks.append(_check_result(
            "mixed_file_symbol_ownership",
            verified=mixed["status"] == "PHASE9_MIXED_SURFACE_MARKER_PASS_NOT_ACTIVATABLE",
            probe_count=len(mixed["probes"]),
            verified_probe_count=sum(item["status"] == "verified" for item in mixed["probes"]),
            manual_review_required=True,
        ))
        checks.append(_frontend_renderer_check(repo_root))
        checks.append(_infrastructure_asset_check(repo_root))
        checks.append(_package_check(repo_root))
    except Exception as error:  # keep the report stable and fail closed
        checks.append(_check_result("matrix_runtime", verified=False, error_type=type(error).__name__))

    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-distribution-matrix-shadow/v1",
        "status": _PASS_STATUS if checks and all(item["status"] == "verified" for item in checks) else _BLOCKED_STATUS,
        "activation_allowed": False,
        "release_execution_allowed": False,
        "source_repository": "PuddingClaw",
        "check_count": len(checks),
        "pass_count": sum(item["status"] == "verified" for item in checks),
        "checks": checks,
        "source_paths_emitted": False,
        "network_contacted": False,
        "files_moved": False,
        "files_deleted": False,
        "independent_repositories_created": False,
        "semantic_equivalence_proven": False,
        "scope": "local worktree boundary and temporary offline package staging only; no extraction, release, or activation",
    }
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(report), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"distribution matrix report violates schema: {errors[0].message}")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**report, "report": str(output_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_shadow(repo_root=args.repo_root, output_path=args.output)
    print(json.dumps({
        "status": result["status"],
        "check_count": result["check_count"],
        "pass_count": result["pass_count"],
        "report": result["report"],
    }, ensure_ascii=False))
    return 0 if result["status"] == _PASS_STATUS else 1


if __name__ == "__main__":
    raise SystemExit(main())
