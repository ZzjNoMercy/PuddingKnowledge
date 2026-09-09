"""Build a path-free review queue for real local Crosswalk collisions."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_platform.semantic.crosswalk_collision_review import load_crosswalk_collision_review_queue


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


def _item_review_id(item: dict[str, Any]) -> str:
    payload = {key: item[key] for key in item if key != "review_id"}
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def build_review_queue(*, report_path: Path, output_path: Path) -> dict[str, Any]:
    report_path = _safe_absolute(report_path, label="Crosswalk report")
    output_path = _safe_absolute(output_path, label="output")
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Crosswalk shadow report is invalid") from exc
    if not isinstance(report, dict) or report.get("status") != "PHASE7_CROSSWALK_REAL_CANONICAL_SHADOW_BLOCKED_DATA_QUALITY":
        raise ValueError("Crosswalk report is not the expected data-quality blocker")
    summaries = report.get("canonical_collision_summaries")
    source = report.get("canonical_source")
    if not isinstance(summaries, list) or not summaries or not isinstance(source, dict):
        raise ValueError("Crosswalk collision evidence is incomplete")
    items: list[dict[str, Any]] = []
    for summary in summaries:
        if not isinstance(summary, dict) or set(summary) != {"candidate_count", "candidate_digests", "normalized_key_digest"}:
            raise ValueError("Crosswalk collision summary fields are invalid")
        candidates = summary["candidate_digests"]
        if summary["candidate_count"] != 2 or not isinstance(candidates, list) or len(candidates) != 2:
            raise ValueError("Crosswalk collision candidate evidence is invalid")
        item: dict[str, Any] = {
            "candidate_count": 2,
            "candidate_digests": sorted(str(value) for value in candidates),
            "decision_status": "pending",
            "normalized_key_digest": str(summary["normalized_key_digest"]),
        }
        item["review_id"] = _item_review_id(item)
        items.append(item)
    items.sort(key=lambda item: item["normalized_key_digest"])
    # Recompute IDs after sorting only affects presentation, not identity.
    report_out: dict[str, Any] = {
        "format": "agent-knowledge-platform-crosswalk-collision-review-queue/v1",
        "status": "PHASE7_CROSSWALK_COLLISION_REVIEW_REQUIRED_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": "This queue records collision evidence only; explicit identity review is required before a Crosswalk candidate can be prepared.",
        "canonical_source": {
            "content_digest": str(report["source_snapshot"]["content_digest"]),
            "identity_basis": list(source.get("identity_basis", [])),
            "stable_identity_columns": list(source.get("stable_identity_columns", [])),
        },
        "summary": {
            "approval_required": True,
            "collision_count": len(items),
            "pending_review_count": len(items),
        },
        "items": items,
    }
    load_crosswalk_collision_review_queue(report_out)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report_out, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report_out


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path, default=Path("artifacts/phase0b-local-catalog/phase7-local-crosswalk-real/phase7-local-crosswalk-real-canonical-shadow-report.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase0b-local-catalog/phase7-local-crosswalk-real/crosswalk-collision-review-queue.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        report = build_review_queue(
            report_path=(root / args.report) if not args.report.is_absolute() else args.report,
            output_path=(root / args.output) if not args.output.is_absolute() else args.output,
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-crosswalk-collision-review-queue/v1", "error": str(exc)}))
        return 1
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
