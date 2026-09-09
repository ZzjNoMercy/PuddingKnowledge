"""Validate the Phase 0A runtime probe registry and its captured outputs."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from knowledge_platform.baseline import load_runtime_probe_registry, validate_runtime_probe_registry

_REPLAY_TIMEOUT_SECONDS = 30


def _replay_probe_outputs(document: dict[object, object], *, repo_root: Path) -> dict[str, object]:
    replayed: list[str] = []
    mismatched: list[str] = []
    skipped: list[str] = []
    for raw_probe in document.get("executed_probes", []):
        if not isinstance(raw_probe, dict):
            continue
        probe_id = str(raw_probe["id"])
        raw_output = raw_probe.get("output")
        if not isinstance(raw_output, dict):
            skipped.append(probe_id)
            continue
        command = [
            sys.executable,
            str(repo_root / "backend/scripts/phase0a_runtime_call_graph.py"),
            "--probe",
            str(raw_probe["reference"]),
        ]
        for prefix in raw_probe["module_prefixes"]:
            command.extend(("--module-prefix", str(prefix)))
        replay_env = os.environ.copy()
        current_pythonpath = replay_env.get("PYTHONPATH", "")
        pythonpath_entries = [str(repo_root / "backend")]
        if current_pythonpath:
            pythonpath_entries.append(current_pythonpath)
        replay_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        try:
            result = subprocess.run(
                command,
                cwd=repo_root,
                check=False,
                capture_output=True,
                env=replay_env,
                timeout=_REPLAY_TIMEOUT_SECONDS,
            )
            output_path = repo_root / str(raw_output["path"])
            output_matches = result.returncode == 0 and result.stdout == output_path.read_bytes()
        except (OSError, subprocess.TimeoutExpired):
            output_matches = False
        if not output_matches:
            mismatched.append(probe_id)
        else:
            replayed.append(probe_id)
    return {
        "status": "matched" if not mismatched else "mismatch",
        "replayed_probe_ids": replayed,
        "mismatched_probe_ids": mismatched,
        "skipped_probe_ids": skipped,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="runtime probe registry; defaults to docs/knowledge-platform/phase-0a-runtime-call-probes.yaml",
    )
    parser.add_argument("--require-complete", action="store_true")
    parser.add_argument(
        "--replay",
        action="store_true",
        help="re-run every probe with an output artifact and compare stdout byte-for-byte",
    )
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    registry = (args.registry or repo_root / "docs/knowledge-platform/phase-0a-runtime-call-probes.yaml").resolve()
    try:
        document, validation = load_runtime_probe_registry(
            registry,
            repo_root=repo_root,
        )
    except (OSError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-runtime-call-probe-report/v1", "error": str(exc)}))
        return 1
    report = {
        "format": "agent-knowledge-platform-runtime-call-probe-report/v1",
        "registry": str(registry.relative_to(repo_root)),
        "status": validation.status,
        "complete": validation.complete,
        "required_families": list(validation.required_families),
        "executed_probe_ids": list(validation.executed_probe_ids),
        "declared_status": document["status"],
    }
    if args.replay:
        report["replay"] = _replay_probe_outputs(document, repo_root=repo_root)
    if args.require_complete and not args.replay:
        # A strict gate always executes the probes; callers should not be able
        # to pass it by validating only the checked-in JSON/YAML.
        report["replay"] = _replay_probe_outputs(document, repo_root=repo_root)
    if args.require_complete:
        try:
            validation = validate_runtime_probe_registry(
                document,
                repo_root=repo_root,
                require_complete=True,
                replay_evidence=report.get("replay"),
            )
            report["status"] = validation.status
            report["complete"] = validation.complete
        except ValueError as exc:
            report["error"] = str(exc)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    replay_failed = (args.replay or args.require_complete) and report["replay"]["status"] != "matched"
    return 1 if replay_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
