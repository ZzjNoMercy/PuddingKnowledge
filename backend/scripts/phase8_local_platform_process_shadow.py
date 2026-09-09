"""Replay the local Platform edge through a real loopback child process.

The child serves only an isolated Catalog copy and explicitly supplied local
Wiki root.  The parent calls it over TCP, shuts it down, and proves that the
canonical local Catalog bytes did not change.  This is evidence only and is
never activatable as production traffic.
"""

from __future__ import annotations

import argparse
import hashlib
import http.client
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from scripts.phase8_local_platform_http_shadow import _query_summary

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


def _catalog_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _post_json(
    port: int, path: str, payload: dict[str, Any], *, timeout_seconds: float = 2
) -> tuple[int, dict[str, Any]]:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
    try:
        connection.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        response = connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw.decode("utf-8"))
        return response.status, decoded if isinstance(decoded, dict) else {"status": "invalid"}
    finally:
        connection.close()


def _get_json(
    port: int, path: str, *, timeout_seconds: float = 2
) -> tuple[int, dict[str, Any]]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout_seconds)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        raw = response.read()
        decoded = json.loads(raw.decode("utf-8"))
        return response.status, decoded if isinstance(decoded, dict) else {"status": "invalid"}
    finally:
        connection.close()


def _query_http_summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    summary = _query_summary(payload)
    summary["http_status"] = status_code
    return summary


def _mcp_http_summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    error = payload.get("error")
    if isinstance(error, dict):
        return {"http_status": status_code, "status": "error", "error": {"code": error.get("code")}}
    result = payload.get("result")
    if not isinstance(result, dict):
        return {"http_status": status_code, "status": "invalid"}
    structured = result.get("structuredContent")
    summary = _query_summary(structured if isinstance(structured, dict) else {})
    summary["http_status"] = status_code
    return summary


def _wait_until_ready(
    process: subprocess.Popen[bytes], ready_file: Path, port: int, *, timeout_seconds: float = 10
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise RuntimeError("shadow server exited before readiness")
        if ready_file.exists():
            ready = json.loads(ready_file.read_text(encoding="utf-8"))
            if ready.get("status") != "ready":
                raise RuntimeError("shadow server failed readiness")
            try:
                status, payload = _post_json(port, "/mcp", {"jsonrpc": "2.0", "id": "ready", "method": "ping"})
            except (ConnectionError, OSError, json.JSONDecodeError):
                time.sleep(0.05)
                continue
            if status == 200 and payload.get("jsonrpc") == "2.0":
                return ready
        time.sleep(0.05)
    raise TimeoutError("shadow server readiness timed out")


def _stop(process: subprocess.Popen[bytes]) -> bool:
    if process.poll() is not None:
        return True
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
    return process.poll() is not None


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_root: Path,
    query: str,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    limit: int = 5,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "http": None,
    }
    process: subprocess.Popen[bytes] | None = None
    canonical_before: str | None = None
    canonical_after: str | None = None
    try:
        catalog = catalog.expanduser().absolute()
        canonical_before = _catalog_digest(catalog)
        with tempfile.TemporaryDirectory(prefix="phase8-local-platform-process-shadow-") as temp_dir:
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
                    str(wiki_root.expanduser().absolute()),
                    "--temp-dir",
                    str(temp_root / "server-data"),
                    "--port",
                    str(port),
                    "--ready-file",
                    str(ready_file),
                ],
                cwd=str(_ROOT),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port)
            wiki_status, wiki_payload = _post_json(
                port, "/v1/wiki/query", {"query": query, "space_id": _SPACE_ID, "limit": limit}
            )
            routed_status, routed_payload = _post_json(
                port,
                "/v1/knowledge/query",
                {
                    "query": query,
                    "space_id": _SPACE_ID,
                    "collection_id": _COLLECTION_ID,
                    "capability_hint": "wiki_query",
                    "limit": limit,
                },
            )
            mcp_status, mcp_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "process-mcp-1",
                    "method": "tools/call",
                    "params": {
                        "name": "knowledge_query",
                        "arguments": {
                            "query": query,
                            "space_id": _SPACE_ID,
                            "collection_id": _COLLECTION_ID,
                            "capability_hint": "wiki_query",
                            "limit": limit,
                        },
                    },
                },
            )
            result["server"] = {
                "ready_pages": ready.get("pages"),
                "port_observed": True,
                "deployment_revision": ready.get("deployment_revision"),
            }
            result["http"] = {
                "wiki_query": _query_http_summary(wiki_status, wiki_payload),
                "knowledge_query": _query_http_summary(routed_status, routed_payload),
                "mcp_knowledge_query": _mcp_http_summary(mcp_status, mcp_payload),
            }
    except Exception as error:  # reports must not expose lower-layer details
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server_shutdown_clean"] = _stop(process)
        canonical_after = _catalog_digest(catalog) if catalog.exists() else None
    result["canonical_catalog_unchanged"] = canonical_before is not None and canonical_before == canonical_after
    http_result = result.get("http")
    if (
        isinstance(http_result, dict)
        and http_result.get("wiki_query", {}).get("status") == "ok"
        and http_result.get("knowledge_query", {}).get("status") == "ok"
        and http_result.get("mcp_knowledge_query", {}).get("status") == "ok"
        and result.get("server_shutdown_clean") is True
        and result["canonical_catalog_unchanged"] is True
        and result.get("server", {}).get("deployment_revision") == "platform-local-process-v1"
    ):
        result["status"] = "PHASE8_LOCAL_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument("--query", required=True)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    result = run_shadow(
        catalog=args.catalog,
        wiki_root=args.wiki_root,
        query=args.query,
        output_dir=args.output_dir,
        limit=args.limit,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
