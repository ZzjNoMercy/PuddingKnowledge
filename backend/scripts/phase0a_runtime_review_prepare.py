"""Prepare a reviewed platform runtime registry candidate from explicit approvals.

This command writes a candidate registry only. It never edits the checked-in
registry and requires every pending review ID from the content-addressed queue.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import yaml

from knowledge_platform.baseline import load_runtime_probe_registry, validate_runtime_probe_registry
from knowledge_platform.baseline.runtime_review_queue import load_runtime_review_queue
from scripts.phase0a_runtime_review_queue import _replay


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


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _load_queue(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("platform runtime review queue is invalid") from exc
    return load_runtime_review_queue(raw)


def prepare_reviewed_registry(
    *,
    registry_path: Path,
    queue_path: Path,
    output_path: Path,
    repo_root: Path,
    approved_review_ids: Sequence[str],
    _replay_evidence: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    registry_path = _safe_absolute(registry_path, label="registry")
    queue_path = _safe_absolute(queue_path, label="review queue")
    output_path = _safe_absolute(output_path, label="output")
    if output_path.exists():
        raise ValueError("output candidate already exists")
    queue = _load_queue(queue_path)
    if queue["registry"]["sha256"] != _sha256_file(registry_path):
        raise ValueError("review queue registry digest does not match registry")
    requested = list(approved_review_ids)
    if not requested or len(requested) != len(set(requested)):
        raise ValueError("approved review IDs must be non-empty and unique")
    expected = {str(item["review_id"]) for item in queue["items"]}
    if set(requested) != expected:
        raise ValueError("every pending review ID must be explicitly approved")

    document, validation = load_runtime_probe_registry(registry_path, repo_root=repo_root)
    replay = _replay(repo_root=repo_root, registry=registry_path) if _replay_evidence is None else dict(_replay_evidence)
    replay_for_validation = {
        "status": replay.get("status"),
        "replayed_probe_ids": replay.get("replayed_probe_ids", []),
        "mismatched_probe_ids": replay.get("mismatched_probe_ids", []),
        "skipped_probe_ids": replay.get("skipped_probe_ids", []),
    }
    expected_probe_ids = set(str(probe_id) for probe_id in validation.executed_probe_ids)
    if (
        replay_for_validation["status"] != "matched"
        or set(replay_for_validation["replayed_probe_ids"]) != expected_probe_ids
        or replay_for_validation["mismatched_probe_ids"] != []
        or replay_for_validation["skipped_probe_ids"] != []
    ):
        raise ValueError("platform runtime replay is not a complete match")

    approved_by_probe = {str(item["probe_id"]): str(item["review_id"]) for item in queue["items"]}
    for raw_probe in document["executed_probes"]:
        if not isinstance(raw_probe, dict):
            raise ValueError("platform runtime probe is invalid")
        probe_id = str(raw_probe["id"])
        review = raw_probe.get("boundary_review")
        if not isinstance(review, dict) or review.get("status") != "pending":
            continue
        if probe_id not in approved_by_probe:
            raise ValueError(f"missing review queue item for {probe_id}")
        review["status"] = "reviewed"
        review["approval_review_id"] = approved_by_probe[probe_id]
        review["approval_source"] = "explicit-local-review-queue"

    reviewed = validate_runtime_probe_registry(
        {**document, "status": "complete"},
        repo_root=repo_root,
        require_complete=True,
        replay_evidence=replay_for_validation,
    )
    if not reviewed.complete or validation.complete:
        raise ValueError("review candidate state is inconsistent")
    document["status"] = "complete"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with output_path.open("x", encoding="utf-8") as stream:
            yaml.safe_dump(document, stream, allow_unicode=True, sort_keys=False)
    except FileExistsError as exc:
        raise ValueError("output candidate already exists") from exc
    return {
        "status": "PHASE0A_PLATFORM_RUNTIME_BOUNDARY_REVIEW_CANDIDATE_READY_NOT_FROZEN",
        "approved_review_count": len(requested),
        "replayed_probe_count": len(replay_for_validation["replayed_probe_ids"]),
        "reviewed": True,
        "output": str(output_path),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--registry", type=Path, default=Path("docs/knowledge-platform/phase-0a-runtime-call-probes.yaml"))
    parser.add_argument("--review-queue", type=Path, default=Path("artifacts/phase0a/platform-runtime-boundary-review-queue.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--approve-review-id", action="append", default=[])
    args = parser.parse_args()
    root = args.repo_root.resolve()
    try:
        result = prepare_reviewed_registry(
            registry_path=(root / args.registry) if not args.registry.is_absolute() else args.registry,
            queue_path=(root / args.review_queue) if not args.review_queue.is_absolute() else args.review_queue,
            output_path=(root / args.output) if not args.output.is_absolute() else args.output,
            repo_root=root,
            approved_review_ids=args.approve_review_id,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-platform-runtime-boundary-review-candidate/v1", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
