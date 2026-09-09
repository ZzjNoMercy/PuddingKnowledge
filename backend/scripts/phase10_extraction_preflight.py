"""Generate a non-executable Phase 10 repository extraction preflight."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.distribution import build_phase10_extraction_manifest

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-extraction-preflight.json"


def run_preflight(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, object]:
    manifest = build_phase10_extraction_manifest(repo_root=repo_root)
    replay = build_phase10_extraction_manifest(repo_root=repo_root)
    replay_consistent = manifest.to_dict() == replay.to_dict()
    payload = manifest.to_dict()
    payload.update({"manifest_digest": manifest.canonical_digest(), "replay_consistent": replay_consistent})
    if not replay_consistent:
        payload["unresolved_gates"] = [*manifest.unresolved_gates, "extraction_manifest_replay_mismatch"]
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return {"status": manifest.status, "report": str(output_path), "manifest": payload}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    args = parser.parse_args()
    result = run_preflight(repo_root=args.repo_root, output_path=args.output)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
