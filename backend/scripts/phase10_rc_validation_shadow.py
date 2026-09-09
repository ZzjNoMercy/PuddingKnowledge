"""Generate the non-releaseable Phase 10 RC validation matrix."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from knowledge_platform.distribution import build_phase10_extraction_manifest, build_rc_validation_manifest
from knowledge_platform.distribution.dependency_closure import build_dependency_closure_shadow
from knowledge_platform.distribution.dependency_lock import build_dependency_lock_preflight
from knowledge_platform.distribution.harness_dependency_scan import scan_harness_dependency_shadow
from scripts.phase10_dependency_sbom_shadow import run_shadow as run_dependency_sbom_shadow
from scripts.phase10_extraction_preflight import run_preflight
from scripts.phase10_local_installation_migration_shadow import run_shadow as run_installation_shadow
from scripts.phase10_package_build_shadow import run_shadow as run_package_build_shadow
from scripts.phase10_source_inventory_shadow import run_shadow as run_source_inventory_shadow

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-rc-validation-shadow.json"
_INSTALLATION_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-local-installation-migration-shadow.json"


def run_shadow(
    *,
    repo_root: Path = _ROOT,
    output_path: Path = _DEFAULT_OUTPUT,
    migration_manifest: Path | None = None,
    stage_report: Path = _INSTALLATION_OUTPUT.parent / "local-catalog-stage-report.json",
) -> dict[str, Any]:
    extraction = run_preflight(
        repo_root=repo_root,
        output_path=repo_root / "artifacts/phase0b-local-catalog/phase10-extraction-preflight.json",
    )["manifest"]
    installation_result = run_installation_shadow(
        stage_report=stage_report,
        output_path=_INSTALLATION_OUTPUT,
        migration_manifest=migration_manifest,
    )
    installation = dict(installation_result)
    installation.pop("report", None)
    source_inventory = run_source_inventory_shadow(
        repo_root=repo_root,
        output_path=repo_root / "artifacts/phase0b-local-catalog/phase10-source-inventory-shadow.json",
    )
    package_build = run_package_build_shadow(
        repo_root=repo_root,
        output_path=repo_root / "artifacts/phase0b-local-catalog/phase10-package-build-shadow.json",
    )
    dependency_sbom = run_dependency_sbom_shadow(
        repo_root=repo_root,
        output_path=repo_root / "artifacts/phase0b-local-catalog/phase10-dependency-sbom-shadow.json",
    )
    dependency_scan = scan_harness_dependency_shadow(
        repo_root=repo_root,
        mixed_file_plans=build_phase10_extraction_manifest(repo_root=repo_root).mixed_file_plans,
    ).to_dict()
    dependency_closure = build_dependency_closure_shadow(repo_root=repo_root).to_dict()
    dependency_lock = build_dependency_lock_preflight(repo_root=repo_root).to_dict()
    matrix = build_rc_validation_manifest(
        extraction_manifest=extraction,
        installation_shadow=installation,
        dependency_scan=dependency_scan,
    )
    result = matrix.to_dict()
    result["source_observations"] = {
        "extraction_preflight": "not-executable-local-shadow",
        "installation_migration": (
            "real-local-prepared-manifest-state-machine-shadow"
            if migration_manifest is not None
            else "synthetic-state-machine-shadow"
        ),
        "harness_dependency_scan": dependency_scan["status"],
        "source_inventory": source_inventory["status"],
        "package_build": package_build["status"],
        "dependency_sbom": dependency_sbom["status"],
        "platform_dependency_closure": dependency_closure["status"],
        "dependency_lock": dependency_lock["status"],
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--migration-manifest", type=Path)
    args = parser.parse_args()
    result = run_shadow(
        repo_root=args.repo_root,
        output_path=args.output,
        migration_manifest=args.migration_manifest,
    )
    print(json.dumps({"status": result["status"], "releaseable": result["releaseable"], "report": result["report"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
