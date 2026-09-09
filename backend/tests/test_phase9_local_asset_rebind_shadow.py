from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
from pathlib import Path

import pytest

from scripts.phase9_local_asset_rebind_shadow import build_shadow_report, main


def _digest(content: bytes) -> str:
    return "sha256:" + hashlib.sha256(content).hexdigest()


def _catalog(path: Path, *, asset_id: str, digest: str) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE knowledge_assets (
                id TEXT PRIMARY KEY,
                space_id TEXT NOT NULL,
                kind TEXT NOT NULL,
                title TEXT NOT NULL,
                description TEXT,
                mime_type TEXT NOT NULL,
                source_type TEXT NOT NULL,
                source_uri TEXT NOT NULL,
                revision TEXT NOT NULL,
                content_digest TEXT NOT NULL
            )
            """
        )
        connection.execute(
            """
            INSERT INTO knowledge_assets
            (id, space_id, kind, title, description, mime_type, source_type, source_uri, revision, content_digest)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                asset_id,
                "space_kb_default",
                "document",
                "Local fixture",
                "",
                "application/octet-stream",
                "local",
                f"knowledge://spaces/space_kb_default/assets/{asset_id}",
                digest,
                digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _manifest(path: Path, *, candidate_path: Path, digest: str, include_ambiguous: bool = True) -> None:
    items = [
        {
            "review_id": "sha256:" + "1" * 64,
            "decision": "human-review-required-content-matched-single-candidate",
            "candidate_verification": {
                "candidate_count": 1,
                "content_digest_confirmed_candidate_count": 1,
                "status": "unique-content-digest-confirmed-review-required",
            },
            "candidates": [{"path": str(candidate_path), "sha256": digest}],
        }
    ]
    if include_ambiguous:
        items.append(
            {
                "review_id": "sha256:" + "2" * 64,
                "decision": "human-review-required-select-one-candidate",
                "candidate_verification": {
                    "candidate_count": 2,
                    "content_digest_confirmed_candidate_count": 2,
                    "status": "ambiguous-content-digest-confirmed",
                },
                "candidates": [],
            }
        )
    path.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-restore-review/v1",
                "status": "REVIEW_REQUIRED",
                "activation": "not-activated",
                "items": items,
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_shadow_verifies_explicit_binding_without_emitting_path_or_activation(tmp_path: Path) -> None:
    content = b"local fixture bytes"
    candidate = tmp_path / "fixture.bin"
    candidate.write_bytes(content)
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, asset_id="asset_fixture", digest=_digest(content))
    manifest = tmp_path / "review.json"
    _manifest(manifest, candidate_path=candidate, digest=_digest(content))
    output = tmp_path / "shadow.json"

    report = build_shadow_report(
        review_manifest_path=manifest,
        catalog_path=catalog,
        output_path=output,
        space_id="space_kb_default",
    )

    serialized = output.read_text(encoding="utf-8")
    assert report["status"] == "PHASE9_LOCAL_ASSET_REBIND_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["activation"] == "not-activated"
    assert report["summary"]["confirmed_candidate_item_count"] == 1
    assert report["summary"]["skipped_ambiguous_or_unresolved_item_count"] == 1
    assert report["summary"]["verified_item_count"] == 1
    assert report["assets"][0]["resource_uri"].startswith("knowledge://")
    assert str(candidate) not in serialized
    assert "activation_allowed" not in serialized


def test_shadow_reports_digest_mismatch_as_failed_and_stays_inactive(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    candidate = tmp_path / "fixture.bin"
    candidate.write_bytes(b"new bytes")
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, asset_id="asset_fixture", digest=_digest(b"old bytes"))
    manifest = tmp_path / "review.json"
    _manifest(manifest, candidate_path=candidate, digest=_digest(b"new bytes"), include_ambiguous=False)

    report = build_shadow_report(
        review_manifest_path=manifest,
        catalog_path=catalog,
        output_path=tmp_path / "shadow.json",
        space_id="space_kb_default",
    )

    assert report["status"] == "PHASE9_LOCAL_ASSET_REBIND_SHADOW_FAILED"
    assert report["activation"] == "not-activated"
    assert report["summary"]["failed_item_count"] == 1
    assert report["summary"]["catalog_asset_match_count"] == 0
    assert report["summary"]["failed_asset_count"] == 0
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "phase9_local_asset_rebind_shadow.py",
            "--review-manifest",
            str(manifest),
            "--catalog",
            str(catalog),
            "--output",
            str(tmp_path / "shadow-cli.json"),
        ],
    )
    assert main() == 1


def test_shadow_rejects_symlink_candidate_before_binding(tmp_path: Path) -> None:
    content = b"local fixture bytes"
    candidate = tmp_path / "fixture.bin"
    candidate.write_bytes(content)
    symlink = tmp_path / "fixture-link.bin"
    symlink.symlink_to(candidate)
    catalog = tmp_path / "catalog.sqlite3"
    _catalog(catalog, asset_id="asset_fixture", digest=_digest(content))
    manifest = tmp_path / "review.json"
    _manifest(manifest, candidate_path=symlink, digest=_digest(content), include_ambiguous=False)

    with pytest.raises(ValueError, match="candidate path contains a symlink"):
        build_shadow_report(
            review_manifest_path=manifest,
            catalog_path=catalog,
            output_path=tmp_path / "shadow.json",
            space_id="space_kb_default",
        )
