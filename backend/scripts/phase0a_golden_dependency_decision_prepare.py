"""Prepare a Golden record candidate for an explicitly selected dependency policy."""

from __future__ import annotations

import copy
import hashlib
import importlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_platform.baseline import capture_baseline_record_document
from knowledge_platform.baseline.golden_dependency_review import load_golden_dependency_review_queue

_OBSERVER_REFERENCE = "scripts.phase0a_connectors_capture_observer:observe"
_OBSERVER_PATH = "backend/scripts/phase0a_connectors_capture_observer.py"
_VALID_DECISIONS = {"include_observer", "exclude_observer"}


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


def _record_bytes(record: Mapping[str, object]) -> bytes:
    return (json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _capture_observation() -> Mapping[str, object]:
    observer = getattr(importlib.import_module(_OBSERVER_REFERENCE.split(":", 1)[0]), "observe")
    observation = observer()
    if not isinstance(observation, Mapping):
        raise ValueError("connector observer must return a mapping")
    return observation


def _capture(
    *, decision: str, source_revision: str, observation: Mapping[str, object] | None = None
) -> dict[str, object]:
    observation = _capture_observation() if observation is None else observation
    if decision == "exclude_observer":
        evidence = observation.get("evidence")
        if not isinstance(evidence, Mapping):
            raise ValueError("connector observer evidence is invalid")
        dependencies = evidence.get("implementation_dependencies")
        if not isinstance(dependencies, list):
            raise ValueError("connector observer dependency evidence is invalid")
        matching = [
            entry
            for entry in dependencies
            if isinstance(entry, Mapping) and entry.get("path") == _OBSERVER_PATH
        ]
        if len(matching) != 1:
            raise ValueError("connector observer self dependency evidence is not uniquely identifiable")
        filtered = [entry for entry in dependencies if entry is not matching[0]]
        evidence = copy.deepcopy(dict(evidence))
        evidence["implementation_dependencies"] = filtered
    else:
        evidence = observation["evidence"]
    return capture_baseline_record_document(
        capability_id="connectors_and_capture",
        source_revision=source_revision,
        result=observation["result"],
        evidence=evidence,
        database_side_effects=observation["database_side_effects"],
        filesystem_side_effects=observation["filesystem_side_effects"],
        provider_revision=observation["provider_revision"],
        failure_semantics=observation["failure_semantics"],
        sanitized_fixture_manifest=observation["sanitized_fixture_manifest"],
    )


def prepare_candidate(
    *,
    queue_path: Path,
    canonical_path: Path,
    output_path: Path,
    review_id: str,
    decision: str,
) -> dict[str, Any]:
    queue_path = _safe_absolute(queue_path, label="queue")
    canonical_path = _safe_absolute(canonical_path, label="canonical")
    output_path = _safe_absolute(output_path, label="output")
    if decision not in _VALID_DECISIONS:
        raise ValueError("decision must be include_observer or exclude_observer")
    if output_path.exists():
        raise ValueError("output candidate already exists")
    queue = load_golden_dependency_review_queue(json.loads(queue_path.read_text(encoding="utf-8")))
    item = queue["items"][0]
    if review_id != item["review_id"]:
        raise ValueError("review ID does not match the current Golden decision queue")
    canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    if not isinstance(canonical, Mapping) or canonical.get("capability_id") != "connectors_and_capture":
        raise ValueError("canonical Golden record is invalid")
    if queue["items"][0]["canonical_record_sha256"] != _sha256_file(canonical_path):
        raise ValueError("Golden decision queue canonical record is stale")
    observation = _capture_observation()
    current_include_candidate = _capture(
        decision="include_observer", source_revision=str(canonical["source_revision"]), observation=observation
    )
    if queue["items"][0]["candidate_record_sha256"] != f"sha256:{hashlib.sha256(_record_bytes(current_include_candidate)).hexdigest()}":
        raise ValueError("Golden decision queue candidate record is stale")
    candidate = current_include_candidate if decision == "include_observer" else _capture(
        decision="exclude_observer", source_revision=str(canonical["source_revision"]), observation=observation
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as stream:
        stream.write(_record_bytes(candidate).decode("utf-8"))
    return {
        "format": "agent-knowledge-platform-golden-dependency-candidate/v1",
        "status": "PHASE0A_GOLDEN_DEPENDENCY_CANDIDATE_READY_NOT_FROZEN",
        "decision": decision,
        "review_id": review_id,
        "candidate_record_sha256": _sha256_file(output_path),
        "canonical_unchanged": True,
        "activation_allowed": False,
        "output": str(output_path),
    }


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-queue", type=Path, default=Path("artifacts/phase0a/golden-dependency-review-queue.json"))
    parser.add_argument("--canonical", type=Path, default=Path("docs/knowledge-platform/golden-records/connectors_and_capture.json"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--review-id", required=True)
    parser.add_argument("--decision", choices=sorted(_VALID_DECISIONS), required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        report = prepare_candidate(
            queue_path=(root / args.review_queue) if not args.review_queue.is_absolute() else args.review_queue,
            canonical_path=(root / args.canonical) if not args.canonical.is_absolute() else args.canonical,
            output_path=(root / args.output) if not args.output.is_absolute() else args.output,
            review_id=args.review_id,
            decision=args.decision,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-golden-dependency-candidate/v1", "error": str(exc)}))
        return 1
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
