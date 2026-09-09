"""Replay the local Database Package through a snapshot Workspace boundary.

This is deliberately a Workspace-only check.  It does not rebuild the Package,
open a database connection, or execute the packaged SQL.  The invalid database
URL injected into the child CLI makes an accidental live-connection regression
observable without contacting a real endpoint.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.package import WorkspaceMaterializer, validate_package

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_PACKAGE = _ROOT / "artifacts/phase9-local-database-vanna-replay/package"
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase9-local-database-vanna-replay"
_INVALID_DATABASE_URL = "postgresql://invalid.invalid/blocked"
_SAFE_TEMP_ROOT = Path("/private/tmp")


def _load_json(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"snapshot JSON is not an object: {path.name}")
    return payload


def _run_cli(workspace: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    environment = os.environ.copy()
    environment["PUDDINGCLAW_DATABASE_URL"] = _INVALID_DATABASE_URL
    return subprocess.run(
        [str(workspace / "bin/knowledge"), *arguments],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )


def run_shadow(
    *,
    package_root: Path = _DEFAULT_PACKAGE,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase9-local-database-workspace-shadow/v1",
        "status": "PHASE9_LOCAL_DATABASE_WORKSPACE_SHADOW_BLOCKED",
        "activation_allowed": False,
        "live_connection": False,
        "database_row_io_performed": False,
        "source_paths_emitted": False,
        "sql_emitted": False,
    }
    try:
        package_root = package_root.expanduser().resolve()
        validation = validate_package(package_root)
        collections = _load_json(package_root / "collections/index.json").get("collections", [])
        sources = _load_json(package_root / "database/index.json").get("sources", [])
        if not isinstance(collections, list) or not isinstance(sources, list) or len(collections) != 1 or len(sources) != 1:
            raise ValueError("shadow requires exactly one packaged Database Collection and source")
        collection = collections[0]
        source = sources[0]
        if not isinstance(collection, dict) or not isinstance(source, dict):
            raise ValueError("packaged Database Collection/source is invalid")
        dataset_id = str(collection.get("id") or "")
        examples = source.get("sql_examples")
        if not dataset_id or not isinstance(examples, list) or len(examples) != 1 or not isinstance(examples[0], dict):
            raise ValueError("shadow requires exactly one packaged SQL example")
        question = str(examples[0].get("question") or "")
        if not question:
            raise ValueError("packaged SQL example question is empty")

        if _SAFE_TEMP_ROOT.is_symlink() or not _SAFE_TEMP_ROOT.is_dir():
            raise RuntimeError("safe temporary root is unavailable")
        with tempfile.TemporaryDirectory(
            prefix="phase9-local-database-workspace-",
            dir=_SAFE_TEMP_ROOT,
        ) as temporary:
            workspace = Path(temporary) / "workspace"
            materialized = WorkspaceMaterializer().materialize(
                package_root=package_root,
                workspace_root=workspace,
            )
            validated = _run_cli(workspace, "validate")
            if validated.returncode != 0:
                raise RuntimeError("materialized Workspace validation failed")
            validation_payload = json.loads(validated.stdout)
            candidate = _run_cli(
                workspace,
                "database",
                "nl2sql",
                "--dataset",
                dataset_id,
                "--question",
                question,
            )
            if candidate.returncode != 0:
                raise RuntimeError("materialized Workspace NL2SQL candidate failed")
            candidate_payload = _load_json_from_text(candidate.stdout)
            provenance = candidate_payload.get("provenance")
            query_plan = candidate_payload.get("query_plan")
            if not isinstance(provenance, dict) or not isinstance(query_plan, dict):
                raise ValueError("Workspace NL2SQL response lacks provenance or query plan")
            if (
                candidate_payload.get("status") != "candidate"
                or provenance.get("provider_versions") != {"nl2sql": "snapshot-package-evidence"}
                or provenance.get("workspace_mode") != "snapshot"
                or provenance.get("live_connection") is not False
                or query_plan.get("execution_allowed") is not False
            ):
                raise ValueError("Workspace snapshot provenance or execution fence is invalid")
            query_plan_id = str(query_plan.get("query_plan_id") or "")
            if not query_plan_id:
                raise ValueError("Workspace query plan id is empty")
            execution = _run_cli(
                workspace,
                "database",
                "execute",
                "--query-plan",
                query_plan_id,
            )
            if execution.returncode == 0 or "connected Platform database contract" not in execution.stderr:
                raise ValueError("Workspace database execution did not fail closed")
            result.update(
                {
                    "status": "PHASE9_LOCAL_DATABASE_WORKSPACE_SHADOW_PASS_NOT_ACTIVATABLE",
                    "package_revision": materialized.package_revision,
                    "package_file_count": validation.file_count,
                    "workspace_file_count": materialized.file_count,
                    "workspace_validation_status": validation_payload.get("status"),
                    "dataset_id": dataset_id,
                    "dataset_version": candidate_payload.get("dataset_version"),
                    "evidence_count": len(candidate_payload.get("evidence", [])),
                    "provider_version": provenance["provider_versions"]["nl2sql"],
                    "execution_fail_closed": True,
                    "invalid_database_url_injected": True,
                    "database_url_contacted": False,
                }
            )
    except Exception as error:
        result["error_type"] = type(error).__name__
    report_path = output_dir / "phase9-local-database-workspace-shadow.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def _load_json_from_text(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("CLI response is not an object")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package", type=Path, default=_DEFAULT_PACKAGE)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(package_root=args.package, output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
