"""Generate a non-releaseable static Platform dependency-closure shadow."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.distribution.dependency_closure import (
    build_dependency_closure_shadow,
    stable_digest,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-platform-dependency-closure-shadow.json"


def run_shadow(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, object]:
    result = build_dependency_closure_shadow(repo_root=repo_root)
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
    result = run_shadow(repo_root=args.repo_root, output_path=args.output)
    print(
        json.dumps(
            {
                "status": result["status"],
                "scanned_file_count": result["scanned_file_count"],
                "import_edge_count": result["import_edge_count"],
                "finding_count": result["finding_count"],
                "replay_consistent": result["replay_consistent"],
                "report": result["report"],
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] == "PHASE10_PLATFORM_DEPENDENCY_CLOSURE_SHADOW_PASS_NOT_ACTIVATABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
