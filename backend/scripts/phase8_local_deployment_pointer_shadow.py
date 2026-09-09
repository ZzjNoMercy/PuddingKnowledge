"""Replay one complete deployment pointer on temporary local handles only."""

from __future__ import annotations

import argparse
import hashlib
import json
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any

from knowledge_platform.catalog.deployment import DeploymentActivationController
from knowledge_platform.catalog.deployment_sqlite import SqliteDeploymentActivationStore
from knowledge_platform.catalog.providers import (
    LocalProviderObservationError,
    deployment_manifest_from_provider_handles,
    observe_local_provider_handles,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_WIKI_ROOT = _ROOT / "docs"


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def run_shadow(
    *,
    catalog: Path = _DEFAULT_CATALOG,
    wiki_root: Path = _DEFAULT_WIKI_ROOT,
    vector_uri: str = "http://127.0.0.1:19530",
    text_collection: str = "puddingclaw_knowledge_text",
    image_collection: str = "puddingclaw_knowledge_image",
    vector_probe: Callable[..., bool] | None = None,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-deployment-pointer-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_DEPLOYMENT_POINTER_SHADOW_FAILED",
        "required_artifact_kinds": ["blob", "catalog", "vector_index", "wiki_root"],
        "pointer_is_single_revision": False,
        "legacy_unchanged": False,
        "provider_handles": [],
        "states": {},
        "rollback": None,
    }
    try:
        handles = observe_local_provider_handles(
            catalog_path=catalog,
            wiki_root=wiki_root,
            vector_uri=vector_uri,
            text_collection=text_collection,
            image_collection=image_collection,
            vector_probe=vector_probe,
        )
        result["provider_handles"] = [handle.to_dict() for handle in handles]
        try:
            legacy = deployment_manifest_from_provider_handles(
                deployment_revision="legacy-local-v1",
                handles=handles,
            )
            candidate = deployment_manifest_from_provider_handles(
                deployment_revision="platform-local-v2",
                handles=handles,
            )
        except LocalProviderObservationError:
            result["status"] = "PHASE8_LOCAL_DEPLOYMENT_POINTER_SHADOW_BLOCKED_PROVIDER"
            report_path = output_dir / "phase8-local-deployment-pointer-shadow-report.json"
            report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            result["report"] = str(report_path)
            return result
        catalog_before_digest = next(handle.locator_digest for handle in handles if handle.kind == "catalog")
        with tempfile.TemporaryDirectory(prefix="phase8-deployment-pointer-") as temp_dir:
            store_path = Path(temp_dir) / "deployment-activation.sqlite3"
            controller = DeploymentActivationController(store=SqliteDeploymentActivationStore(store_path))
            controller.prepare(
                installation_id="phase8-local",
                legacy_manifest=legacy,
                candidate_manifest=candidate,
            )
            controller.mark_drained(proof_digest=_digest_bytes(b"local-deployment-drain-proof"))
            controller.verify(
                legacy_before_digest=legacy.manifest_digest(),
                legacy_after_digest=legacy.manifest_digest(),
                candidate_manifest_digest=candidate.manifest_digest(),
                checks={"legacy_unchanged": True, "candidate_manifest_matches": True, "bundle_integrity": True},
            )
            activated = controller.activate(deployment_revision=candidate.deployment_revision)
            active = controller.read_context()
            result["states"]["activated"] = activated.status
            result["pointer_is_single_revision"] = (
                active.deployment_revision == candidate.deployment_revision
                and {artifact.revision for artifact in active.artifacts} == {candidate.deployment_revision}
            )
            rollback = controller.rollback(reason="controlled local deployment failure")
            result["rollback"] = {
                "status": rollback.status,
                "active_deployment_revision": rollback.active_deployment_revision,
                "legacy_read_only": rollback.legacy_read_only,
                "candidate_read_only": rollback.candidate_read_only,
            }
            restarted = DeploymentActivationController(store=SqliteDeploymentActivationStore(store_path))
            result["states"]["restarted"] = restarted.state.status
            result["legacy_unchanged"] = catalog_before_digest == _digest_bytes(catalog.read_bytes())
        if (
            result["states"] == {"activated": "activated", "restarted": "rolled_back"}
            and result["pointer_is_single_revision"]
            and result["legacy_unchanged"]
            and result["rollback"] == {
                "status": "rolled_back",
                "active_deployment_revision": "legacy-local-v1",
                "legacy_read_only": False,
                "candidate_read_only": True,
            }
        ):
            result["status"] = "PHASE8_LOCAL_DEPLOYMENT_POINTER_SHADOW_PASS_NOT_ACTIVATABLE"
    except (OSError, RuntimeError, ValueError, TypeError, json.JSONDecodeError):
        result["error_type"] = "local_deployment_pointer_rehearsal_failed"
    report_path = output_dir / "phase8-local-deployment-pointer-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--wiki-root", type=Path, default=_DEFAULT_WIKI_ROOT)
    parser.add_argument("--vector-uri", default="http://127.0.0.1:19530")
    parser.add_argument("--text-collection", default="puddingclaw_knowledge_text")
    parser.add_argument("--image-collection", default="puddingclaw_knowledge_image")
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(
        catalog=args.catalog,
        wiki_root=args.wiki_root,
        vector_uri=args.vector_uri,
        text_collection=args.text_collection,
        image_collection=args.image_collection,
        output_dir=args.output_dir,
    )
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
