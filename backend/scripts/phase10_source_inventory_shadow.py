"""Generate a replayable but non-releaseable Platform source inventory."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path

from knowledge_platform.distribution import build_source_inventory

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase0b-local-catalog/phase10-source-inventory-shadow.json"


def _source_revision(repo_root: Path) -> str:
    try:
        completed = subprocess.run(
            ("git", "rev-parse", "HEAD"),
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("git source revision is unavailable") from error
    revision = completed.stdout.strip()
    if not revision:
        raise RuntimeError("git source revision is empty")
    return revision


def run_shadow(*, repo_root: Path = _ROOT, output_path: Path = _DEFAULT_OUTPUT) -> dict[str, object]:
    revision = _source_revision(repo_root)
    inventory = build_source_inventory(repo_root=repo_root, source_revision=revision)
    replay = build_source_inventory(repo_root=repo_root, source_revision=revision)
    payload = inventory.to_dict()
    payload["replay_consistent"] = inventory.to_dict() == replay.to_dict()
    payload["report_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
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
    print(json.dumps({"status": result["status"], "file_count": result["file_count"], "replay_consistent": result["replay_consistent"], "report": result["report"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
