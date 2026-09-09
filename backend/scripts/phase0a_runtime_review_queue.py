"""Build a path-free, approval-only queue for platform runtime boundary review."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_platform.baseline import load_runtime_probe_registry
from knowledge_platform.baseline.runtime_review_queue import load_runtime_review_queue, review_id_for_item

_REPLAY_TIMEOUT_SECONDS = 60


def _safe_absolute(path: Path, *, label: str) -> Path:
    candidate = path.expanduser().absolute()
    cursor = candidate
    while True:
        if cursor.is_symlink():
            raise ValueError(f"{label} contains a symlink")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _replay(*, repo_root: Path, registry: Path) -> dict[str, Any]:
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(repo_root / "backend"), env.get("PYTHONPATH", "")]))
    result = subprocess.run(
        [
            sys.executable,
            str(repo_root / "backend/scripts/phase0a_runtime_probe_registry.py"),
            "--registry",
            str(registry),
            "--replay",
        ],
        cwd=repo_root,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        timeout=_REPLAY_TIMEOUT_SECONDS,
    )
    try:
        report = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise ValueError("platform runtime replay report is invalid") from exc
    replay = report.get("replay")
    if result.returncode != 0 or not isinstance(replay, dict) or replay.get("status") != "matched":
        raise ValueError("platform runtime replay did not match")
    if replay.get("mismatched_probe_ids") != [] or replay.get("skipped_probe_ids") != []:
        raise ValueError("platform runtime replay has mismatched or skipped probes")
    replayed_ids = replay.get("replayed_probe_ids")
    if not isinstance(replayed_ids, list) or any(not isinstance(value, str) for value in replayed_ids):
        raise ValueError("platform runtime replay probe IDs are invalid")
    return {
        "status": "matched",
        "replayed_probe_ids": sorted(replayed_ids),
        "replayed_probe_count": len(replayed_ids),
    }


def _validate_replay_evidence(replay: Mapping[str, Any], *, document: Mapping[str, Any]) -> dict[str, Any]:
    expected_ids = sorted(str(probe["id"]) for probe in document["executed_probes"] if isinstance(probe, Mapping))
    if replay.get("status") != "matched" or replay.get("replayed_probe_ids") != expected_ids:
        raise ValueError("platform runtime replay evidence does not cover every probe")
    if replay.get("mismatched_probe_ids", []) != [] or replay.get("skipped_probe_ids", []) != []:
        raise ValueError("platform runtime replay evidence contains mismatches or skips")
    return {"status": "matched", "replayed_probe_ids": expected_ids, "replayed_probe_count": len(expected_ids)}


def _load_report(path: Path, *, probe_id: str, reference: str) -> dict[str, Any]:
    try:
        report = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"{probe_id} runtime report is invalid") from exc
    if not isinstance(report, Mapping) or report.get("format") != "agent-native-knowledge-platform-runtime-call-graph-report/v1":
        raise ValueError(f"{probe_id} runtime report format is invalid")
    probes = report.get("probes")
    if report.get("observation_only") is not True or not isinstance(probes, list) or len(probes) != 1 or not isinstance(probes[0], Mapping):
        raise ValueError(f"{probe_id} runtime report shape is invalid")
    observed = probes[0]
    if observed.get("probe_reference") != reference or observed.get("observation_only") is not True:
        raise ValueError(f"{probe_id} runtime report identity is invalid")
    edges = observed.get("edges")
    loaded_modules = observed.get("loaded_modules")
    graph_digest = observed.get("graph_digest")
    if not isinstance(edges, list) or not edges or not isinstance(loaded_modules, list) or not loaded_modules:
        raise ValueError(f"{probe_id} runtime report evidence is incomplete")
    if not isinstance(graph_digest, str) or not graph_digest.startswith("sha256:"):
        raise ValueError(f"{probe_id} runtime report graph digest is invalid")
    return {"edges": edges, "loaded_modules": loaded_modules, "graph_digest": graph_digest}


def build_review_queue(
    *,
    registry_path: Path,
    output_path: Path,
    repo_root: Path,
    _replay_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    registry_path = _safe_absolute(registry_path, label="registry")
    output_path = _safe_absolute(output_path, label="output")
    document, validation = load_runtime_probe_registry(registry_path, repo_root=repo_root)
    replay = _replay(repo_root=repo_root, registry=registry_path) if _replay_evidence is None else _validate_replay_evidence(_replay_evidence, document=document)
    replay_digest = _sha256_bytes(json.dumps(replay, sort_keys=True, separators=(",", ":")).encode("utf-8"))
    items: list[dict[str, Any]] = []
    for raw_probe in document["executed_probes"]:
        if not isinstance(raw_probe, Mapping):
            raise ValueError("platform runtime probe is invalid")
        review = raw_probe.get("boundary_review")
        if not isinstance(review, Mapping) or review.get("status") != "pending":
            continue
        output = raw_probe.get("output")
        if not isinstance(output, Mapping) or not isinstance(output.get("path"), str):
            raise ValueError(f"{raw_probe.get('id')} output is invalid")
        relative = Path(output["path"])
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError(f"{raw_probe.get('id')} output path is invalid")
        report_path = _safe_absolute(repo_root / relative, label=f"{raw_probe.get('id')} output")
        observed = _load_report(report_path, probe_id=str(raw_probe["id"]), reference=str(raw_probe["reference"]))
        item: dict[str, Any] = {
            "byte_equal": True,
            "family": str(raw_probe["family"]),
            "graph_digest": str(observed["graph_digest"]),
            "loaded_module_count": len(observed["loaded_modules"]),
            "observed_edge_count": len(observed["edges"]),
            "output_sha256": _sha256_file(report_path),
            "probe_id": str(raw_probe["id"]),
            "probe_reference": str(raw_probe["reference"]),
            "reason": str(review["reason"]),
            "replay_runs": 1,
            "review_status": "pending",
        }
        item["review_id"] = review_id_for_item(item)
        items.append(item)
    items.sort(key=lambda item: item["probe_id"])
    pending_probe_ids = {
        str(probe["id"])
        for probe in document["executed_probes"]
        if isinstance(probe, Mapping)
        and isinstance(probe.get("boundary_review"), Mapping)
        and probe["boundary_review"].get("status") == "pending"
    }
    if {item["probe_id"] for item in items} != pending_probe_ids:
        raise ValueError("platform runtime review queue item count does not match registry")
    report: dict[str, Any] = {
        "format": "agent-native-knowledge-platform-platform-runtime-boundary-review-queue/v1",
        "status": "PHASE0A_PLATFORM_RUNTIME_BOUNDARY_REVIEW_QUEUE_READY_NOT_FROZEN",
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": "This queue is review-only; explicit boundary approval is required before the platform runtime gate can be reviewed.",
        "registry": {
            "sha256": _sha256_file(registry_path),
            "status": str(document["status"]),
            "coverage_scope": str(document["coverage_scope"]),
            "platform_runtime_coverage": "six platform probe families observed; boundary interpretations pending",
        },
        "replay": {
            "digest": replay_digest,
            "replayed_probe_count": replay["replayed_probe_count"],
            "replayed_probe_ids": replay["replayed_probe_ids"],
            "status": "matched",
        },
        "summary": {
            "all_replayed": True,
            "approval_required": True,
            "pending_review_count": len(items),
            "replayed_probe_count": replay["replayed_probe_count"],
            "review_item_count": len(items),
        },
        "items": items,
    }
    load_runtime_review_queue(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--registry", type=Path, default=Path("docs/knowledge-platform/phase-0a-runtime-call-probes.yaml"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase0a/platform-runtime-boundary-review-queue.json"))
    args = parser.parse_args()
    try:
        report = build_review_queue(
            registry_path=(args.repo_root / args.registry) if not args.registry.is_absolute() else args.registry,
            output_path=(args.repo_root / args.output) if not args.output.is_absolute() else args.output,
            repo_root=args.repo_root.resolve(),
        )
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(json.dumps({"format": "agent-native-knowledge-platform-platform-runtime-boundary-review-queue/v1", "error": str(exc)}))
        return 1
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
