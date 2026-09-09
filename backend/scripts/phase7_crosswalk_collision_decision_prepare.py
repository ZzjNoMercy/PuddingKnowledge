"""Prepare an explicit, non-activating local Crosswalk collision decision candidate."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from knowledge_platform.semantic.crosswalk_collision_decision import (
    CrosswalkCollisionDecisionError,
    load_crosswalk_collision_decision,
    prepare_crosswalk_collision_decision,
)


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


def _selection(value: str) -> dict[str, str]:
    review_id, separator, candidate_digest = value.partition("=")
    if not separator or not review_id or not candidate_digest:
        raise ValueError("selection must be REVIEW_ID=CANDIDATE_DIGEST")
    return {"review_id": review_id, "candidate_digest": candidate_digest}


def prepare(*, queue_path: Path, output_path: Path, selections: list[str]) -> dict[str, object]:
    queue_path = _safe_absolute(queue_path, label="queue")
    output_path = _safe_absolute(output_path, label="output")
    if output_path.exists():
        raise ValueError("output candidate already exists")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    candidate = prepare_crosswalk_collision_decision(queue, [_selection(value) for value in selections])
    load_crosswalk_collision_decision(candidate)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {**candidate, "output": str(output_path)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-queue",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/phase7-local-crosswalk-real/crosswalk-collision-review-queue.json"),
    )
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--select-review-candidate",
        action="append",
        default=[],
        metavar="REVIEW_ID=CANDIDATE_DIGEST",
        help="repeat exactly once for every pending collision review",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        result = prepare(
            queue_path=(root / args.review_queue) if not args.review_queue.is_absolute() else args.review_queue,
            output_path=(root / args.output) if not args.output.is_absolute() else args.output,
            selections=args.select_review_candidate,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError, CrosswalkCollisionDecisionError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-crosswalk-collision-decision-candidate/v1", "error": str(exc)}))
        return 1
    print(json.dumps({"status": result["status"], "summary": result["summary"], "output": result["output"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
