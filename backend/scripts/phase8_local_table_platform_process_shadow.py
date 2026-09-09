"""Replay an explicit local Structured Asset through an independent sidecar.

The parent supplies an Asset ID and an explicit local file.  The child verifies
the digest and binds only a temporary Catalog copy before serving REST/MCP.
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
    _mcp_http_summary,
    _post_json,
    _query_http_summary,
    _stop,
    _wait_until_ready,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_shadow(
    *,
    asset_id: str,
    table_file: Path,
    query: str,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    table_sheet: str | None = None,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    table_file = table_file.expanduser().absolute()
    canonical_before = _digest(catalog)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-table-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_TABLE_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "binding": {"asset_id": asset_id, "file_bound": False},
        "query_plane": None,
        "server": None,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        if not asset_id.strip() or not query.strip() or not table_file.is_file():
            raise ValueError("explicit table asset, file, and query are required")
        with tempfile.TemporaryDirectory(prefix="phase8-local-table-process-shadow-") as temp_dir:
            temp_root = Path(temp_dir)
            ready_file = temp_root / "ready.json"
            port = _free_loopback_port()
            process = subprocess.Popen(
                [
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
                    "--table-asset-id",
                    asset_id,
                    "--table-file",
                    str(table_file),
                    *(('--table-sheet', table_sheet) if table_sheet else ()),
                ],
                cwd=str(_ROOT),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port, timeout_seconds=60)
            body = {
                "query": query,
                "space_id": _SPACE_ID,
                "collection_id": _COLLECTION_ID,
                "capability_hint": "table_query",
                "limit": 5,
            }
            rest_status, rest_payload = _post_json(
                port, "/v1/knowledge/query", body, timeout_seconds=60
            )
            mcp_status, mcp_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "table-process-mcp-1",
                    "method": "tools/call",
                    "params": {"name": "knowledge_query", "arguments": body},
                },
                timeout_seconds=60,
            )
            result["binding"]["file_bound"] = True
            result["server"] = {
                "capability": ready.get("capability"),
                "provider_id": ready.get("provider_id"),
                "binding_present": ready.get("binding_present"),
                "deployment_revision": ready.get("deployment_revision"),
                "port_observed": True,
            }
            result["query_plane"] = {
                "rest": _query_http_summary(rest_status, rest_payload),
                "mcp": _mcp_http_summary(mcp_status, mcp_payload),
            }
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server_shutdown_clean"] = _stop(process)
        result["canonical_catalog_unchanged"] = canonical_before == _digest(catalog)
    server = result.get("server") or {}
    plane = result.get("query_plane") or {}
    rest = plane.get("rest") or {}
    mcp = plane.get("mcp") or {}
    if (
        result["binding"]["file_bound"] is True
        and server.get("capability") == "table_query"
        and server.get("binding_present") is True
        and server.get("deployment_revision") == "platform-local-process-v1"
        and rest.get("status") == "ok"
        and mcp.get("status") == "ok"
        and rest.get("data", {}).get("count", 0) >= 1
        and mcp.get("data", {}).get("count", 0) >= 1
        and len(rest.get("evidence", [])) >= 1
        and len(mcp.get("evidence", [])) >= 1
        and result.get("server_shutdown_clean") is True
        and result.get("canonical_catalog_unchanged") is True
    ):
        result["status"] = "PHASE8_LOCAL_TABLE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-table-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", dest="table_file", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--sheet", dest="table_sheet")
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
