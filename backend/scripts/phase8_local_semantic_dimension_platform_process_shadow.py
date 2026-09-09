"""Continue Semantic Dimension Processing across two local Platform processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository, SqliteSemanticDimensionJobWriter
from knowledge_platform.semantic import SemanticDimensionAuthoringRequest, SemanticDimensionAuthoringService
from scripts.phase8_local_platform_process_shadow import _post_json, _stop, _wait_until_ready

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_SOURCE_ID = "structured_tbl_concat_847eed5f3f93dd93e4cb7111"
_DIMENSION_ID = "phase8_process_semantic_dimension"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"http_status": status_code, "status": payload.get("status")}
    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {"code": error.get("code")}
        return summary
    data = payload.get("data")
    if not isinstance(data, dict):
        return summary
    artifact = data.get("artifact")
    if isinstance(artifact, dict):
        summary["artifact"] = {
            key: artifact.get(key)
            for key in ("staging_uri", "staging_digest")
            if key in artifact
        }
    job = data.get("job")
    if isinstance(job, dict):
        summary["job"] = {
            key: job.get(key)
            for key in ("id", "state", "status", "current_step", "progress")
            if key in job
        }
    return summary


def _start_server(
    *, catalog: Path, temp_root: Path, job_id: str
) -> tuple[subprocess.Popen[bytes], int, Path, dict[str, Any]]:
    temp_root.mkdir(parents=True, exist_ok=True)
    ready_file = temp_root / "ready.json"
    port = _free_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(_ROOT / "backend/scripts/phase8_local_platform_process_server.py"),
            "--catalog",
            str(catalog),
            "--wiki-root",
            str(_ROOT / "docs"),
            "--temp-dir",
            str(temp_root / "server-data"),
            "--port",
            str(port),
            "--ready-file",
            str(ready_file),
            "--semantic-dimension-mode",
            "--semantic-job-id",
            job_id,
            "--semantic-space-id",
            _SPACE_ID,
        ],
        cwd=str(_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = _wait_until_ready(process, ready_file, port, timeout_seconds=30)
    return process, port, temp_root / "server-data" / "knowledge-platform.sqlite3", ready


def _job_state(path: Path, job_id: str) -> dict[str, Any]:
    with sqlite3.connect(path) as connection:
        row = connection.execute(
            "SELECT status, current_step, progress, staging_uri, staging_reference_digest, published_uri, attempt "
            "FROM knowledge_authoring_jobs WHERE id = ?",
            (job_id,),
        ).fetchone()
    if row is None:
        return {}
    return {
        "status": row[0],
        "current_step": row[1],
        "progress": row[2],
        "staging_uri": row[3],
        "staging_digest": row[4],
        "published_uri": row[5],
        "attempt": row[6],
    }


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-semantic-dimension-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_SEMANTIC_DIMENSION_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_processes": 2},
        "source_asset_id": _SOURCE_ID,
        "server": {"first": None, "second": None},
        "authoring": None,
        "processing": {"first": None, "second": None},
        "decision": None,
        "canonical_catalog_unchanged": None,
    }
    first_process: subprocess.Popen[bytes] | None = None
    second_process: subprocess.Popen[bytes] | None = None
    canonical_before = _digest(catalog)
    try:
        with tempfile.TemporaryDirectory(
            prefix="phase8-local-semantic-dimension-platform-process-",
            **({"dir": "/private/tmp"} if Path("/private/tmp").is_dir() else {}),
        ) as temp_dir:
            root = Path(temp_dir)
            seed_catalog = root / "seed.sqlite3"
            shutil.copy2(catalog, seed_catalog)
            repository = SqliteCatalogQueryRepository(seed_catalog)
            source = repository.get_structured_asset(asset_id=_SOURCE_ID)
            if (
                source is None
                or source.get("space_id") != _SPACE_ID
                or source.get("reference_status") not in {"ready", "verified", "active"}
                or "table_query" not in set(source.get("capabilities") or ())
            ):
                raise ValueError("approved local Structured Asset is unavailable")
            principal = Principal(
                subject_id="phase8-local-semantic-dimension-platform-process-shadow",
                scopes=("knowledge.admin", "knowledge.semantic_authoring", f"knowledge.space:{_SPACE_ID}"),
            )
            enqueue = SemanticDimensionAuthoringService(
                catalog=repository,
                writer=SqliteSemanticDimensionJobWriter(seed_catalog),
            ).enqueue(
                principal=principal,
                correlation=Correlation("phase8-semantic-process-authoring"),
                request=SemanticDimensionAuthoringRequest(
                    dimension_id=_DIMENSION_ID,
                    space_id=_SPACE_ID,
                    adapter="local",
                    input_snapshot={"source_asset_ids": [_SOURCE_ID]},
                ),
            )
            if enqueue.status != "ok" or not isinstance(enqueue.data.get("job"), Mapping):
                raise ValueError("semantic dimension authoring did not enqueue")
            job_id = str(enqueue.data["job"].get("id") or "")
            if not job_id:
                raise ValueError("semantic dimension job identity is missing")
            result["authoring"] = {"status": "queued", "job_id": job_id, "source_count": 1}

            first_root = root / "first"
            first_process, first_port, first_catalog, first_ready = _start_server(
                catalog=seed_catalog, temp_root=first_root, job_id=job_id
            )
            process_status, process_payload = _post_json(
                first_port,
                f"/v1/semantic-dimensions/jobs/{job_id}:process",
                {"space_id": _SPACE_ID},
                timeout_seconds=30,
            )
            result["server"]["first"] = {
                "capability": first_ready.get("capability"),
                "binding_present": first_ready.get("binding_present"),
                "deployment_revision": first_ready.get("deployment_revision"),
            }
            result["processing"]["first"] = _summary(process_status, process_payload)
            first_state = _job_state(first_catalog, job_id)
            result["processing"]["first"]["durable_state"] = {
                key: first_state.get(key)
                for key in ("status", "current_step", "progress", "staging_uri", "staging_digest", "attempt")
                if key in first_state
            }
            result["first_server_shutdown_clean"] = _stop(first_process)
            first_process = None

            second_root = root / "second"
            second_process, second_port, second_catalog, second_ready = _start_server(
                catalog=first_catalog, temp_root=second_root, job_id=job_id
            )
            decision_status, decision_payload = _post_json(
                second_port,
                f"/v1/semantic-dimensions/jobs/{job_id}:decision",
                {
                    "space_id": _SPACE_ID,
                    "decision": "confirm",
                    "expected_status": "waiting_for_publish_confirmation",
                },
                timeout_seconds=30,
            )
            result["decision"] = _summary(decision_status, decision_payload)
            publish_status, publish_payload = _post_json(
                second_port,
                f"/v1/semantic-dimensions/jobs/{job_id}:process",
                {"space_id": _SPACE_ID},
                timeout_seconds=30,
            )
            result["server"]["second"] = {
                "capability": second_ready.get("capability"),
                "binding_present": second_ready.get("binding_present"),
                "deployment_revision": second_ready.get("deployment_revision"),
            }
            result["processing"]["second"] = _summary(publish_status, publish_payload)
            second_state = _job_state(second_catalog, job_id)
            result["processing"]["second"]["durable_state"] = {
                key: second_state.get(key)
                for key in ("status", "current_step", "progress", "staging_uri", "staging_digest", "published_uri", "attempt")
                if key in second_state
            }
            result["second_server_shutdown_clean"] = _stop(second_process)
            second_process = None
            result["restart_continuation"] = (
                result["processing"]["first"].get("status") == "ok"
                and result["processing"]["second"].get("status") == "ok"
                and result["decision"].get("status") == "ok"
                and first_state.get("status") == "waiting_for_publish_confirmation"
                and second_state.get("status") == "published"
                and first_state.get("staging_digest") == second_state.get("staging_digest")
                and second_state.get("published_uri", "").endswith("/published")
            )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        result["error_type"] = "bounded_local_process_failure"
    finally:
        if first_process is not None:
            result["first_server_shutdown_clean"] = _stop(first_process)
        if second_process is not None:
            result["second_server_shutdown_clean"] = _stop(second_process)
        result["canonical_catalog_unchanged"] = _digest(catalog) == canonical_before
    if (
        result.get("restart_continuation") is True
        and result.get("first_server_shutdown_clean", True)
        and result.get("second_server_shutdown_clean", True)
        and result["canonical_catalog_unchanged"] is True
        and result["server"]["first"].get("binding_present") is True
        and result["server"]["second"].get("binding_present") is True
        and result["server"]["first"].get("deployment_revision") == "platform-local-process-v1"
        and result["server"]["second"].get("deployment_revision") == "platform-local-process-v1"
    ):
        result["status"] = "PHASE8_LOCAL_SEMANTIC_DIMENSION_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-semantic-dimension-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    result = run_shadow(**vars(parser.parse_args()))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
