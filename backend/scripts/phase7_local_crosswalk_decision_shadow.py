"""Build a non-activating Crosswalk candidate from an explicit local decision candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from knowledge_platform.semantic import build_vehicle_series_crosswalk, resolve_crosswalk_collision_policy
from knowledge_platform.semantic.crosswalk_collision_decision import load_crosswalk_collision_decision
from knowledge_platform.semantic.crosswalk_collision_review import load_crosswalk_collision_review_queue
from scripts.phase7_local_crosswalk_shadow import _read_canonical_rows, _read_source_rows

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_QUEUE = _ROOT / "artifacts/phase0b-local-catalog/phase7-local-crosswalk-real/crosswalk-collision-review-queue.json"
_DEFAULT_CANDIDATE = _ROOT / "artifacts/phase7-local-crosswalk/crosswalk-collision-decision-candidate.json"
_DEFAULT_OUTPUT = _ROOT / "artifacts/phase7-local-crosswalk/crosswalk-shadow-candidate.json"
_DEFAULT_FILE = Path("/Users/pet/Documents/knowledge/imported/20260710/2023年7月乘用车市场上险量.xlsx")


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


def _file_digest(path: Path) -> str:
    return f"sha256:{hashlib.sha256(path.read_bytes()).hexdigest()}"


def _document_digest(document: object) -> str:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


async def run_shadow(
    *,
    queue_path: Path,
    decision_path: Path,
    output_path: Path,
    source_path: Path,
    source_row_limit: int,
    db_host: str,
    db_port: int,
    db_name: str,
    db_user: str,
) -> dict[str, Any]:
    queue_path = _safe_absolute(queue_path, label="queue")
    decision_path = _safe_absolute(decision_path, label="decision candidate")
    output_path = _safe_absolute(output_path, label="output")
    source_path = _safe_absolute(source_path, label="source")
    if output_path.exists():
        raise ValueError("Crosswalk shadow candidate output already exists")
    queue = json.loads(queue_path.read_text(encoding="utf-8"))
    decision = json.loads(decision_path.read_text(encoding="utf-8"))
    normalized_queue = load_crosswalk_collision_review_queue(queue)
    normalized_decision = load_crosswalk_collision_decision(decision)
    canonical_rows, canonical_columns = await _read_canonical_rows(
        host=db_host, port=db_port, database=db_name, user=db_user
    )
    source_rows, source_rows_read = _read_source_rows(source_path, row_limit=source_row_limit)
    queue_digest = _document_digest(normalized_queue)
    decision_queue = normalized_decision["queue"]
    if not isinstance(decision_queue, dict) or decision_queue["content_digest"] != queue_digest:
        raise ValueError("Crosswalk decision candidate is bound to a stale queue")
    source_digest = _file_digest(source_path)
    if normalized_queue["canonical_source"]["content_digest"] != source_digest:
        raise ValueError("Crosswalk source snapshot is stale")
    policy = resolve_crosswalk_collision_policy(
        queue=normalized_queue,
        decision_candidate=normalized_decision,
        canonical_rows=canonical_rows,
    )
    generated = build_vehicle_series_crosswalk(
        canonical_rows=canonical_rows,
        source_rows=source_rows,
        canonical_source_ref="database:dbs_77982e981bac4a6fa8:vehicle_model_base",
        source_ref="table_asset:tbl_da58c9ee25ba7a6a44efef65",
        canonical_collision_policy=policy,
    )
    encoded = (json.dumps(generated, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    candidate = {
        "format": "agent-knowledge-platform-crosswalk-decision-shadow-candidate/v1",
        "status": "PHASE7_CROSSWALK_COLLISION_DECISION_SHADOW_PASS_NOT_ACTIVATABLE",
        "activation": "not-activated",
        "execution_allowed": False,
        "activation_allowed": False,
        "canonical_unchanged": True,
        "queue_content_digest": queue_digest,
        "decision_candidate_digest": _file_digest(decision_path),
        "source_snapshot_digest": source_digest,
        "canonical_source": {
            "row_count": len(canonical_rows),
            "columns": canonical_columns,
            "stable_identity_columns": [],
            "identity_basis": ["brand", "serial_name"],
        },
        "source_snapshot": {
            "bounded_row_limit": source_row_limit,
            "rows_read": source_rows_read,
            "distinct_rows": len(source_rows),
        },
        "crosswalk_content_digest": f"sha256:{hashlib.sha256(encoded).hexdigest()}",
        "crosswalk": generated,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("x", encoding="utf-8") as stream:
        stream.write(json.dumps(candidate, ensure_ascii=False, indent=2, sort_keys=True) + "\n")
    return {"status": candidate["status"], "output": str(output_path), "crosswalk_content_digest": candidate["crosswalk_content_digest"]}


def main() -> int:
    import asyncio
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--review-queue", type=Path, default=_DEFAULT_QUEUE)
    parser.add_argument("--decision-candidate", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=_DEFAULT_OUTPUT)
    parser.add_argument("--file", type=Path, default=_DEFAULT_FILE)
    parser.add_argument("--source-row-limit", type=int, default=5000)
    parser.add_argument("--db-host", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_HOST", "127.0.0.1"))
    parser.add_argument("--db-port", type=int, default=int(os.getenv("PUDDINGCLAW_CANONICAL_DB_PORT", "5432")))
    parser.add_argument("--db-name", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_NAME", "insight_data"))
    parser.add_argument("--db-user", default=os.getenv("PUDDINGCLAW_CANONICAL_DB_USER", "pet"))
    args = parser.parse_args()
    try:
        result = asyncio.run(
            run_shadow(
                queue_path=args.review_queue,
                decision_path=args.decision_candidate,
                output_path=args.output,
                source_path=args.file,
                source_row_limit=args.source_row_limit,
                db_host=args.db_host,
                db_port=args.db_port,
                db_name=args.db_name,
                db_user=args.db_user,
            )
        )
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        print(json.dumps({"format": "agent-knowledge-platform-crosswalk-decision-shadow-candidate/v1", "error": str(exc)}))
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
