"""Run the evidence-only Phase 9 mixed-file marker probes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from jsonschema import Draft202012Validator

from knowledge_platform.distribution.mixed_surface import run_mixed_surface_probes

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase9-local-mixed-surface-shadow-report.json"
_SCHEMA = _ROOT / "docs/knowledge-platform/mixed-surface-shadow.schema.json"


def run_shadow(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, object]:
    result = run_mixed_surface_probes(repo_root=repo_root)
    schema = json.loads(_SCHEMA.read_text(encoding="utf-8"))
    errors = sorted(Draft202012Validator(schema).iter_errors(result), key=lambda error: list(error.path))
    if errors:
        raise ValueError(f"mixed surface shadow report violates schema: {errors[0].message}")
    output = output_path.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {**result, "report": str(output)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_shadow(repo_root=args.repo_root, output_path=args.output)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if result["status"] == "PHASE9_MIXED_SURFACE_MARKER_PASS_NOT_ACTIVATABLE" else 1


if __name__ == "__main__":
    raise SystemExit(main())
