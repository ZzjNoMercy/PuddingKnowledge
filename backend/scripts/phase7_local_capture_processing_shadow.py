"""Run a non-activating local Read Later/Web Capture processing rehearsal."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import sqlite3
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.capture import (
    CaptureProcessingRequest,
    CaptureProcessingWorker,
    LocalCapturePublishingService,
    SqliteCaptureProcessingJobStore,
)
from knowledge_platform.wiki import LocalImmutableRawSnapshotRepository

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _asset(catalog: Path, capture_id: str) -> dict[str, str] | None:
    with sqlite3.connect(catalog) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            """
            SELECT w.id AS capture_id, w.space_id, w.asset_id, w.content_digest,
                   a.revision, a.source_uri, a.title
              FROM knowledge_web_captures AS w
              JOIN knowledge_assets AS a ON a.id = w.asset_id
             WHERE w.id = ? AND w.parse_status = 'ready'
            """,
            (capture_id,),
        ).fetchone()
    return dict(row) if row is not None else None


async def _process(*, asset: dict[str, str], source_path: Path, temporary_catalog: Path, root: Path) -> dict[str, Any]:
    source_uri = str(asset["source_uri"])
    snapshot = LocalImmutableRawSnapshotRepository(
        snapshot_root=root / "raw",
        snapshot_id=str(asset["asset_id"]),
        source_revision=str(asset["revision"]),
        source_uri=source_uri,
        path=source_path,
        expected_digest=str(asset["content_digest"]),
    )
    publisher = LocalCapturePublishingService(root=root, space_id=str(asset["space_id"]))
    worker = CaptureProcessingWorker(
        snapshots=snapshot,
        publisher=publisher,
        jobs=SqliteCaptureProcessingJobStore(database_path=temporary_catalog, space_id=str(asset["space_id"])),
    )
    request = CaptureProcessingRequest(
        asset_id=str(asset["asset_id"]),
        source_revision=str(asset["revision"]),
        source_uri=source_uri,
        content_digest=str(asset["content_digest"]),
        idempotency_key=f"phase7-local-capture-{asset['capture_id']}-{asset['content_digest'].removeprefix('sha256:')[:24]}",
    )
    first = await worker.process(request)
    second = await worker.process(request)
    if first != second or publisher.publish_count != 1:
        raise RuntimeError("Capture Processing idempotency rehearsal failed")
    lint = publisher.lint(first.resource_uri)
    if lint.get("ok") is not True:
        raise RuntimeError("Capture publication lint failed")
    output_path = root / "captures" / f"{asset['asset_id']}.md"
    output_digest = _digest(output_path)
    return {
        "resource_uri": first.resource_uri,
        "asset_id": first.asset_id,
        "source_revision": first.source_revision,
        "source": {"content_digest": asset["content_digest"], "bytes": source_path.stat().st_size},
        "output": {"path_digest": _path_digest(output_path), "content_digest": output_digest, "bytes": output_path.stat().st_size},
        "publication_lint": lint,
        "publish_count": publisher.publish_count,
        "second_matches_first": first == second,
    }


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR, capture_id: str = "", source_path: Path | None = None) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-capture-processing-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_CAPTURE_SHADOW_REJECTED_NO_EXPLICIT_SOURCE_BINDING",
        "catalog_path_digest": _path_digest(catalog),
        "capture_id": capture_id or None,
        "source_path_digest": _path_digest(source_path) if source_path else None,
        "process": None,
        "canonical_catalog_unchanged": None,
    }
    try:
        if not capture_id or source_path is None:
            return _write_report(output_dir, result)
        before_catalog_digest = _digest(catalog)
        asset = _asset(catalog, capture_id)
        if asset is None:
            result["status"] = "PHASE7_CAPTURE_SHADOW_REJECTED_CAPTURE_NOT_READY"
            return _write_report(output_dir, result)
        source_digest = _digest(source_path)
        if source_digest != asset["content_digest"]:
            result["status"] = "PHASE7_CAPTURE_SHADOW_REJECTED_DIGEST_MISMATCH"
            return _write_report(output_dir, result)
        result["source_path_digest"] = _path_digest(source_path)
        with tempfile.TemporaryDirectory(prefix="phase7-capture-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog, temporary_catalog)
            result["process"] = asyncio.run(
                _process(
                    asset=asset,
                    source_path=source_path.expanduser().absolute(),
                    temporary_catalog=temporary_catalog,
                    root=Path(temp_dir).resolve(),
                )
            )
        result["canonical_catalog_unchanged"] = _digest(catalog) == before_catalog_digest
        if not result["canonical_catalog_unchanged"]:
            raise RuntimeError("canonical Catalog changed during Capture Processing shadow")
        result["status"] = "PHASE7_CAPTURE_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TypeError, ValueError, LookupError, RuntimeError, sqlite3.Error):
        result["status"] = "PHASE7_CAPTURE_SHADOW_FAILED"
        result["error"] = "phase7 local Capture Processing shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-capture-processing-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--capture-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    result = run_shadow(output_dir=args.output_dir, capture_id=args.capture_id, source_path=args.file)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
