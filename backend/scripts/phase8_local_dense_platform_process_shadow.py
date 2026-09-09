"""Replay the local dense Query Plane through an independent sidecar process."""

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
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-rebuild-manifest.json"
_DEFAULT_MODEL_DIR = Path("/Users/pet/models/jina-embeddings-v4-vllm-retrieval")
_DENSE_PROVIDER_ID = "puddingclaw_platform_candidate_text"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


def _catalog_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest: Path = _DEFAULT_MANIFEST,
    model_dir: Path = _DEFAULT_MODEL_DIR,
    dimension: int = 2048,
    max_length: int = 1024,
    query: str = "knowledge platform",
    vector_uri: str = "http://127.0.0.1:19530",
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    limit: int = 3,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().absolute()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-dense-platform-process-shadow/v1",
        "status": "PHASE8_LOCAL_DENSE_PLATFORM_PROCESS_SHADOW_FAILED",
        "activation_allowed": False,
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "candidate_collection_name": _DENSE_PROVIDER_ID,
        "catalog_binding_written": False,
        "production_endpoint": False,
        "network_contacted": False,
        "external_network_contacted": False,
        "source_paths_emitted": False,
        "query_text_emitted": False,
        "vector_values_emitted": False,
    }
    process: subprocess.Popen[bytes] | None = None
    canonical_before: str | None = None
    try:
        if not isinstance(query, str) or not query.strip() or len(query) > 512:
            raise ValueError("dense process query is invalid")
        if type(dimension) is not int or dimension < 1:
            raise ValueError("dense process dimension is invalid")
        if type(max_length) is not int or not 32 <= max_length <= 8192:
            raise ValueError("dense process max_length is invalid")
        if type(limit) is not int or not 1 <= limit <= 10:
            raise ValueError("dense process limit is invalid")
        canonical_before = _catalog_digest(catalog)
        with tempfile.TemporaryDirectory(prefix="phase8-local-dense-process-shadow-") as temp_dir:
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
                    "--dense-manifest",
                    str(manifest.expanduser().absolute()),
                    "--embedding-model-dir",
                    str(model_dir.expanduser().absolute()),
                    "--embedding-dimension",
                    str(dimension),
                    "--embedding-max-length",
                    str(max_length),
                    "--vector-uri",
                    vector_uri,
                ],
                cwd=str(_ROOT),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port, timeout_seconds=45)
            body = {
                "query": query,
                "space_id": _SPACE_ID,
                "collection_id": _COLLECTION_ID,
                "capability_hint": "document_rag_query",
                "limit": limit,
            }
            rest_status, rest_payload = _post_json(port, "/v1/document-rag/query", body, timeout_seconds=10)
            routed_status, routed_payload = _post_json(port, "/v1/knowledge/query", body, timeout_seconds=10)
            mcp_status, mcp_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "dense-process-mcp-1",
                    "method": "tools/call",
                    "params": {"name": "knowledge_query", "arguments": body},
                },
                timeout_seconds=10,
            )
            responses = [rest_payload, routed_payload, mcp_payload]
            serialized = json.dumps(responses, ensure_ascii=False)
            if any(marker in serialized for marker in ("source_path", "/Users/", "file://")):
                raise RuntimeError("dense process response exposed a physical source path")
            result["server"] = {
                "capability": ready.get("capability"),
                "provider_id": ready.get("provider_id"),
                "binding_present": ready.get("binding_present"),
                "deployment_revision": ready.get("deployment_revision"),
                "port_observed": True,
            }
            result["query_plane"] = {
                "rest_document": _query_http_summary(rest_status, rest_payload),
                "routed_knowledge": _query_http_summary(routed_status, routed_payload),
                "mcp_knowledge": _mcp_http_summary(mcp_status, mcp_payload),
            }
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server_shutdown_clean"] = _stop(process)
        result["canonical_catalog_unchanged"] = canonical_before is not None and canonical_before == _catalog_digest(catalog)
    server = result.get("server") or {}
    plane = result.get("query_plane") or {}
    if (
        server.get("capability") == "document_rag_query"
        and server.get("provider_id") == _DENSE_PROVIDER_ID
        and server.get("binding_present") is True
        and server.get("deployment_revision") == "platform-local-process-v1"
        and plane.get("rest_document", {}).get("status") == "ok"
        and plane.get("routed_knowledge", {}).get("status") == "ok"
        and plane.get("mcp_knowledge", {}).get("status") == "ok"
        and plane.get("rest_document", {}).get("data", {}).get("count", 0) >= 1
        and plane.get("routed_knowledge", {}).get("data", {}).get("count", 0) >= 1
        and plane.get("mcp_knowledge", {}).get("data", {}).get("count", 0) >= 1
        and result.get("server_shutdown_clean") is True
        and result.get("canonical_catalog_unchanged") is True
    ):
        result["status"] = "PHASE8_LOCAL_DENSE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-dense-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--model-dir", type=Path, default=_DEFAULT_MODEL_DIR)
    parser.add_argument("--dimension", type=int, default=2048)
    parser.add_argument("--max-length", type=int, default=1024)
    parser.add_argument("--query", default="knowledge platform")
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--limit", type=int, default=3)
    result = run_shadow(**vars(parser.parse_args()))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
