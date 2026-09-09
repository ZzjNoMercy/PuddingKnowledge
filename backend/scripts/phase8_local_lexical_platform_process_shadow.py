"""Replay the local lexical Query Plane through an independent sidecar process.

The child process binds a temporary Catalog copy to the local lexical candidate
and serves REST/MCP on loopback.  This is a rehearsal artifact only: it never
activates production traffic or writes the canonical Catalog.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.vector_rebuild import VectorRebuildManifest
from knowledge_platform.retrieval import MilvusBm25CatalogRetrievalProvider
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
_DEFAULT_MANIFEST = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-manifest.json"
_DEFAULT_CHUNKS = _DEFAULT_OUTPUT_DIR / "phase8-local-vector-lexical-chunks.json"
_PROVIDER_ID = "puddingclaw_platform_candidate_lexical_text"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"
_UNSAFE_QUERY_RE = __import__("re").compile(r"(?i)token|secret|password|credential|/Users/|file://")


class _ManifestCatalog:
    """Minimal Catalog probe used only to choose a safe local query."""

    def __init__(self, manifest: VectorRebuildManifest) -> None:
        self._assets = {
            item.asset_id: {
                "space_id": manifest.space_id,
                "source_uri": f"knowledge://spaces/{manifest.space_id}/assets/{item.asset_id}",
            }
            for item in manifest.documents
        }

    def get_asset(self, *, asset_id: str) -> dict[str, str] | None:
        return self._assets.get(asset_id)


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _safe_query(*, chunks: Path, provider: MilvusBm25CatalogRetrievalProvider) -> str:
    payload = json.loads(chunks.expanduser().read_text(encoding="utf-8"))
    rows = payload.get("chunks") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        raise ValueError("lexical candidate chunks are unavailable")
    for row in rows:
        if not isinstance(row, dict) or not isinstance(row.get("text"), str):
            continue
        candidate = row["text"][:120]
        if not candidate.strip() or _UNSAFE_QUERY_RE.search(candidate):
            continue
        try:
            hits = asyncio.run(provider.search(query=candidate, space_id=_SPACE_ID, limit=1))
        except Exception:
            continue
        if hits and all(not _UNSAFE_QUERY_RE.search(hit.quote) for hit in hits):
            return candidate
    raise ValueError("no safe lexical candidate query found")


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    manifest: Path = _DEFAULT_MANIFEST,
    chunks: Path = _DEFAULT_CHUNKS,
    vector_uri: str = "http://127.0.0.1:19530",
    query: str | None = None,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    canonical_before = _digest(catalog)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-lexical-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_LEXICAL_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "query_plane": None,
        "server": None,
    }
    process: subprocess.Popen[bytes] | None = None
    try:
        manifest_value = VectorRebuildManifest.from_dict(
            json.loads(manifest.expanduser().read_text(encoding="utf-8"))
        )
        if manifest_value.provider_collection_name != _PROVIDER_ID:
            raise ValueError("lexical manifest provider does not match the fixed candidate")
        from pymilvus import MilvusClient

        client = MilvusClient(uri=vector_uri, timeout=3)
        provider = MilvusBm25CatalogRetrievalProvider(
            catalog=_ManifestCatalog(manifest_value),
            client=client,
            collection_name=_PROVIDER_ID,
            manifest=manifest_value,
        )
        query = query or _safe_query(chunks=chunks, provider=provider)
        if not query.strip() or _UNSAFE_QUERY_RE.search(query):
            raise ValueError("query is unsafe or empty")
        close_client = getattr(client, "close", None)
        if callable(close_client):
            close_client()
        with tempfile.TemporaryDirectory(prefix="phase8-local-lexical-process-shadow-") as temp_dir:
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
                    "--lexical-manifest",
                    str(manifest.expanduser().absolute()),
                    "--vector-uri",
                    vector_uri,
                ],
                cwd=str(_ROOT),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port)
            body = {
                "query": query,
                "space_id": _SPACE_ID,
                "collection_id": _COLLECTION_ID,
                "capability_hint": "document_rag_query",
                "limit": 1,
            }
            rest_status, rest_payload = _post_json(port, "/v1/knowledge/query", body)
            mcp_status, mcp_payload = _post_json(
                port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "lexical-process-mcp-1",
                    "method": "tools/call",
                    "params": {"name": "knowledge_query", "arguments": body},
                },
            )
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
        server.get("capability") == "document_rag_query"
        and server.get("provider_id") == _PROVIDER_ID
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
        result["status"] = "PHASE8_LOCAL_LEXICAL_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-lexical-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--manifest", type=Path, default=_DEFAULT_MANIFEST)
    parser.add_argument("--chunks", type=Path, default=_DEFAULT_CHUNKS)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--query")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
