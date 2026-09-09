"""Generate a development-only Phase 10 source snapshot manifest."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.distribution.development_source_snapshot import (
    DEFAULT_SELECTED_SOURCE_PATHS,
    build_development_source_snapshot,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/repository-split/phase10-development-source-snapshot-round1.json"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=_ROOT)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument(
        "--selected-path",
        action="append",
        dest="selected_paths",
        help="A relative source path/glob; repeat to replace the default selection.",
    )
    args = parser.parse_args()
    selected = tuple(args.selected_paths) if args.selected_paths else DEFAULT_SELECTED_SOURCE_PATHS
    payload = build_development_source_snapshot(repo_root=args.repo_root, selected_paths=selected)
    output = args.output.expanduser().resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({"status": payload["status"], "report": str(output), "snapshot_digest": payload["snapshot_digest"]}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
