"""Replay Catalog activation on temporary local copies only."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.activation import CatalogActivationController
from knowledge_platform.catalog.activation_sqlite import SqliteCatalogActivationStore

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def run_shadow(*, catalog: Path = _DEFAULT_CATALOG, output_dir: Path = _DEFAULT_OUTPUT_DIR) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-catalog-activation-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_CATALOG_ACTIVATION_SHADOW_FAILED",
        "source_unchanged": False,
        "states": {},
        "rollback": None,
    }
    try:
        catalog = catalog.expanduser().absolute()
        source_before = _digest(catalog)
        with tempfile.TemporaryDirectory(prefix="phase8-catalog-activation-") as temp_dir:
            temp_root = Path(temp_dir)
            source_copy = temp_root / "source.sqlite3"
            target_copy = temp_root / "platform.sqlite3"
            shutil.copy2(catalog, source_copy)
            shutil.copy2(catalog, target_copy)
            controller = CatalogActivationController(
                store=SqliteCatalogActivationStore(temp_root / "activation.sqlite3")
            )
            source_digest = _digest(source_copy)
            target_digest = _digest(target_copy)
            controller.prepare(
                installation_id="phase8-local",
                source_revision="legacy-local-v1",
                target_revision="platform-local-v1",
                source_digest=source_digest,
                target_digest=target_digest,
            )
            controller.mark_drained(proof_digest="sha256:" + hashlib.sha256(b"local-drain-proof").hexdigest())
            controller.verify(
                source_before_digest=source_digest,
                source_after_digest=_digest(source_copy),
                target_digest=_digest(target_copy),
                expected_target_digest=target_digest,
                checks={"source_unchanged": True, "target_digest_matches": True, "catalog_integrity": True},
            )
            activated = controller.activate(deployment_revision="platform-local-v1")
            result["states"]["activated"] = activated.status
            rollback = controller.rollback(reason="controlled local activation failure")
            result["rollback"] = {
                "status": rollback.status,
                "active_revision": rollback.active_revision,
                "source_read_only": rollback.source_read_only,
                "target_read_only": rollback.target_read_only,
            }
            restarted = CatalogActivationController(
                store=SqliteCatalogActivationStore(temp_root / "activation.sqlite3")
            )
            result["states"]["restarted"] = restarted.state.status
            result["source_unchanged"] = source_before == _digest(catalog)
        if (
            result["states"] == {"activated": "activated", "restarted": "rolled_back"}
            and result["rollback"] == {
                "status": "rolled_back",
                "active_revision": "legacy-local-v1",
                "source_read_only": False,
                "target_read_only": True,
            }
            and result["source_unchanged"]
        ):
            result["status"] = "PHASE8_LOCAL_CATALOG_ACTIVATION_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, RuntimeError, ValueError, TypeError):
        result["error_type"] = "local_catalog_activation_rehearsal_failed"
    report_path = output_dir / "phase8-local-catalog-activation-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(catalog=args.catalog, output_dir=args.output_dir)
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
