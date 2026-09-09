"""Rebuild a portable Package into an inactive local Vanna Collection candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from pathlib import Path
from typing import Any

from knowledge_platform.database.vanna_rebuild import local_vanna_collection_candidate
from knowledge_platform.package import PackageValidationError, validate_package

_DEFAULT_OUTPUT_DIR = Path("artifacts/phase9-local-vanna")
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _collection_name(package_root: Path) -> str:
    manifest = json.loads((package_root / "package-manifest.json").read_text(encoding="utf-8"))
    package_id = str(manifest.get("id") or "package")
    safe_id = re.sub(r"[^A-Za-z0-9._-]+", "-", package_id).strip("-") or "package"
    return f"puddingknowledge_vanna_{safe_id}_{hashlib.sha256(str(manifest.get('package_revision', '')).encode()).hexdigest()[:16]}"


def run_vanna_collection_shadow(*, package_root: Path, output_dir: Path, collection_name: str | None = None) -> dict[str, Any]:
    package_root = package_root.expanduser().absolute()
    output_dir = output_dir.expanduser().absolute()
    if output_dir.exists() and output_dir.is_symlink():
        raise ValueError("Vanna shadow output directory must not be a symlink")
    output_dir.mkdir(parents=True, exist_ok=True)
    if output_dir.is_symlink():
        raise ValueError("Vanna shadow output directory must not be a symlink")
    report_path = output_dir / "vanna-collection-shadow-report.json"
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-vanna-collection-shadow/v1",
        "status": "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_FAILED",
        "activation": "not-activated",
        "activation_allowed": False,
        "provider_io_performed": False,
        "legacy_collection_read": False,
        "package": {"validated": False},
    }
    try:
        validate_package(package_root)
        selected_name = collection_name or _collection_name(package_root)
        if not _NAME_RE.fullmatch(selected_name):
            raise PackageValidationError("Vanna Collection name is invalid")
        result = local_vanna_collection_candidate(
            package_root=package_root,
            output_root=output_dir,
            collection_name=selected_name,
        )
        report.update(
            {
                "status": "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE",
                "package": {
                    "validated": True,
                    "package_revision": result.package_revision,
                    "source_count": result.source_count,
                },
                "collection": {
                    "name": selected_name,
                    "package_revision": result.package_revision,
                    "input_digest": result.input_digest,
                    "counts": dict(result.counts),
                    "active": False,
                    "activation_allowed": False,
                },
            }
        )
    except PackageValidationError:
        report["failure"] = "package_validation_failed"
    except (OSError, TypeError, ValueError, json.JSONDecodeError):
        report["failure"] = "local_collection_staging_failed"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    parser.add_argument("--collection-name")
    args = parser.parse_args()
    result = run_vanna_collection_shadow(
        package_root=args.package_root,
        output_dir=args.output_dir,
        collection_name=args.collection_name,
    )
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result["status"] == "PHASE9_LOCAL_VANNA_COLLECTION_REBUILD_PASS_NOT_ACTIVATABLE" else 2


if __name__ == "__main__":
    raise SystemExit(main())
