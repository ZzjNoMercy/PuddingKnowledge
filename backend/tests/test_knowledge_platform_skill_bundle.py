from __future__ import annotations

import json
from pathlib import Path

from knowledge_platform.transport import McpQueryAdapter

_ROOT = Path(__file__).resolve().parents[2]
_BUNDLE = _ROOT / "packages/knowledge-platform-skills"
_FORBIDDEN = (
    "llamaindex_knowledge_query",
    "analytics_model_id",
    "/knowledge",
    "PuddingClaw",
    "Claw Session",
    "local filesystem path",
)


def test_skill_bundle_manifest_is_portable_and_explicit() -> None:
    manifest = json.loads((_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["protocol"] == "REST-or-MCP-public-contract"
    skills = manifest["skills"]
    assert all(item["status"] in {"ready", "pending-platform-api"} for item in skills)
    ready = [item for item in skills if item["status"] == "ready"]
    assert ready and all(item["path"] for item in ready)
    assert all(item["operations"] for item in ready)
    assert all(item["path"] is None and item["operations"] == [] for item in skills if item["status"] == "pending-platform-api")


def test_ready_skill_bundle_does_not_reference_host_or_legacy_protocol() -> None:
    manifest = json.loads((_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["skills"]:
        if item["status"] != "ready":
            continue
        path = _BUNDLE / str(item["path"])
        assert path.is_file()
        content = path.read_text(encoding="utf-8")
        assert not any(token in content for token in _FORBIDDEN)
        assert all(operation in content for operation in item["operations"])


def test_pending_skill_migrations_are_not_accidentally_installable() -> None:
    manifest = json.loads((_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    for item in manifest["skills"]:
        if item["status"] != "pending-platform-api":
            continue
        assert item["path"] is None
        assert item["operations"] == []


def test_query_skill_operations_match_the_public_mcp_descriptor() -> None:
    manifest = json.loads((_BUNDLE / "manifest.json").read_text(encoding="utf-8"))
    declared = {
        operation
        for item in manifest["skills"]
        if item["status"] == "ready" and str(item["id"]).endswith("query")
        for operation in item["operations"]
    }
    public_tools = {str(item["name"]) for item in McpQueryAdapter.tool_descriptors()}
    assert declared <= public_tools
