"""Replay legacy semantic discovery, prepare, publish, and registry loading."""

from __future__ import annotations

import hashlib
import json
import tempfile
from pathlib import Path
from typing import Any

from analytics.semantic_assets import get_semantic_asset_registry
from analytics.semantic_authoring.discovery import discover_semantic_definitions
from analytics.semantic_authoring.service import prepare_semantic_markdown, publish_semantic_markdown
from runtime_identity.paths import PuddingClawPaths

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/semantic_assets_and_authoring.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/semantic-assets-authoring/v1":
        raise ValueError("semantic authoring fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("scenario"), dict):
        raise ValueError("semantic authoring fixture must be explicitly sanitized")
    return document


def _count_files(root: Path) -> int:
    return sum(1 for path in root.rglob("*") if path.is_file()) if root.exists() else 0


def _digest_text(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def observe() -> dict[str, object]:
    fixture = _fixture()
    scenario = fixture["scenario"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-authoring-") as directory:
        paths = PuddingClawPaths(Path(directory))
        discovery = discover_semantic_definitions(
            query="measure:golden-measure",
            kinds=["measure"],
            session_id=scenario["session_id"],
            paths=paths,
        )
        plan = prepare_semantic_markdown(
            logical_path=scenario["logical_path"],
            candidate_markdown=scenario["candidate_markdown"],
            baseline_digest="absent",
            discovery_receipt_id=discovery["receipt_id"],
            session_id=scenario["session_id"],
            brief=scenario["brief"],
            paths=paths,
        )
        published = publish_semantic_markdown(
            plan_id=plan["plan_id"],
            plan_digest=plan["plan_digest"],
            session_id=scenario["session_id"],
            paths=paths,
        )
        registry = get_semantic_asset_registry(paths.user_definitions())
        catalog = registry.refresh()
        loaded = registry.get_asset("measure:golden-measure")
        definition_path = paths.user_definitions() / Path(*scenario["logical_path"].split("/"))
        state_files = _count_files(paths.state())
        published_file_count = _count_files(paths.user_definitions())
        published_content = definition_path.read_text(encoding="utf-8")

    return {
        "result": {
            "discovery_complete": discovery["complete"],
            "discovery_matches": discovery["match_count"],
            "plan_status": plan["status"],
            "plan_valid": plan["validation"]["valid"],
            "published_ok": published["ok"],
            "published_digest": published["published_digest"],
            "registry_asset_id": loaded["id"],
            "registry_catalog_count": catalog["count"],
        },
        "evidence": {
            "kind": plan["kind"],
            "candidate_digest": plan["candidate_digest"],
            "registry_definition_digest": _digest_text(published_content),
            "discovery_catalog_digest": discovery["catalog_digest"],
            "published_content_has_formatter": "formatter: semantic-asset" in published_content,
            "published_content_has_measure_type": "type: measure" in published_content,
        },
        "database_side_effects": {"business_rows_written": 0, "active_revision_probe": "not_applicable"},
        "filesystem_side_effects": {
            "business_writes": [scenario["logical_path"]],
            "published_definition_file_count": published_file_count,
            "authoring_state_file_count": state_files,
        },
        "provider_revision": "legacy-semantic-authoring-v1",
        "failure_semantics": "invalid_candidate->publish_rejected; baseline_changed->publish_rejected; valid_candidate->digest_bound_publish",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/semantic_assets_and_authoring.json"]},
    }


if __name__ == "__main__":
    print(json.dumps(observe(), ensure_ascii=False, indent=2, sort_keys=True))
