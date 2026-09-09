"""Replay explicitly bound local Assets through the REST and MCP read edges.

The command uses the human-review manifest only as a source of candidate
evidence.  It copies the staged Catalog into a temporary directory, supplies
the local paths to the HTTP app as host-owned bindings, and reads bounded
bytes through both public edges.  It never persists a binding, changes the
Catalog, or emits a physical path or content body in its report.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import re
import shutil
import tempfile
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from knowledge_contracts import Principal
from knowledge_platform.catalog.sqlite_query import SqliteCatalogQueryRepository
from scripts.phase8_local_platform_http_shadow import _build_app
from scripts.phase9_local_asset_rebind_shadow import _confirmed_candidates, _load_review_manifest

_SHADOW_FORMAT = "agent-knowledge-platform-local-asset-http-shadow/v1"
_PATH_RE = re.compile(
    r"(?i)(?:file://|(?<![A-Za-z0-9_])[A-Za-z]:[\\/]|\\\\|/(?:Users|private|tmp|var|home|etc|opt|usr|root|mnt|Applications|System|Volumes)/)"
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def _http_result_is_portable(payload: Any, *, resource_uri: str, digest: str) -> bool:
    if not isinstance(payload, Mapping) or payload.get("status") != "ok":
        return False
    data = payload.get("data")
    evidence = payload.get("evidence")
    if not isinstance(data, Mapping) or not isinstance(evidence, list) or not evidence:
        return False
    try:
        bounded = base64.b64decode(str(data["content_base64"]), validate=True)
    except (KeyError, TypeError, ValueError):
        return False
    return bool(
        data.get("resource_uri") == resource_uri
        and data.get("asset_digest") == digest
        and data.get("content_digest") == "sha256:" + hashlib.sha256(bounded).hexdigest()
        and evidence[0].get("resource_uri") == resource_uri
        and not _PATH_RE.search(json.dumps({"data": dict(data), "evidence": evidence}, ensure_ascii=False))
    )


def _mcp_result_is_portable(payload: Any, *, resource_uri: str) -> bool:
    if not isinstance(payload, Mapping):
        return False
    result = payload.get("result")
    structured = result.get("structuredContent") if isinstance(result, Mapping) else None
    if not isinstance(result, Mapping) or not isinstance(structured, Mapping) or structured.get("status") != "ok":
        return False
    contents = result.get("contents")
    if not isinstance(contents, list) or len(contents) != 1 or not isinstance(contents[0], Mapping):
        return False
    content = contents[0]
    return bool(
        content.get("uri") == resource_uri
        and isinstance(content.get("blob"), str)
        and not _PATH_RE.search(json.dumps(dict(content), ensure_ascii=False))
    )


def run_shadow(
    *,
    review_manifest_path: Path,
    catalog_path: Path,
    output_path: Path,
    space_id: str = "space_kb_default",
    end: int = 128,
) -> dict[str, Any]:
    manifest, manifest_sha256 = _load_review_manifest(review_manifest_path)
    catalog_path = catalog_path.expanduser().absolute()
    output_path = output_path.expanduser().absolute()
    if end < 1 or end > 4096:
        raise ValueError("Asset HTTP shadow range must be between 1 and 4096 bytes")
    canonical_catalog_sha256 = _sha256(catalog_path)
    candidate_entries = _confirmed_candidates(manifest)
    digest_to_path: dict[str, Path] = {}
    for entry in candidate_entries:
        digest_to_path.setdefault(entry["digest"], entry["path"])

    report: dict[str, Any] = {
        "format": _SHADOW_FORMAT,
        "status": "PHASE9_LOCAL_ASSET_HTTP_SHADOW_FAILED",
        "activation": "not-activated",
        "mode": "explicit-host-binding-http-mcp-read-only",
        "review_manifest": {"sha256": manifest_sha256, "status": manifest["status"]},
        "summary": {
            "review_item_count": len(manifest["items"]),
            "confirmed_candidate_item_count": len(candidate_entries),
            "unique_candidate_digest_count": len(digest_to_path),
            "asset_match_count": 0,
            "verified_asset_count": 0,
            "failed_asset_count": 0,
        },
        "catalog": {"canonical_sha256_before": canonical_catalog_sha256, "canonical_unchanged": False},
        "assets": [],
    }
    with tempfile.TemporaryDirectory(prefix="phase9-local-asset-http-shadow-") as temp_dir:
        temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
        shutil.copy2(catalog_path, temporary_catalog)
        repository = SqliteCatalogQueryRepository(temporary_catalog)
        assets = [
            dict(asset)
            for asset in repository.list_assets(space_id=space_id)
            if str(asset.get("content_digest") or "") in digest_to_path
        ]
        report["summary"]["asset_match_count"] = len(assets)
        bindings = {str(asset["id"]): digest_to_path[str(asset["content_digest"])] for asset in assets}
        principal = Principal(
            "phase9-local-asset-http-shadow",
            scopes=("knowledge.list", "knowledge.read", f"knowledge.space:{space_id}"),
        )
        app = _build_app(repository, bindings, principal)
        with TestClient(app) as client:
            for asset in sorted(assets, key=lambda value: str(value.get("id") or "")):
                asset_id = str(asset["id"])
                resource_uri = str(asset["source_uri"])
                digest = str(asset["content_digest"])
                bound_path = bindings[asset_id]
                read_end = min(end, max(1, bound_path.stat().st_size))
                rest_response = client.post(
                    f"/v1/assets/{asset_id}:read",
                    json={
                        "resource_uri": resource_uri,
                        "start": 0,
                        "end": read_end,
                        "expected_digest": digest,
                    },
                )
                rest_payload = rest_response.json()
                mcp_response = client.post(
                    "/mcp",
                    json={
                        "jsonrpc": "2.0",
                        "id": "phase9-asset-read",
                        "method": "resources/read",
                        "params": {"uri": resource_uri, "start": 0, "end": read_end},
                    },
                )
                mcp_payload = mcp_response.json()
                rest_ok = _http_result_is_portable(rest_payload, resource_uri=resource_uri, digest=digest)
                mcp_ok = _mcp_result_is_portable(mcp_payload, resource_uri=resource_uri)
                report["assets"].append(
                    {
                        "asset_id": asset_id,
                        "resource_uri": resource_uri,
                        "content_digest": digest,
                        "read_bytes": read_end,
                        "rest_status": rest_payload.get("status"),
                        "mcp_status": mcp_payload.get("result", {}).get("structuredContent", {}).get("status")
                        if isinstance(mcp_payload.get("result"), Mapping)
                        else None,
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
        and report["summary"]["confirmed_candidate_item_count"] > 0
        and report["summary"]["failed_asset_count"] == 0
        and report["summary"]["verified_asset_count"] == report["summary"]["asset_match_count"]
    ):
        report["status"] = "PHASE9_LOCAL_ASSET_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--review-manifest",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/restore-rebind-review.json"),
    )
    parser.add_argument(
        "--catalog",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/knowledge-platform.sqlite3"),
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/local-asset-http-shadow-report.json"),
    )
    parser.add_argument("--space-id", default="space_kb_default")
    parser.add_argument("--end", type=int, default=128)
    args = parser.parse_args()
    report = run_shadow(
        review_manifest_path=args.review_manifest,
        catalog_path=args.catalog,
        output_path=args.output,
        space_id=args.space_id,
        end=args.end,
    )
    print(json.dumps({"status": report["status"], "summary": report["summary"], "report": str(args.output)}, ensure_ascii=False))
    return 0 if report["status"].endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
