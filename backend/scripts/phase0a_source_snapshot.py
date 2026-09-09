"""Emit the content-addressed source/test inputs named by the Golden registry."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.baseline import build_source_snapshot


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="Golden baseline registry; defaults to <repo-root>/docs/knowledge-platform/golden-baseline.yaml",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    registry = (args.registry or repo_root / "docs/knowledge-platform/golden-baseline.yaml").resolve()
    print(json.dumps(build_source_snapshot(repo_root, registry).to_dict(), ensure_ascii=False, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
