"""Replay a prepared Host Asset binding through an independent loopback process.

This is a local, non-activatable rehearsal.  It consumes an already prepared
Host binding manifest, starts the existing shadow-only Platform process, reads
the bound Assets through REST and MCP, and proves that the canonical Catalog
was not changed.  It never creates an approval manifest and never emits local
paths or content in its report.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from scripts.phase8_local_platform_process_shadow import (
    _free_loopback_port,
    _post_json,
    _stop,
    _wait_until_ready,
)
from scripts.phase9_local_asset_binding_prepare import load_binding_manifest
from scripts.phase9_local_asset_http_shadow import _http_result_is_portable, _mcp_result_is_portable

_ROOT = Path(__file__).resolve().parents[2]
_SHADOW_FORMAT = "agent-knowledge-platform-local-asset-binding-process-shadow/v1"
_SPACE_ID = "space_kb_default"


def _catalog_digest(path: Path) -> str:
    import hashlib

    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _file_digest(path: Path) -> str:
    return _catalog_digest(path)


def _mcp_result_is_bound(payload: Any, *, resource_uri: str, digest: str) -> bool:
    if not _mcp_result_is_portable(payload, resource_uri=resource_uri):
        return False
    result = payload.get("result") if isinstance(payload, Mapping) else None
    structured = result.get("structuredContent") if isinstance(result, Mapping) else None
    data = structured.get("data") if isinstance(structured, Mapping) else None
    return isinstance(data, Mapping) and data.get("resource_uri") == resource_uri and data.get("asset_digest") == digest


def run_shadow(
    *,
    binding_manifest_path: Path,
    catalog_path: Path,
    wiki_root: Path,
    output_path: Path,
    space_id: str = _SPACE_ID,
    end: int = 128,
) -> dict[str, Any]:
    if end < 1 or end > 4096:
        raise ValueError("Asset process shadow range must be between 1 and 4096 bytes")
    binding_manifest_path = binding_manifest_path.expanduser().absolute()
    catalog_path = catalog_path.expanduser().absolute()
    wiki_root = wiki_root.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    if output_path == catalog_path:
        raise ValueError("shadow report must not overwrite the Catalog")

    bindings = load_binding_manifest(
        manifest_path=binding_manifest_path,
        catalog_path=catalog_path,
        space_id=space_id,
    )
    catalog_before = _catalog_digest(catalog_path)
    result: dict[str, Any] = {
        "format": _SHADOW_FORMAT,
        "status": "PHASE9_LOCAL_ASSET_BINDING_PROCESS_SHADOW_FAILED",
        "activation": "not-activated",
        "transport": {"host": "127.0.0.1", "independent_process": True},
        "summary": {
            "binding_asset_count": len(bindings),
            "verified_asset_count": 0,
            "failed_asset_count": 0,
        },
        "server": {"ready": False, "manifest_loaded": False, "shutdown_clean": False},
        "assets": [],
        "catalog": {"canonical_sha256_before": catalog_before, "canonical_unchanged": False},
    }

    process: subprocess.Popen[bytes] | None = None
    try:
        with tempfile.TemporaryDirectory(prefix="phase9-local-asset-binding-process-shadow-") as temp_dir:
            temp_root = Path(temp_dir)
            ready_file = temp_root / "ready.json"
            port = _free_loopback_port()
            process = subprocess.Popen(
                [
                    sys.executable,
                    str(_ROOT / "backend/scripts/phase8_local_platform_process_server.py"),
                    "--catalog",
                    str(catalog_path),
                    "--wiki-root",
                    str(wiki_root),
                    "--temp-dir",
                    str(temp_root / "server-data"),
                    "--port",
                    str(port),
                    "--ready-file",
                    str(ready_file),
                    "--asset-binding-manifest",
                    str(binding_manifest_path),
                ],
                cwd=str(_ROOT),
                env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            ready = _wait_until_ready(process, ready_file, port)
            result["server"] = {
                "ready": True,
                "manifest_loaded": ready.get("asset_binding_manifest_loaded") is True,
                "deployment_revision": ready.get("deployment_revision"),
            }
            if not result["server"]["manifest_loaded"]:
                raise RuntimeError("Platform process did not report the Host binding manifest")

            for asset_id in sorted(bindings):
                resource_uri = f"knowledge://spaces/{space_id}/assets/{asset_id}"
                expected_digest = _file_digest(bindings[asset_id])
                status, asset = _post_json(
                    port,
                    f"/v1/assets/{asset_id}:read",
                    {
                        "resource_uri": resource_uri,
                        "start": 0,
                        "end": min(end, max(1, bindings[asset_id].stat().st_size)),
                    },
                )
                rest_ok = status == 200 and _http_result_is_portable(
                    asset, resource_uri=resource_uri, digest=expected_digest
                )
                mcp_status, mcp = _post_json(
                    port,
                    "/mcp",
                    {
                        "jsonrpc": "2.0",
                        "id": "phase9-asset-binding-process-read",
                        "method": "resources/read",
                        "params": {
                            "uri": resource_uri,
                            "start": 0,
                            "end": min(end, max(1, bindings[asset_id].stat().st_size)),
                        },
                    },
                )
                mcp_ok = mcp_status == 200 and _mcp_result_is_bound(
                    mcp, resource_uri=resource_uri, digest=expected_digest
                )
                result["assets"].append(
                    {
                        "asset_id": asset_id,
                        "status": "verified" if rest_ok and mcp_ok else "failed",
                        "rest_portable_verified": rest_ok,
                        "mcp_portable_verified": mcp_ok,
                    }
                )
    except Exception as error:
        result["error_type"] = type(error).__name__
    finally:
        if process is not None:
            result["server"]["shutdown_clean"] = _stop(process)
        catalog_after = _catalog_digest(catalog_path) if catalog_path.is_file() else None
        result["catalog"]["canonical_unchanged"] = catalog_before == catalog_after

    verified = sum(item["status"] == "verified" for item in result["assets"])
    result["summary"]["verified_asset_count"] = verified
    result["summary"]["failed_asset_count"] = len(result["assets"]) - verified
    if (
        result["server"]["ready"]
        and result["server"]["manifest_loaded"]
        and result["server"]["shutdown_clean"]
        and result["catalog"]["canonical_unchanged"]
        and result["summary"]["binding_asset_count"] == result["summary"]["verified_asset_count"]
        and result["summary"]["failed_asset_count"] == 0
    ):
        result["status"] = "PHASE9_LOCAL_ASSET_BINDING_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--binding-manifest",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/local-asset-bindings.local.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"),
    )
    parser.add_argument("--wiki-root", type=Path, required=True)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/local-asset-binding-process-shadow-report.json"),
    )
    parser.add_argument("--space-id", default=_SPACE_ID)
    parser.add_argument("--end", type=int, default=128)
    args = parser.parse_args()
    result = run_shadow(
        binding_manifest_path=args.binding_manifest,
        catalog_path=args.catalog,
        wiki_root=args.wiki_root,
        output_path=args.output,
        space_id=args.space_id,
        end=args.end,
    )
    print(json.dumps({"status": result["status"], "summary": result["summary"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if result["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
