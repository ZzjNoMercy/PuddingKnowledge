"""Replay a prepared Host Asset binding manifest through REST and MCP.

The binding manifest is the only source of local paths in this shadow.  The
Catalog is copied to a temporary directory, and the public HTTP/MCP edges are
served by the existing Platform composition.  The command never writes the
Catalog, exposes a host path, or changes activation state.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from knowledge_contracts import Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from scripts.phase8_local_platform_http_shadow import _build_app
from scripts.phase9_local_asset_binding_prepare import load_binding_manifest
from scripts.phase9_local_asset_http_shadow import _http_result_is_portable, _mcp_result_is_portable

_SHADOW_FORMAT = "agent-knowledge-platform-local-asset-binding-http-shadow/v1"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _manifest_sha256(path: Path) -> str:
    return _sha256(path.expanduser().absolute())


def run_shadow(
    *,
    binding_manifest_path: Path,
    catalog_path: Path,
    output_path: Path,
    space_id: str = "space_kb_default",
    end: int = 128,
) -> dict[str, Any]:
    binding_manifest_path = binding_manifest_path.expanduser().absolute()
    catalog_path = catalog_path.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    if end < 1 or end > 4096:
        raise ValueError("Asset HTTP shadow range must be between 1 and 4096 bytes")
    if output_path == catalog_path:
        raise ValueError("shadow report must not overwrite the Catalog")

    bindings = load_binding_manifest(
        manifest_path=binding_manifest_path,
        catalog_path=catalog_path,
        space_id=space_id,
    )
    catalog_before = _sha256(catalog_path)
    report: dict[str, Any] = {
        "format": _SHADOW_FORMAT,
        "status": "PHASE9_LOCAL_ASSET_BINDING_HTTP_SHADOW_FAILED",
        "activation": "not-activated",
        "mode": "prepared-host-binding-http-mcp-read-only",
        "binding_manifest": {
            "sha256": _manifest_sha256(binding_manifest_path),
            "status": "APPROVED_HOST_BINDINGS",
        },
        "summary": {
            "binding_asset_count": len(bindings),
            "asset_match_count": 0,
            "verified_asset_count": 0,
            "failed_asset_count": 0,
        },
        "catalog": {
            "canonical_sha256_before": catalog_before,
            "canonical_sha256_after": None,
            "canonical_unchanged": False,
        },
        "assets": [],
    }

    with tempfile.TemporaryDirectory(prefix="phase9-local-asset-binding-http-shadow-") as temp_dir:
        temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
        shutil.copy2(catalog_path, temporary_catalog)
        repository = SqliteCatalogQueryRepository(temporary_catalog)
        if _sha256(temporary_catalog) != catalog_before:
            raise ValueError("Catalog changed while preparing the HTTP shadow copy")
        assets = []
        for asset_id in sorted(bindings):
            asset = repository.get_asset(asset_id=asset_id)
            if asset is None or str(asset.get("space_id") or "") != space_id:
                raise ValueError("prepared binding Asset is unavailable in the Catalog copy")
            assets.append(dict(asset))
        report["summary"]["asset_match_count"] = len(assets)
        principal = Principal(
            "phase9-local-asset-binding-http-shadow",
            scopes=("knowledge.list", "knowledge.read", f"knowledge.space:{space_id}"),
        )
        app = _build_app(repository, bindings, principal)
        with TestClient(app) as client:
            for asset in assets:
                asset_id = str(asset["id"])
                resource_uri = str(asset["source_uri"])
                digest = str(asset["content_digest"])
                bound_path = bindings[asset_id]
                read_end = min(end, max(1, bound_path.stat().st_size))
                rest_payload = client.post(
                    f"/v1/assets/{asset_id}:read",
                    json={
                        "resource_uri": resource_uri,
                        "start": 0,
                        "end": read_end,
                        "expected_digest": digest,
                    },
                ).json()
                mcp_payload = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-asset-binding-read",
                        "method": "resources/read",
                        "params": {"uri": resource_uri, "start": 0, "end": read_end},
                    },
                ).json()
                rest_ok = _http_result_is_portable(rest_payload, resource_uri=resource_uri, digest=digest)
                mcp_ok = _mcp_result_is_portable(mcp_payload, resource_uri=resource_uri)
                report["assets"].append(
                    {
                        "asset_id": asset_id,
                        "resource_uri": resource_uri,
                        "content_digest": digest,
                        "read_bytes": read_end,
                        "status": "verified" if rest_ok and mcp_ok else "failed",
                        "rest_portable_verified": rest_ok,
                        "mcp_portable_verified": mcp_ok,
                    }
                )

    verified = sum(item["status"] == "verified" for item in report["assets"])
    report["summary"]["verified_asset_count"] = verified
    report["summary"]["failed_asset_count"] = len(report["assets"]) - verified
    report["catalog"]["canonical_sha256_after"] = _sha256(catalog_path)
    report["catalog"]["canonical_unchanged"] = (
        report["catalog"]["canonical_sha256_before"] == report["catalog"]["canonical_sha256_after"]
    )
    if (
        report["catalog"]["canonical_unchanged"]
        and report["summary"]["binding_asset_count"] > 0
        and report["summary"]["failed_asset_count"] == 0
        and report["summary"]["verified_asset_count"] == report["summary"]["asset_match_count"]
    ):
        report["status"] = "PHASE9_LOCAL_ASSET_BINDING_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


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
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/local-asset-binding-http-shadow-report.json"),
    )
    parser.add_argument("--space-id", default="space_kb_default")
    parser.add_argument("--end", type=int, default=128)
    args = parser.parse_args()
    report = run_shadow(
        binding_manifest_path=args.binding_manifest,
        catalog_path=args.catalog,
        output_path=args.output,
        space_id=args.space_id,
        end=args.end,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
