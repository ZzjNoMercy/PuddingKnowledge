"""Run a non-activating local Semantic Dimension worker rehearsal."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import (
    SqliteCatalogQueryRepository,
    SqliteSemanticDimensionJobStore,
    SqliteSemanticDimensionJobWriter,
)
from knowledge_platform.semantic import (
    LocalSemanticDimensionBuilder,
    LocalSemanticDimensionPublisher,
    SemanticDimensionAuthoringRequest,
    SemanticDimensionAuthoringService,
    SemanticDimensionBuildWorker,
)

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")
_SPACE_ID = "space_kb_default"
_SOURCE_ID = "structured_tbl_concat_847eed5f3f93dd93e4cb7111"
_DIMENSION_ID = "phase7_local_semantic_dimension"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR, canonical_catalog: Path | None = None) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    canonical_catalog = (canonical_catalog or output_dir / "knowledge-platform.sqlite3").expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-semantic-dimension-worker-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_SEMANTIC_DIMENSION_WORKER_SHADOW_REJECTED_NO_APPROVED_SOURCE",
        "catalog_path_digest": _path_digest(canonical_catalog),
        "source_asset_id": _SOURCE_ID,
        "job": None,
        "canonical_catalog_unchanged": None,
    }
    try:
        before_catalog_digest = _digest(canonical_catalog)
        with tempfile.TemporaryDirectory(prefix="phase7-semantic-dimension-shadow-", dir=output_dir) as temp_dir:
            temporary_catalog = Path(temp_dir) / canonical_catalog.name
            shutil.copy2(canonical_catalog, temporary_catalog)
            repository = SqliteCatalogQueryRepository(temporary_catalog)
            source = repository.get_structured_asset(asset_id=_SOURCE_ID)
            if source is None or source.get("space_id") != _SPACE_ID or source.get("reference_status") not in {"ready", "verified", "active"} or "table_query" not in set(source.get("capabilities") or ()):
                return _write_report(output_dir, result)
            principal = Principal(
                subject_id="phase7-local-semantic-worker",
                scopes=("knowledge.admin", "knowledge.semantic_authoring", f"knowledge.space:{_SPACE_ID}"),
            )
            enqueue = SemanticDimensionAuthoringService(
                catalog=repository,
                writer=SqliteSemanticDimensionJobWriter(temporary_catalog),
            ).enqueue(
                principal=principal,
                correlation=Correlation("phase7-semantic-enqueue"),
                request=SemanticDimensionAuthoringRequest(
                    dimension_id=_DIMENSION_ID,
                    space_id=_SPACE_ID,
                    adapter="local",
                    input_snapshot={"source_asset_ids": [_SOURCE_ID]},
                ),
            )
            if enqueue.status != "ok":
                raise RuntimeError("semantic dimension authoring did not enqueue")
            job = enqueue.data.get("job")
            if not isinstance(job, Mapping):
                raise RuntimeError("semantic dimension enqueue result is invalid")
            job_id = str(job.get("id") or "")
            worker = SemanticDimensionBuildWorker(
                builder=LocalSemanticDimensionBuilder(),
                jobs=SqliteSemanticDimensionJobStore(database_path=temporary_catalog),
                publisher=LocalSemanticDimensionPublisher(root=Path(temp_dir) / "semantic-publication"),
            )
            first = worker.process(job_id=job_id, space_id=_SPACE_ID)
            actor_digest = "sha256:" + hashlib.sha256(b"phase7-local-semantic-admin").hexdigest()
            correlation_digest = "sha256:" + hashlib.sha256(b"phase7-local-semantic-decision").hexdigest()
            SqliteSemanticDimensionJobWriter(temporary_catalog).decide_authoring_job(
                job_id=job_id,
                space_id=_SPACE_ID,
                decision="confirm",
                expected_status="waiting_for_publish_confirmation",
                actor_digest=actor_digest,
                correlation_digest=correlation_digest,
            )
            second = worker.process(job_id=job_id, space_id=_SPACE_ID)
            if first != second:
                raise RuntimeError("semantic dimension staging replay mismatch")
            with sqlite3.connect(temporary_catalog) as connection:
                row = connection.execute(
                    "SELECT status, current_step, progress, staging_uri, staging_reference_digest, published_uri, lease_owner, attempt "
                    "FROM knowledge_authoring_jobs WHERE id = ?",
                    (job_id,),
                ).fetchone()
                event_count = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_authoring_events WHERE job_id = ? AND id LIKE ?",
                    (job_id, f"{job_id}_staged_%"),
                ).fetchone()[0]
            if row is None or row[:3] != ("published", "published", 100) or not row[5].endswith("/published") or row[6] is not None or row[7] != 2:
                raise RuntimeError("semantic dimension terminal staging state is invalid")
            result["job"] = {
                "job_id": job_id,
                "source_asset_id": _SOURCE_ID,
                "staging_uri": row[3],
                "staging_digest": row[4],
                "published_uri": row[5],
                "attempt": row[7],
                "staging_replay_matches": first == second,
                "staging_event_count": event_count,
            }
        result["canonical_catalog_unchanged"] = _digest(canonical_catalog) == before_catalog_digest
        if not result["canonical_catalog_unchanged"]:
            raise RuntimeError("canonical Catalog changed during semantic dimension shadow")
        result["status"] = "PHASE7_SEMANTIC_DIMENSION_WORKER_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TypeError, ValueError, LookupError, RuntimeError, sqlite3.Error):
        result["status"] = "PHASE7_SEMANTIC_DIMENSION_WORKER_SHADOW_FAILED"
        result["error"] = "phase7 local semantic dimension worker shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-semantic-dimension-worker-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


if __name__ == "__main__":
    outcome = run_shadow()
    print(json.dumps({"status": outcome["status"], "report": outcome["report"]}, ensure_ascii=False))
    raise SystemExit(0 if str(outcome["status"]).endswith("NOT_ACTIVATABLE") else 1)
