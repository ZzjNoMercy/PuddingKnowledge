"""Run the external Platform MCP client against a real local child process."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Principal
from knowledge_platform.agent import AgentCapabilitySurfaceBuilder
from knowledge_platform.transport.external_mcp import (
    ExternalPlatformMcpHttpClient,
    discover_external_platform_mcp,
)
from scripts.phase8_local_platform_http_shadow import _query_summary
from scripts.phase8_local_platform_process_shadow import (
    _catalog_digest,
    _free_loopback_port,
    _stop,
    _wait_until_ready,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_WIKI_ROOT = _ROOT / "docs"
_SPACE_ID = "space_kb_default"
_COLLECTION_ID = "dataset_kb_default"


async def _invoke(*, port: int, query: str, limit: int) -> dict[str, Any]:
    config = discover_external_platform_mcp(
        {
            "enabled": True,
            "endpoint": f"http://127.0.0.1:{port}/mcp",
            "tool_allowlist": ["knowledge_query"],
        }
    )
    if config is None:
        raise RuntimeError("external MCP config unexpectedly disabled")
    client = ExternalPlatformMcpHttpClient(config, issuer_id="loopback-platform-authz")
    principal = Principal("phase8-external-mcp-shadow")
    correlation = Correlation("phase8-external-mcp-correlation")
    surface = await AgentCapabilitySurfaceBuilder(
        client,
        trusted_issuer_ids=frozenset({"loopback-platform-authz"}),
    ).build(principal=principal, correlation=correlation)
    capability_ids = [capability.capability_id for capability in surface.capabilities]
    capability_id = "platform/knowledge_query"
    if capability_id not in capability_ids:
        raise RuntimeError("external MCP tools/list did not return the allowlisted capability")
    result = await surface.invoke(
        capability_id,
        {
            "query": query,
            "space_id": _SPACE_ID,
            "collection_id": _COLLECTION_ID,
            "capability_hint": "wiki_query",
            "limit": limit,
        },
        invoker=client,
    )
    structured = result.get("structuredContent") if isinstance(result, dict) else None
    return {
        "discovered_capability_count": len(capability_ids),
        "called_capability": capability_id,
        "query_result": _query_summary(structured if isinstance(structured, dict) else {}),
        "surface_principal_bound": True,
        "surface_correlation_bound": True,
    }


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_root: Path = _DEFAULT_WIKI_ROOT,
    query: str = "PuddingClaw",
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    limit: int = 5,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    wiki_root = wiki_root.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-external-platform-mcp-e2e-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_EXTERNAL_PLATFORM_MCP_E2E_SHADOW_FAILED",
        "loopback_only": True,
        "production_endpoint": False,
        "network_contacted": True,
        "local_tool_registration": False,
        "server": None,
        "client": None,
        "canonical_catalog_unchanged": None,
        "server_shutdown_clean": None,
    }
    process: subprocess.Popen[bytes] | None = None
    canonical_before: str | None = None
    try:
        canonical_before = _catalog_digest(catalog)
        with tempfile.TemporaryDirectory(prefix="phase8-external-mcp-e2e-") as temp_dir:
            temp_root = Path(temp_dir)
            ready_file = temp_root / "ready.json"
            port = _free_loopback_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_ROOT / "backend/scripts/phase8_local_platform_process_server.py"),
                    "--catalog",
                    str(catalog),
                    "--wiki-root",
                    str(wiki_root),
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
            result["server"] = {"ready_pages": ready.get("pages"), "loopback_port_observed": True}
            invocation = asyncio.run(_invoke(port=port, query=query, limit=limit))
            result["client"] = invocation
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server_shutdown_clean"] = _stop(process)
        canonical_after = _catalog_digest(catalog) if catalog.exists() else None
        result["canonical_catalog_unchanged"] = canonical_before is not None and canonical_before == canonical_after
    if (
        isinstance(result.get("client"), dict)
        and result["client"].get("discovered_capability_count", 0) >= 1
        and result["client"].get("called_capability") == "platform/knowledge_query"
        and result["client"].get("query_result", {}).get("status") == "ok"
        and result["server_shutdown_clean"] is True
        and result["canonical_catalog_unchanged"] is True
    ):
        result["status"] = "PHASE8_EXTERNAL_PLATFORM_MCP_E2E_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-external-platform-mcp-e2e-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--query", default="PuddingClaw")
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
