"""Build a path-free queue for an explicit Golden dependency decision."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_platform.baseline.golden_dependency_review import load_golden_dependency_review_queue


def _sha256_file(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


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


def build_review_queue(*, replay_report_path: Path, candidate_path: Path, canonical_path: Path, output_path: Path) -> dict[str, Any]:
    replay_report_path = _safe_absolute(replay_report_path, label="replay report")
    candidate_path = _safe_absolute(candidate_path, label="candidate")
    canonical_path = _safe_absolute(canonical_path, label="canonical")
    output_path = _safe_absolute(output_path, label="output")
    try:
        replay = json.loads(replay_report_path.read_text(encoding="utf-8"))
        candidate = json.loads(candidate_path.read_text(encoding="utf-8"))
        canonical = json.loads(canonical_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError("Golden review inputs are invalid") from exc
    if not isinstance(replay, dict) or replay.get("status") != "GOLDEN_REPLAY_MISMATCH_NOT_FROZEN":
        raise ValueError("Golden replay report is not the expected non-frozen mismatch")
    capabilities = replay.get("capabilities")
    if not isinstance(capabilities, list):
        raise ValueError("Golden replay report capabilities are invalid")
    mismatch = next((item for item in capabilities if isinstance(item, dict) and item.get("capability_id") == "connectors_and_capture"), None)
    if not isinstance(mismatch, dict) or mismatch.get("status") != "mismatch":
        raise ValueError("connectors_and_capture Golden mismatch is unavailable")
    if not isinstance(candidate, dict) or not isinstance(canonical, dict) or candidate.get("capability_id") != canonical.get("capability_id"):
        raise ValueError("Golden records do not describe the same capability")
    differing_fields = sorted(key for key in set(candidate) | set(canonical) if candidate.get(key) != canonical.get(key))
    if differing_fields != list(mismatch.get("differing_fields", [])):
        raise ValueError("Golden candidate differs from replay report")
    drifts = mismatch.get("dependency_digest_drifts")
    if not isinstance(drifts, list) or not drifts:
        raise ValueError("Golden dependency drift is unavailable")
    item: dict[str, Any] = {
        "candidate_record_sha256": _sha256_file(candidate_path),
        "canonical_record_sha256": _sha256_file(canonical_path),
        "capability_id": "connectors_and_capture",
        "decision_status": "pending",
        "dependency_digest_drifts": sorted(drifts, key=lambda value: value["path"]),
        "differing_fields": differing_fields,
        "options": [
            {
                "id": "include_observer",
                "description": "显式接受 recapture candidate，并将 observer 自身视为 implementation dependency；仅生成新的 baseline candidate，不自动冻结。",
            },
            {
                "id": "exclude_observer",
                "description": "将 observer 视为测试工具而非业务实现依赖；先调整证据口径并重新 capture，再决定是否冻结。",
            },
        ],
    }
    import hashlib as _hashlib

    encoded = json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    item["review_id"] = f"sha256:{_hashlib.sha256(encoded).hexdigest()}"
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-golden-dependency-review-queue/v1",
        "status": "PHASE0A_GOLDEN_DEPENDENCY_REVIEW_REQUIRED_NOT_FROZEN",
        "activation": "not-activated",
        "execution_allowed": False,
        "policy": "This queue records an explicit Golden dependency decision; it never changes canonical baseline or readiness.",
        "replay": {
            "mismatch_count": int(replay["summary"]["mismatched_count"]),
            "report_sha256": _sha256_file(replay_report_path),
            "status": "mismatch",
        },
        "summary": {"approval_required": True, "pending_review_count": 1, "review_item_count": 1},
        "items": [item],
    }
    load_golden_dependency_review_queue(report)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-report", type=Path, default=Path("artifacts/phase0a/golden-replay-report.json"))
    parser.add_argument("--candidate", type=Path, default=Path("artifacts/phase0a/golden-recapture-candidates/connectors_and_capture.json"))
    parser.add_argument("--canonical", type=Path, default=Path("docs/knowledge-platform/golden-records/connectors_and_capture.json"))
    parser.add_argument("--output", type=Path, default=Path("artifacts/phase0a/golden-dependency-review-queue.json"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    try:
        report = build_review_queue(
            replay_report_path=(root / args.replay_report) if not args.replay_report.is_absolute() else args.replay_report,
            candidate_path=(root / args.candidate) if not args.candidate.is_absolute() else args.candidate,
            canonical_path=(root / args.canonical) if not args.canonical.is_absolute() else args.canonical,
            output_path=(root / args.output) if not args.output.is_absolute() else args.output,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-golden-dependency-review-queue/v1", "error": str(exc)}))
        return 1
    print(json.dumps({"status": report["status"], "summary": report["summary"], "output": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
