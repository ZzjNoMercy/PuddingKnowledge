from __future__ import annotations

from pathlib import Path

import pytest

from scripts.phase8_local_platform_process_server import _merge_explicit_asset_bindings
from scripts.phase9_local_asset_manifest_process_shadow import _mcp_result_is_bound


def test_process_server_merges_explicit_bindings_without_replacing_existing_path(tmp_path: Path) -> None:
    wiki = tmp_path / "wiki.md"
    wiki.write_text("wiki", encoding="utf-8")
    document = tmp_path / "document.md"
    document.write_text("document", encoding="utf-8")

    merged = _merge_explicit_asset_bindings(
        {"asset_wiki": wiki},
        {"asset_document": document, "asset_wiki": wiki},
    )

    assert merged == {"asset_wiki": wiki.absolute(), "asset_document": document.absolute()}


def test_process_server_rejects_conflicting_explicit_binding(tmp_path: Path) -> None:
    first = tmp_path / "first.md"
    second = tmp_path / "second.md"
    first.write_text("first", encoding="utf-8")
    second.write_text("second", encoding="utf-8")

    with pytest.raises(ValueError, match="conflicting local path bindings"):
        _merge_explicit_asset_bindings({"asset_1": first}, {"asset_1": second})


def test_process_shadow_binds_mcp_structured_asset_digest() -> None:
    uri = "knowledge://spaces/space_kb_default/assets/asset_1"
    digest = "sha256:" + "a" * 64
    payload = {
        "result": {
            "structuredContent": {
                "status": "ok",
                "data": {"resource_uri": uri, "asset_digest": digest},
            },
            "contents": [{"uri": uri, "blob": "AQ=="}],
        }
    }

    assert _mcp_result_is_bound(payload, resource_uri=uri, digest=digest) is True
    assert _mcp_result_is_bound(payload, resource_uri=uri, digest="sha256:" + "b" * 64) is False
