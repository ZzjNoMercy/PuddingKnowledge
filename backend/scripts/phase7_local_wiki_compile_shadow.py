"""Run a non-activating local Phase 7 Wiki compilation rehearsal.

The source Asset and file are both explicit inputs.  The staged Catalog is
read-only; compilation output is written to a temporary directory and the
report stores digests rather than physical paths or source contents.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.wiki import (
    BoundedWikiContextService,
    DeterministicWikiModelGateway,
    LocalImmutableRawSnapshotRepository,
    LocalWikiDraftValidator,
    LocalWikiPublishingService,
    SqliteWikiCompilationJobStore,
    WikiCompilationRequest,
    WikiCompilationWorker,
)

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _digest(path: Path) -> tuple[str | None, int | None]:
    if not path.is_file() or path.is_symlink():
        return None, None
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
    return f"sha256:{digest.hexdigest()}", size


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode("utf-8")).hexdigest()


async def _compile(
    *, asset: dict[str, Any], source_path: Path, output_root: Path, job_database: Path, raw_root: Path
) -> dict[str, Any]:
    asset_id = str(asset["id"])
    space_id = str(asset["space_id"])
    snapshot = LocalImmutableRawSnapshotRepository(
        snapshot_root=raw_root,
        snapshot_id=asset_id,
        source_revision=str(asset["revision"]),
        source_uri=str(asset["source_uri"]),
        path=source_path,
        expected_digest=str(asset["content_digest"]),
    )
    publisher = LocalWikiPublishingService(root=output_root, space_id=space_id)
    worker = WikiCompilationWorker(
        snapshots=snapshot,
        context=BoundedWikiContextService(),
        model=DeterministicWikiModelGateway({asset_id: str(asset.get("title") or "Compiled Wiki")}),
        validator=LocalWikiDraftValidator(),
        publisher=publisher,
        jobs=SqliteWikiCompilationJobStore(database_path=job_database, space_id=space_id),
    )
    request = WikiCompilationRequest(
        snapshot_id=asset_id,
        source_revision=str(asset["revision"]),
        source_uri=str(asset["source_uri"]),
        content_digest=str(asset["content_digest"]),
        idempotency_key=f"phase7-local-wiki-{asset_id}-{str(asset['content_digest']).removeprefix('sha256:')[:24]}",
    )
    first = await worker.compile(request)
    second = await worker.compile(request)
    if first != second or publisher.publish_count != 1:
        raise RuntimeError("Wiki compilation idempotency rehearsal failed")
    output_path = output_root / "wiki" / f"{asset_id}.md"
    output_digest, output_bytes = _digest(output_path)
    if output_digest is None:
        raise RuntimeError("Wiki compilation did not produce isolated output")
    lint = publisher.lint_published(first.resource_uri)
    if lint.get("ok") is not True:
        raise RuntimeError("Wiki publication lint did not confirm the published bytes")
    retirement = publisher.retire(first.resource_uri)
    already_retired = publisher.retire(first.resource_uri)
    if not retirement.get("retired") or not already_retired.get("already_retired"):
        raise RuntimeError("Wiki publication retirement was not idempotent")
    return {
        "first": {
            "resource_uri": first.resource_uri,
            "snapshot_id": first.snapshot_id,
            "source_revision": first.source_revision,
        },
        "second_matches_first": first == second,
        "published_file": {"path_digest": _path_digest(output_path), "content_digest": output_digest, "bytes": output_bytes},
        "publish_count": publisher.publish_count,
        "publication_lint": lint,
        "retirement": retirement,
        "retirement_retry": already_retired,
        "raw_snapshot": {
            "path_digest": _path_digest(raw_root / f"{asset_id}-{str(asset['content_digest']).removeprefix('sha256:')[:16]}.md"),
            "content_digest": str(asset["content_digest"]),
        },
    }


def run_shadow(*, output_dir: Path = _DEFAULT_OUTPUT_DIR, asset_id: str = "", source_path: Path | None = None) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    catalog_path = output_dir / "knowledge-platform.sqlite3"
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase7-local-wiki-compile-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE7_WIKI_SHADOW_REJECTED_NO_EXPLICIT_SOURCE_BINDING",
        "catalog_path_digest": _path_digest(catalog_path),
        "source": {"path_digest": _path_digest(source_path) if source_path else None, "content_digest": None, "bytes": None},
        "compile": None,
    }
    try:
        if not asset_id or source_path is None:
            return _write_report(output_dir, result)
        repository = SqliteCatalogQueryRepository(catalog_path)
        assets = {str(item.get("id") or ""): item for item in repository.list_assets()}
        asset = assets.get(asset_id)
        if not isinstance(asset, dict) or str(asset.get("kind") or "") != "document":
            result["status"] = "PHASE7_WIKI_SHADOW_REJECTED_ASSET_NOT_BINDABLE"
            return _write_report(output_dir, result)
        content_digest, size = _digest(source_path)
        result["source"].update({"content_digest": content_digest, "bytes": size})
        if content_digest != str(asset.get("content_digest") or ""):
            result["status"] = "PHASE7_WIKI_SHADOW_REJECTED_DIGEST_MISMATCH"
            return _write_report(output_dir, result)
        with tempfile.TemporaryDirectory(prefix="phase7-wiki-shadow-") as temp_dir:
            temporary_catalog = Path(temp_dir) / "knowledge-platform.sqlite3"
            shutil.copy2(catalog_path, temporary_catalog)
            compile_data = asyncio.run(
                _compile(
                    asset=asset,
                    source_path=source_path.expanduser().absolute(),
                    output_root=Path(temp_dir).resolve(),
                    job_database=temporary_catalog,
                    raw_root=Path(temp_dir).resolve() / "raw",
                )
            )
            result["compile"] = compile_data
        result["status"] = "PHASE7_WIKI_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, TypeError, ValueError, LookupError, RuntimeError):
        result["status"] = "PHASE7_WIKI_SHADOW_FAILED"
        result["error"] = "phase7 local Wiki shadow failed"
    return _write_report(output_dir, result)


def _write_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    report_path = output_dir / "phase7-local-wiki-compile-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", type=Path, required=True)
    args = parser.parse_args()
    result = run_shadow(output_dir=args.output_dir, asset_id=args.asset_id, source_path=args.file)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") or "REJECTED" in str(result["status"]) else 1


if __name__ == "__main__":
    raise SystemExit(main())
