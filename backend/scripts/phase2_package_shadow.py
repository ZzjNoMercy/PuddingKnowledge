"""Export a local Package only from a staged Catalog and explicit bindings."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.package import LegacyVirtualMountPackageSource, PackageBuildError, PackageExportService
from scripts.phase1_catalog_query_shadow import _stage_binding
from scripts.phase9_local_asset_binding_prepare import load_binding_manifest

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase0b-local-catalog")


def _package_failure_kind(error: PackageBuildError) -> str:
    message = str(error)
    if message.startswith("package export rejected; missing asset files"):
        return "incomplete_package_inputs"
    return "invalid_package_inputs"


def run_package_shadow(
    *,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    approved_bindings_path: Path | None = None,
    normalize_legacy_metadata: bool = False,
) -> dict[str, Any]:
    requested_output_dir = output_dir.expanduser()
    if requested_output_dir.is_symlink():
        raise ValueError("package shadow output directory must not be a symlink")
    output_dir = requested_output_dir.resolve()
    # A shadow must leave a machine-readable report even when its input
    # staging directory does not exist yet.  Creating only the requested
    # report directory is safe; the Catalog/stage files themselves remain
    # entirely caller-owned and are never synthesized here.
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise ValueError("package shadow output directory must not be a symlink")
    database_path = output_dir / "knowledge-platform.sqlite3"
    stage_bound, stage_error, stage_digest = _stage_binding(output_dir, database_path)
    package_output = output_dir / "phase2-package-should-not-exist"
    output_preexisting = package_output.exists() or package_output.is_symlink()
    repository = None
    collections: list[dict[str, Any]] = []
    assets: list[dict[str, Any]] = []
    catalog_revision = ""
    package_error = ""
    failure_kind = ""
    binding_asset_count = 0
    binding_validated = False
    metadata_projection: dict[str, Any] = {
        "requested": normalize_legacy_metadata,
        "applied": False,
        "rule": "legacy_virtual_knowledge_mount_to_portable_label",
        "normalized_field_count": 0,
        "normalized_record_count": 0,
        "execution_allowed": False,
        "activation_allowed": False,
    }
    projection_source: LegacyVirtualMountPackageSource | None = None
    if not stage_bound:
        package_error = stage_error or "staged Catalog is not bound to its provenance report"
        failure_kind = "stage_binding_failure"
    elif output_preexisting:
        package_error = "shadow output path already exists"
        failure_kind = "preexisting_shadow_output"
    else:
        try:
            repository = SqliteCatalogQueryRepository(database_path)
            collections = repository.list_collections()
            assets = repository.list_assets()
            catalog_revision = repository.catalog_revision
            asset_files = {}
            if approved_bindings_path is not None:
                asset_files = load_binding_manifest(
                    manifest_path=approved_bindings_path,
                    catalog_path=database_path,
                )
                binding_asset_count = len(asset_files)
                binding_validated = True
            package_source = repository
            if normalize_legacy_metadata:
                projection_source = LegacyVirtualMountPackageSource(repository)
                package_source = projection_source
            PackageExportService(package_source).export_snapshot(
                output_dir=package_output,
                package_id="local-staged-catalog",
                version="staged",
                # Only an explicitly approved, revalidated host manifest may
                # provide physical files; legacy metadata is never inferred.
                asset_files=asset_files,
                capabilities=["knowledge_list", "knowledge_search", "knowledge_read"],
            )
            if projection_source is not None:
                metadata_projection = projection_source.report
        except PackageBuildError as error:
            package_error = str(error)
            failure_kind = _package_failure_kind(error)
            if projection_source is not None:
                metadata_projection = projection_source.report
        except (OSError, sqlite3.Error, TypeError, ValueError) as error:
            package_error = str(error)
            failure_kind = "catalog_read_or_validation_failure"
            if projection_source is not None:
                metadata_projection = projection_source.report
    result = {
        "format": "agent-knowledge-platform-package-shadow/v1",
        "status": (
            "PHASE2_PACKAGE_EXPORT_PASS_NOT_ACTIVATABLE"
            if failure_kind == "" and package_output.exists()
            else
            "PHASE2_PACKAGE_EXPORT_REJECTED_INCOMPLETE"
            if failure_kind == "incomplete_package_inputs"
            else
            "PHASE2_PACKAGE_EXPORT_REJECTED_INVALID"
            if failure_kind == "invalid_package_inputs"
            else "PHASE2_PACKAGE_SHADOW_FAILED"
        ),
        "activation": "not-activated",
        "execution_allowed": False,
        "activation_allowed": False,
        "scope": (
            "staged local Catalog + explicitly approved host bindings; no automatic physical reference inference"
            if approved_bindings_path is not None
            else "staged local Catalog; no physical reference inference"
        ),
        "stage_report_digest": stage_digest,
        "stage_binding": {"bound": stage_bound, "error": stage_error},
        "binding_manifest": {
            "provided": approved_bindings_path is not None,
            "validated": binding_validated,
            "asset_count": binding_asset_count,
        },
        "metadata_projection": metadata_projection,
        "catalog": {
            "catalog_revision": catalog_revision,
            "collection_count": len(collections),
            "asset_count": len(assets),
        },
        "export": {
            "output_created": not output_preexisting and package_output.exists(),
            "output_preexisting": output_preexisting,
            "rejected_reason": package_error,
            "failure_kind": failure_kind,
        },
    }
    report_path = output_dir / "package-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--bindings", type=Path, help="explicitly approved host binding manifest")
    parser.add_argument(
        "--normalize-legacy-metadata",
        action="store_true",
        help="explicitly project the known legacy /knowledge/ description marker to portable text",
    )
    args = parser.parse_args()
    result = run_package_shadow(
        output_dir=args.output_dir,
        approved_bindings_path=args.bindings,
        normalize_legacy_metadata=args.normalize_legacy_metadata,
    )
    print(
        json.dumps(
            {
                "status": result["status"],
                "collection_count": result["catalog"]["collection_count"],
                "asset_count": result["catalog"]["asset_count"],
                "output_created": result["export"]["output_created"],
                "report": str(args.output_dir / "package-shadow-report.json"),
            },
            ensure_ascii=False,
        )
    )
    return 0 if result["status"] in {
        "PHASE2_PACKAGE_EXPORT_REJECTED_INCOMPLETE",
        "PHASE2_PACKAGE_EXPORT_REJECTED_INVALID",
        "PHASE2_PACKAGE_EXPORT_PASS_NOT_ACTIVATABLE",
    } else 1


if __name__ == "__main__":
    raise SystemExit(main())
