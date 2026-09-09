"""Replay and validate the supplemental legacy runtime probe registry."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from knowledge_platform.baseline.legacy_runtime_registry import (
    load_legacy_runtime_probe_registry,
    validate_legacy_runtime_probe_registry,
)

_REPLAY_TIMEOUT_SECONDS = 60
def _replay_probe_outputs(document: dict[object, object], *, repo_root: Path) -> dict[str, object]:
    contract = document["probe_contract"]
    assert isinstance(contract, dict)
    replayed: list[str] = []
    mismatched: list[str] = []
    skipped: list[str] = []
    run_counts: dict[str, int] = {}
    for raw_probe in document["executed_probes"]:
        if not isinstance(raw_probe, dict):
            continue
        probe_id = str(raw_probe["id"])
        prefixes = [str(prefix) for prefix in raw_probe.get("module_scope", contract["module_scope"])]
        raw_output = raw_probe.get("output")
        if not isinstance(raw_output, dict):
            skipped.append(probe_id)
            continue
        replay_metadata = raw_probe.get("replay")
        if not isinstance(replay_metadata, dict) or not isinstance(replay_metadata.get("runs"), int):
            mismatched.append(probe_id)
            run_counts[probe_id] = 0
            continue
        declared_runs = replay_metadata["runs"]
        command = [
            sys.executable,
            str(repo_root / "backend/scripts/phase0a_runtime_call_graph.py"),
            "--probe",
            str(raw_probe["reference"]),
        ]
        for prefix in prefixes:
            command.extend(("--module-prefix", prefix))
        replay_env = os.environ.copy()
        for key in tuple(replay_env):
            if key.startswith("PUDDINGCLAW_"):
                # The observer owns its sanitized fixture roots. Do not let a
                # caller's deployment/test environment change the replay.
                replay_env.pop(key, None)
        current_pythonpath = replay_env.get("PYTHONPATH", "")
        pythonpath_entries = [str(repo_root / "backend")]
        if current_pythonpath:
            pythonpath_entries.append(current_pythonpath)
        replay_env["PYTHONPATH"] = os.pathsep.join(pythonpath_entries)
        output_path = repo_root / str(raw_output["path"])
        output_matches = True
        executed_runs = 0
        for _ in range(declared_runs):
            executed_runs += 1
            try:
                result = subprocess.run(
                    command,
                    cwd=repo_root,
                    check=False,
                    capture_output=True,
                    env=replay_env,
                    timeout=_REPLAY_TIMEOUT_SECONDS,
                )
                output_matches = output_matches and result.returncode == 0 and result.stdout == output_path.read_bytes()
            except (OSError, subprocess.TimeoutExpired):
                output_matches = False
        run_counts[probe_id] = executed_runs
        if output_matches:
            replayed.append(probe_id)
        else:
            mismatched.append(probe_id)
    return {
        "status": "matched" if not mismatched and not skipped else "mismatch",
        "replayed_probe_ids": replayed,
        "mismatched_probe_ids": mismatched,
        "skipped_probe_ids": skipped,
        "run_counts": run_counts,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument(
        "--registry",
        type=Path,
        default=None,
        help="legacy runtime probe registry; defaults to docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
    )
    parser.add_argument("--replay", action="store_true", help="re-run every legacy observer and compare stdout bytes")
    parser.add_argument("--require-reviewed", action="store_true", help="fail unless every boundary review is reviewed")
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    registry = (
        args.registry or repo_root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml"
    ).resolve()
    try:
        document, validation = load_legacy_runtime_probe_registry(registry, repo_root=repo_root)
    except (OSError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-legacy-runtime-probe-report/v1", "error": str(exc)}))
        return 1
    report: dict[str, object] = {
        "format": "agent-knowledge-platform-legacy-runtime-probe-report/v1",
        "registry": str(registry.relative_to(repo_root)),
        "status": validation.status,
        "reviewed": validation.reviewed,
        "pending_boundary_review_ids": list(validation.pending_boundary_review_ids),
        "required_capabilities": list(validation.required_capabilities),
        "executed_probe_ids": list(validation.executed_probe_ids),
    }
    if args.replay or args.require_reviewed:
        report["replay"] = _replay_probe_outputs(document, repo_root=repo_root)
    if args.require_reviewed:
        try:
            validation = validate_legacy_runtime_probe_registry(
                document,
                repo_root=repo_root,
                require_reviewed=True,
                replay_evidence=report.get("replay"),
            )
            report["reviewed"] = validation.reviewed
        except ValueError as exc:
            report["error"] = str(exc)
            print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
            return 1
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    replay_failed = (args.replay or args.require_reviewed) and report["replay"]["status"] != "matched"
    return 1 if replay_failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
