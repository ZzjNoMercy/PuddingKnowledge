"""Generate the non-releaseable Phase 10 dependency-lock split preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.distribution.dependency_lock import build_dependency_lock_preflight, stable_digest

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-dependency-lock-preflight.json"


def run_preflight(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, object]:
    result = build_dependency_lock_preflight(repo_root=repo_root)
    payload = result.to_dict()
    payload["report_digest"] = stable_digest(payload)
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    payload["report"] = str(output_path)
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_preflight(repo_root=args.repo_root, output_path=args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "declared_dependency_count": result["declared_dependency_count"],
                "locked_package_count": result["locked_package_count"],
                "missing_declared_from_uv_lock": len(result["missing_declared_from_uv_lock"]),
                "target_lockfiles_present": result["target_lockfiles_present"],
                "mixed_project_dependency_graph": result["mixed_project_dependency_graph"],
                "replay_consistent": result["replay_consistent"],
                "report": result["report"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
