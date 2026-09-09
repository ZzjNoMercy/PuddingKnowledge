"""Replay the two-phase local Database Capability through an independent sidecar.

The child owns an isolated in-memory QueryPlan repository and reads the
explicit local PostgreSQL source with the read-only adapter.  REST and MCP
both exercise ``database_nl2sql`` followed by ``database_execute_readonly``;
the canonical Catalog is never modified.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from scripts.phase8_local_platform_process_shadow import (
    _get_json,
    _post_json,
    _stop,
    _wait_until_ready,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"
_DATASET_ID = "database_insight_data_vehicle_model_base"
_QUESTION = "统计本地车型能源类型数量"


def _digest(value: object) -> str:
    return "sha256:" + hashlib.sha256(str(value).encode("utf-8")).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _structured(payload: dict[str, Any]) -> dict[str, Any]:
    result = payload.get("result")
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    return structured if isinstance(structured, dict) else {}


def _resource_summary(payload: dict[str, Any], status_code: int, *, resource_uri: str) -> dict[str, Any]:
    """Keep the resources/read observation path-free and bounded."""

    result = payload.get("result")
    result = result if isinstance(result, dict) else {}
    structured = result.get("structuredContent")
    structured = structured if isinstance(structured, dict) else {}
    data = structured.get("data")
    data = data if isinstance(data, dict) else {}
    resource = data.get("resource")
    resource = resource if isinstance(resource, dict) else {}
    contents = result.get("contents")
    contents = contents if isinstance(contents, list) else []
    first = contents[0] if contents and isinstance(contents[0], dict) else {}
    return {
        "http_status": status_code,
        "status": structured.get("status"),
        "content_count": len(contents),
        "mime_type": first.get("mimeType"),
        "uri_match": first.get("uri") == resource_uri,
        "column_count": len(resource.get("columns")) if isinstance(resource.get("columns"), list) else 0,
    }


def _plan_tokens(payload: dict[str, Any]) -> tuple[str, str] | None:
    data = payload.get("data")
    plan = data.get("query_plan") if isinstance(data, dict) else None
    if not isinstance(plan, dict):
        return None
    plan_id = plan.get("query_plan_id")
    sql_hash = data.get("sql_hash")
    if not isinstance(plan_id, str) or not isinstance(sql_hash, str):
        return None
    return plan_id, sql_hash


def _db_summary(payload: dict[str, Any], status_code: int, *, phase: str) -> dict[str, Any]:
    error = payload.get("error")
    if isinstance(error, dict):
        return {"http_status": status_code, "phase": phase, "status": "error", "error": {"code": error.get("code")}}
    data = payload.get("data")
    if not isinstance(data, dict):
        return {"http_status": status_code, "phase": phase, "status": "invalid"}
    summary: dict[str, Any] = {"http_status": status_code, "phase": phase, "status": payload.get("status")}
    if isinstance(data.get("query_plan"), dict):
        plan = data["query_plan"]
        summary["query_plan"] = {
            "plan_id_digest": _digest(plan.get("query_plan_id")),
            "sql_hash": data.get("sql_hash"),
            "deployment_revision_present": isinstance(plan.get("deployment_revision"), str),
        }
    for key in ("row_count", "columns"):
        if key in data:
            value = data[key]
            summary[key] = len(value) if key == "columns" and isinstance(value, list) else value
    summary["evidence_count"] = len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0
    return summary


def run_shadow(
    *,
    question: str = _QUESTION,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    db_host: str = "127.0.0.1",
    db_port: int = 5432,
    db_name: str = "insight_data",
    db_user: str = "pet",
    db_password_env: str = "PUDDINGCLAW_CANONICAL_DB_PASSWORD",
    vanna_collection: Path | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    canonical_before = _digest(catalog.read_bytes())
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-database-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_DATABASE_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "source": {
            "host_digest": _digest(db_host),
            "database_digest": _digest(db_name),
            "username_digest": _digest(db_user),
            "dataset_id": _DATASET_ID,
        },
        "atomic_capability": None,
        "server": None,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        if not question.strip():
            raise ValueError("database question is empty")
        with tempfile.TemporaryDirectory(prefix="phase8-local-database-process-shadow-") as temp_dir:
            temp_root = Path(temp_dir)
            ready_file = temp_root / "ready.json"
            port = _free_loopback_port()
            child_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")}
            if db_password_env in os.environ:
                child_env[db_password_env] = os.environ[db_password_env]
            child_args = [
                    sys.executable,
                    str(Path(__file__).with_name("phase8_local_platform_process_server.py")),
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
                    "--database-mode",
                    "--db-host",
                    db_host,
                    "--db-port",
                    str(db_port),
                    "--db-name",
                    db_name,
                    "--db-user",
                    db_user,
                    "--db-password-env",
                    db_password_env,
            ]
            if vanna_collection is not None:
                child_args.extend(["--database-vanna-collection", str(vanna_collection.expanduser().absolute())])
            process = subprocess.Popen(
                child_args,
                cwd=str(_ROOT),
                env=child_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port, timeout_seconds=60)
            nl_body = {"space_id": _SPACE_ID, "dataset_id": _DATASET_ID, "question": question}
            schema_status, schema_payload = _get_json(
                port,
                f"/v1/database/schema?space_id={_SPACE_ID}&dataset_id={_DATASET_ID}",
                timeout_seconds=60,
            )
            schema_data = schema_payload.get("data") if isinstance(schema_payload, dict) else None
            schema_tables = schema_data.get("tables") if isinstance(schema_data, dict) else None
            schema_summary = {
                "http_status": schema_status,
                "status": schema_payload.get("status"),
                "table_count": len(schema_tables) if isinstance(schema_tables, list) else 0,
                "source_revision_present": isinstance(schema_data, dict) and isinstance(schema_data.get("source_revision"), str),
            }
            if schema_payload.get("status") != "ok" or schema_summary["table_count"] < 1:
                raise RuntimeError("REST database schema did not return a bound table")
            rest_nl_status, rest_nl_payload = _post_json(port, "/v1/database/nl2sql", nl_body, timeout_seconds=60)
            rest_tokens = _plan_tokens(rest_nl_payload)
            if rest_tokens is None:
                raise RuntimeError("REST database_nl2sql did not return a QueryPlan")
            rest_plan_id, rest_sql_hash = rest_tokens
            rest_exec_status, rest_exec_payload = _post_json(
                port,
                f"/v1/database/query-plans/{rest_plan_id}:execute",
                {"space_id": _SPACE_ID, "expected_sql_hash": rest_sql_hash, "page_size": 20},
                timeout_seconds=60,
            )
            mcp_nl_status, mcp_nl_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "database-process-mcp-nl-1",
                    "method": "tools/call",
                    "params": {"name": "database_nl2sql", "arguments": nl_body},
                },
                timeout_seconds=60,
            )
            mcp_nl_structured = _structured(mcp_nl_payload)
            mcp_tokens = _plan_tokens(mcp_nl_structured)
            if mcp_tokens is None:
                raise RuntimeError("MCP database_nl2sql did not return a QueryPlan")
            mcp_plan_id, mcp_sql_hash = mcp_tokens
            mcp_exec_status, mcp_exec_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "database-process-mcp-execute-1",
                    "method": "tools/call",
                    "params": {
                        "name": "database_execute_readonly",
                        "arguments": {
                            "space_id": _SPACE_ID,
                            "query_plan_id": mcp_plan_id,
                            "expected_sql_hash": mcp_sql_hash,
                            "page_size": 20,
                        },
                    },
                },
                timeout_seconds=60,
            )
            mcp_exec_structured = _structured(mcp_exec_payload)
            schema_resource_uri = (
                f"knowledge://spaces/{_SPACE_ID}/databases/{_DATASET_ID}/schema/vehicle_model_base"
            )
            mcp_resource_status, mcp_resource_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "database-process-mcp-schema-read-1",
                    "method": "resources/read",
                    "params": {"uri": schema_resource_uri},
                },
                timeout_seconds=60,
            )
            result["server"] = {
                "capability": ready.get("capability"),
                "provider_id": ready.get("provider_id"),
                "database_vanna_collection_bound": ready.get("database_vanna_collection_bound"),
                "binding_present": ready.get("binding_present"),
                "deployment_revision": ready.get("deployment_revision"),
                "port_observed": True,
            }
            result["atomic_capability"] = {
                "schema": schema_summary,
                "rest": {
                    "nl2sql": _db_summary(rest_nl_payload, rest_nl_status, phase="generate"),
                    "execute": _db_summary(rest_exec_payload, rest_exec_status, phase="execute"),
                },
                "mcp": {
                    "nl2sql": _db_summary(mcp_nl_structured, mcp_nl_status, phase="generate"),
                    "execute": _db_summary(mcp_exec_structured, mcp_exec_status, phase="execute"),
                    "schema_resource": _resource_summary(
                        mcp_resource_payload,
                        mcp_resource_status,
                        resource_uri=schema_resource_uri,
                    ),
                },
            }
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server_shutdown_clean"] = _stop(process)
        result["canonical_catalog_unchanged"] = canonical_before == _digest(catalog.read_bytes())
    server = result.get("server") or {}
    atomic = result.get("atomic_capability") or {}
    schema = atomic.get("schema") or {}
    rest = atomic.get("rest") or {}
    mcp = atomic.get("mcp") or {}
    if (
        server.get("capability") == "database_nl2sql"
        and server.get("provider_id") == _DATASET_ID
        and server.get("binding_present") is True
        and server.get("deployment_revision") == "platform-local-process-v1"
        and schema.get("status") == "ok"
        and schema.get("table_count", 0) >= 1
        and schema.get("source_revision_present") is True
        and all((rest.get(key) or {}).get("status") == "ok" for key in ("nl2sql", "execute"))
        and all((mcp.get(key) or {}).get("status") == "ok" for key in ("nl2sql", "execute"))
        and (mcp.get("schema_resource") or {}).get("status") == "ok"
        and (mcp.get("schema_resource") or {}).get("uri_match") is True
        and (mcp.get("schema_resource") or {}).get("column_count", 0) >= 1
        and (rest.get("execute") or {}).get("row_count", 0) >= 1
        and (mcp.get("execute") or {}).get("row_count", 0) >= 1
        and result.get("server_shutdown_clean") is True
        and result.get("canonical_catalog_unchanged") is True
    ):
        result["status"] = "PHASE8_LOCAL_DATABASE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-database-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--question", default=_QUESTION)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--db-host", default="127.0.0.1")
    parser.add_argument("--db-port", type=int, default=5432)
    parser.add_argument("--db-name", default="insight_data")
    parser.add_argument("--db-user", default="pet")
    parser.add_argument("--db-password-env", default="PUDDINGCLAW_CANONICAL_DB_PASSWORD")
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
