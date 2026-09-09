"""Generate a path-free index of current local migration evidence."""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from knowledge_platform.distribution.evidence_bundle import build_evidence_bundle

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-evidence-bundle.json"


def _source_revision(repo_root: Path) -> str:
    try:
        result = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("git source revision is unavailable") from error
    revision = result.stdout.strip()
    if not revision:
        raise RuntimeError("git source revision is empty")
    return revision


def run_shadow(
    *,
    repo_root: Path = _ROOT,
    output_path: Path = _DEFAULT_OUTPUT,
    source_revision: str | None = None,
) -> dict[str, object]:
    """Write the bundle for a checkout, with an explicit test revision escape hatch.

    Production callers keep the Git-derived revision.  Target-only tests may
    build a synthetic evidence checkout in a temporary directory without
    manufacturing a source repository or importing the legacy project.
    """
    payload = build_evidence_bundle(
        repo_root=repo_root,
        source_revision=_source_revision(repo_root) if source_revision is None else source_revision,
    )
    output_path = output_path.expanduser().resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result = dict(payload)
    result["report"] = str(output_path)
    return result


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
                "report_count": result["report_count"],
                "replay_consistent": result["replay_consistent"],
                "bundle_digest": result["bundle_digest"],
                "report": result["report"],
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
