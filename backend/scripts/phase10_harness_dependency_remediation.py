"""Build a path-free remediation checklist from the Harness dependency scan."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from knowledge_platform.distribution import build_phase10_extraction_manifest
from knowledge_platform.distribution.harness_dependency_remediation import (
    build_harness_dependency_remediation_plan,
)
from knowledge_platform.distribution.harness_dependency_scan import scan_harness_dependency_shadow

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-harness-dependency-remediation.json"


def run_remediation_plan(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, Any]:
    manifest = build_phase10_extraction_manifest(repo_root=repo_root)
    scan = scan_harness_dependency_shadow(repo_root=repo_root, mixed_file_plans=manifest.mixed_file_plans)
    result = build_harness_dependency_remediation_plan(scan).to_dict()
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(output_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_remediation_plan(repo_root=args.repo_root, output_path=args.output)
    print(json.dumps({"status": result["status"], "finding_count": result["finding_count"], "files": len(result["files"]), "report": result["report"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
