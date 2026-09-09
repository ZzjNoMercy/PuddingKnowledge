"""Validate the sanitized Golden fixture manifest and emit a compact report."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.baseline import fixture_manifest_digest, load_fixture_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="fixture manifest; defaults to docs/knowledge-platform/golden-fixture-manifest.yaml",
    )
    parser.add_argument("--require-frozen", action="store_true")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    manifest = (args.manifest or repo_root / "docs/knowledge-platform/golden-fixture-manifest.yaml").resolve()
    try:
        document, validation = load_fixture_manifest(
            manifest,
            repo_root=repo_root,
            require_frozen=args.require_frozen,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-golden-fixture-report/v1", "error": str(exc)}))
        return 1
    print(
        json.dumps(
            {
                "format": "agent-knowledge-platform-golden-fixture-report/v1",
                "manifest": str(manifest.relative_to(repo_root)),
                "manifest_sha256": fixture_manifest_digest(document),
                "status": validation.status,
                "frozen": validation.frozen,
                "capability_ids": list(validation.capability_ids),
                "incomplete_capabilities": list(validation.incomplete_capabilities),
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
