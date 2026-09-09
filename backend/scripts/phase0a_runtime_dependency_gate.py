"""Run both Phase 0A runtime evidence registries as one readiness check."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    commands = (
        (
            "platform",
            repo_root / "backend/scripts/phase0a_runtime_probe_registry.py",
        ),
        (
            "legacy",
            repo_root / "backend/scripts/phase0a_legacy_runtime_probe_registry.py",
        ),
    )
    reports: dict[str, object] = {}
    failed = False
    for name, script in commands:
        command = [sys.executable, str(script), "--repo-root", str(repo_root), "--replay", "--require-complete"]
        if name == "legacy":
            command[-1] = "--require-reviewed"
        result = subprocess.run(
            command,
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
        )
        try:
            report: object = json.loads(result.stdout)
        except json.JSONDecodeError:
            report = {"stdout": result.stdout, "stderr": result.stderr}
        reports[name] = {"returncode": result.returncode, "report": report}
        failed = failed or result.returncode != 0
    print(
        json.dumps(
            {
                "format": "agent-knowledge-platform-runtime-dependency-gate/v1",
                "checks": reports,
                "status": "passed" if not failed else "blocked",
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
