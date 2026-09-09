"""Run a non-activating local Phase 7 semantic-job decision rehearsal.

The command exercises the Platform Admin decision boundary against a copied
Catalog.  It never marks a job as published: confirmation re-queues the job
for a worker, while rejection cancels it.  The canonical staged Catalog is
kept read-only and the report contains no physical paths or job metadata.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
from knowledge_platform.semantic import (
    SemanticDimensionAuthoringRequest,
    SemanticDimensionAuthoringService,
    SemanticDimensionJobDecisionRequest,
    SemanticDimensionJobDecisionService,
)

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")
_WAITING_STATUSES = {
    "waiting_for_publish_confirmation",
    "waiting_for_baseline_change_confirmation",
}


def _digest(path: Path) -> str:
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return "sha256:" + value.hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _job(database: Path, job_id: str) -> dict[str, str] | None:
    with sqlite3.connect(database) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id, scope_uri, status, published_uri FROM knowledge_authoring_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _space_from_scope(scope_uri: str) -> str:
    parts = scope_uri.removeprefix("knowledge://").split("/")
    if len(parts) != 4 or parts[0] != "spaces" or parts[2] != "semantic-dimensions" or not all(parts):
        raise ValueError("semantic job scope URI is not canonical")
    return parts[1]


def run_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    job_id: str = "",
    decision: str = "confirm",
    create_probe: bool = False,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-semantic-decision-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_SEMANTIC_DECISION_SHADOW_REJECTED_NO_EXPLICIT_JOB",
        "catalog_path_digest": _path_digest(catalog),
        "job_id": job_id or None,
        "decision": decision,
        "before": None,
        "after": None,
        "replay": None,
        "canonical_catalog_unchanged": None,
    }
    try:
        if not job_id and not create_probe:
            return _write_report(output_dir, result)
        before_catalog_digest = _digest(catalog)
        if create_probe:
            with tempfile.TemporaryDirectory(prefix="phase7-semantic-decision-shadow-") as temp_dir:
                temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
                shutil.copy2(catalog, temporary_catalog)
                with sqlite3.connect(temporary_catalog) as connection:
                    source = connection.execute(
                        "SELECT id, space_id, columns_json FROM knowledge_structured_assets "
                        "WHERE reference_status IN ('ready', 'verified', 'active') ORDER BY id LIMIT 1"
                    ).fetchone()
                if source is None:
                    result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_REJECTED_NO_APPROVED_SOURCE"
                    return _write_report(output_dir, result)
                source_id, space_id, columns_json = (str(source[0]), str(source[1]), str(source[2]))
                columns = json.loads(columns_json)
                authoring = SemanticDimensionAuthoringService(
                    catalog=SqliteCatalogQueryRepository(temporary_catalog),
                    writer=SqliteSemanticDimensionJobWriter(temporary_catalog),
                )
                queued = authoring.enqueue(
                    principal=Principal(
                        subject_id="phase7-local-admin",
                        scopes=("knowledge:admin", f"knowledge:space:{space_id}"),
                    ),
                    correlation=Correlation("phase7-local-semantic-enqueue"),
                    request=SemanticDimensionAuthoringRequest(
                        dimension_id="phase7_decision_probe",
                        space_id=space_id,
                        adapter="phase7_local_probe",
                        input_snapshot={"source_asset_ids": [source_id]},
                        title="Phase 7 local decision probe",
                    ),
                )
                if queued.status != "ok":
                    raise RuntimeError("semantic decision probe job could not be created")
                job_id = str(queued.data["job"]["id"])
                with sqlite3.connect(temporary_catalog) as connection:
                    connection.execute(
                        "UPDATE knowledge_authoring_jobs SET status = 'waiting_for_publish_confirmation', "
                        "current_step = 'waiting_for_publish_confirmation', progress = 100 WHERE id = ?",
                        (job_id,),
                    )
                result["probe_source"] = {"asset_id": source_id, "column_count": len(columns)}
                before = _job(temporary_catalog, job_id)
                result.update(_run_decision(temporary_catalog, before, job_id, decision, result))
            result["canonical_catalog_unchanged"] = _digest(catalog) == before_catalog_digest
            if not result["canonical_catalog_unchanged"]:
                raise RuntimeError("canonical Catalog changed during semantic decision shadow")
            result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_PASS_NOT_ACTIVATABLE"
            return _write_report(output_dir, result)
        before = _job(catalog, job_id)
        if before is None or before["status"] not in _WAITING_STATUSES:
            result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_REJECTED_JOB_NOT_WAITING"
            return _write_report(output_dir, result)
        try:
            space_id = _space_from_scope(before["scope_uri"])
        except ValueError:
            result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_REJECTED_JOB_SCOPE_NOT_BINDABLE"
            result["before"] = {"status": before["status"]}
            return _write_report(output_dir, result)
        with tempfile.TemporaryDirectory(prefix="phase7-semantic-decision-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog, temporary_catalog)
            service = SemanticDimensionJobDecisionService(
                writer=SqliteSemanticDimensionJobWriter(temporary_catalog),
            )
            request = SemanticDimensionJobDecisionRequest(
                job_id=job_id,
                space_id=space_id,
                decision=decision,
                expected_status=before["status"],
            )
            principal = Principal(
                subject_id="phase7-local-admin",
                scopes=("knowledge:admin", f"knowledge:space:{space_id}"),
            )
            first = service.decide(
                principal=principal,
                correlation=Correlation("phase7-local-semantic-decision"),
                request=request,
            )
            second = service.decide(
                principal=principal,
                correlation=Correlation("phase7-local-semantic-decision-retry"),
                request=request,
            )
            after = _job(temporary_catalog, job_id)
            if first.status != "ok" or second.status != "ok" or after is None:
                raise RuntimeError("semantic job decision shadow did not complete")
            expected_after = "queued" if decision == "confirm" else "cancelled"
            if after["status"] != expected_after or after["published_uri"] != before["published_uri"]:
                raise RuntimeError("semantic job decision shadow violated state boundary")
            result["before"] = {"status": before["status"], "published": bool(before["published_uri"])}
            result["after"] = {"status": after["status"], "published": bool(after["published_uri"])}
            result["replay"] = {"first_ok": True, "second_ok": True, "same_terminal_state": True}
            with sqlite3.connect(temporary_catalog) as connection:
                result["decision_event_count"] = connection.execute(
                    "SELECT COUNT(*) FROM knowledge_authoring_events "
                    "WHERE job_id = ? AND id LIKE 'authoring_decision_%'",
                    (job_id,),
                ).fetchone()[0]
        result["canonical_catalog_unchanged"] = _digest(catalog) == before_catalog_digest
        if not result["canonical_catalog_unchanged"]:
            raise RuntimeError("canonical Catalog changed during semantic decision shadow")
        result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TypeError, ValueError, LookupError, RuntimeError, sqlite3.Error):
        result["status"] = "PHASE7_SEMANTIC_DECISION_SHADOW_FAILED"
        result["error"] = "phase7 local semantic decision shadow failed"
    return _write_report(output_dir, result)


def _run_decision(
    temporary_catalog: Path,
    before: dict[str, str] | None,
    job_id: str,
    decision: str,
    result: dict[str, Any],
) -> dict[str, Any]:
    if before is None:
        raise RuntimeError("semantic decision probe job disappeared")
    space_id = _space_from_scope(before["scope_uri"])
    service = SemanticDimensionJobDecisionService(
        writer=SqliteSemanticDimensionJobWriter(temporary_catalog),
    )
    request = SemanticDimensionJobDecisionRequest(
        job_id=job_id,
        space_id=space_id,
        decision=decision,
        expected_status=before["status"],
    )
    principal = Principal(
        subject_id="phase7-local-admin",
        scopes=("knowledge:admin", f"knowledge:space:{space_id}"),
    )
    first = service.decide(
        principal=principal,
        correlation=Correlation("phase7-local-semantic-decision"),
        request=request,
    )
    second = service.decide(
        principal=principal,
        correlation=Correlation("phase7-local-semantic-decision-retry"),
        request=request,
    )
    after = _job(temporary_catalog, job_id)
    if first.status != "ok" or second.status != "ok" or after is None:
        raise RuntimeError("semantic job decision shadow did not complete")
    expected_after = "queued" if decision == "confirm" else "cancelled"
    if after["status"] != expected_after or after["published_uri"] != before["published_uri"]:
        raise RuntimeError("semantic job decision shadow violated state boundary")
    result["before"] = {"status": before["status"], "published": bool(before["published_uri"])}
    result["after"] = {"status": after["status"], "published": bool(after["published_uri"])}
    result["replay"] = {"first_ok": True, "second_ok": True, "same_terminal_state": True}
    with sqlite3.connect(temporary_catalog) as connection:
        result["decision_event_count"] = connection.execute(
            "SELECT COUNT(*) FROM knowledge_authoring_events "
            "WHERE job_id = ? AND id LIKE 'authoring_decision_%'",
            (job_id,),
        ).fetchone()[0]
    return result


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-semantic-decision-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--job-id")
    parser.add_argument("--create-probe", action="store_true")
    parser.add_argument("--decision", choices=("confirm", "reject"), default="confirm")
    args = parser.parse_args()
    result = run_shadow(
        output_dir=args.output_dir,
        job_id=args.job_id or "",
        decision=args.decision,
        create_probe=args.create_probe,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
