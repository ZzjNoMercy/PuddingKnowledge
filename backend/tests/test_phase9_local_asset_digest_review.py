from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from scripts.phase9_local_asset_binding_prepare import build_binding_manifest, load_binding_manifest
from scripts.phase9_local_asset_digest_review import build_digest_review


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_catalog(path: Path, digest: str) -> None:
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE knowledge_assets (
            id TEXT, space_id TEXT, kind TEXT, title TEXT, description TEXT,
            mime_type TEXT, source_type TEXT, source_uri TEXT, revision TEXT, content_digest TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "asset-1",
            "space_kb_default",
            "document",
            "Fixture",
            "",
            "text/plain",
            "local",
            "knowledge://spaces/space_kb_default/assets/asset-1",
            "v1",
            digest,
        ),
    )
    connection.commit()
    connection.close()


def test_digest_review_finds_unique_and_duplicate_content_candidates(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    content = b"digest review fixture"
    (root / "one.md").write_bytes(content)
    catalog = tmp_path / "catalog.sqlite3"
    _write_catalog(catalog, _digest(content))
    output = tmp_path / "review.json"

    report = build_digest_review(catalog_path=catalog, root=root, output_path=output)

    assert report["summary"]["unique_candidate_asset_count"] == 1
    assert report["summary"]["ambiguous_candidate_asset_count"] == 0
    assert report["summary"]["unresolved_asset_count"] == 0
    assert report["items"][0]["decision"] == "human-review-required-content-matched-single-candidate"
    assert report["execution_allowed"] is False
    assert output.is_file()

    bindings = tmp_path / "bindings.json"
    build_binding_manifest(
        review_manifest_path=output,
        catalog_path=catalog,
        output_path=bindings,
        approval_review_ids=[report["items"][0]["review_id"]],
        approved_by="test-user",
    )
    assert load_binding_manifest(manifest_path=bindings, catalog_path=catalog) == {
        "asset-1": (root / "one.md").absolute()
    }

    (root / "two.md").write_bytes(content)
    second = build_digest_review(catalog_path=catalog, root=root, output_path=output)
    assert second["summary"]["ambiguous_candidate_asset_count"] == 1
    assert second["items"][0]["decision"] == "human-review-required-select-one-candidate"


def test_digest_review_skips_symlink_candidates_and_rejects_symlink_root(tmp_path: Path) -> None:
    root = tmp_path / "knowledge"
    root.mkdir()
    content = b"symlink fixture"
    source = tmp_path / "source.md"
    source.write_bytes(content)
    (root / "linked.md").symlink_to(source)
    catalog = tmp_path / "catalog.sqlite3"
    _write_catalog(catalog, _digest(content))

    report = build_digest_review(catalog_path=catalog, root=root, output_path=tmp_path / "review.json")
    assert report["summary"]["unresolved_asset_count"] == 1
    assert report["items"][0]["candidates"] == []

    linked_root = tmp_path / "linked-root"
    linked_root.symlink_to(root, target_is_directory=True)
    with pytest.raises(ValueError, match="non-symlink directory"):
        build_digest_review(catalog_path=catalog, root=linked_root, output_path=tmp_path / "other.json")
