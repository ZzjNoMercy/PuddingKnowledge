"""Run a non-activating local gbrain projection rehearsal."""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.gbrain import GbrainProjectionRequest, LocalGbrainProjectionService

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def run_shadow(*, output_dir: Path, catalog: Path, space_id: str, asset_id: str, source_path: Path) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = catalog.expanduser().absolute()
    source_path = source_path.expanduser().absolute()
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-gbrain-projection-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_GBRAIN_PROJECTION_SHADOW_REJECTED_NO_EXPLICIT_BINDING",
        "catalog_path_digest": _path_digest(catalog),
        "source_path_digest": _path_digest(source_path),
        "space_id": space_id,
        "asset_id": asset_id,
        "projection": None,
        "canonical_catalog_unchanged": None,
    }
    try:
        before_catalog_digest = _digest(catalog)
        with sqlite3.connect(catalog) as connection:
            row = connection.execute(
                "SELECT space_id, source_uri, revision, content_digest FROM knowledge_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
        if row is None or row[0] != space_id or row[3] != _digest(source_path) or row[2] != row[3]:
            result["status"] = "PHASE7_GBRAIN_PROJECTION_SHADOW_REJECTED_ASSET_DIGEST_OR_SPACE_MISMATCH"
            return _write_report(output_dir, result)
        markdown = source_path.read_text(encoding="utf-8")
        request = GbrainProjectionRequest(
            space_id=space_id,
            asset_id=asset_id,
            source_uri=row[1],
            published_uri=f"knowledge://spaces/{space_id}/wiki/{asset_id}",
            source_revision=row[2],
            published_digest=row[3],
            published_markdown=markdown,
            idempotency_key=f"phase7-local-gbrain-{asset_id}-{row[3].removeprefix('sha256:')[:24]}",
        )
        with tempfile.TemporaryDirectory(prefix="phase7-gbrain-projection-shadow-", dir=output_dir) as temp_dir:
            service = LocalGbrainProjectionService(root=Path(temp_dir) / "gbrain")
            first = service.project(request)
            second = service.project(request)
            if first != second or service.projection_count != 1:
                raise RuntimeError("gbrain projection idempotency rehearsal failed")
            result["projection"] = {
                "projection_uri": first.projection_uri,
                "source_uri": first.source_uri,
                "published_uri": first.published_uri,
                "source_revision": first.source_revision,
                "published_digest": first.published_digest,
                "bytes": first.bytes,
                "schema_pack": first.schema_pack,
                "second_matches_first": first == second,
                "gbrain_binary_invoked": False,
            }
        result["canonical_catalog_unchanged"] = _digest(catalog) == before_catalog_digest
        if not result["canonical_catalog_unchanged"]:
            raise RuntimeError("canonical Catalog changed during gbrain projection shadow")
        result["status"] = "PHASE7_GBRAIN_PROJECTION_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, UnicodeError, TypeError, ValueError, LookupError, RuntimeError, sqlite3.Error):
        result["status"] = "PHASE7_GBRAIN_PROJECTION_SHADOW_FAILED"
        result["error"] = "phase7 local gbrain projection shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-gbrain-projection-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3")
    parser.add_argument("--space-id", required=True)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    result = run_shadow(
        output_dir=args.output_dir,
        catalog=args.catalog,
        space_id=args.space_id,
        asset_id=args.asset_id,
        source_path=args.file,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
