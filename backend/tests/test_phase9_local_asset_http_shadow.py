from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

from scripts.phase9_local_asset_http_shadow import run_shadow


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _write_catalog(path: Path, digest: str) -> None:
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
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "asset_fixture",
                "space_kb_default",
                "document",
                "Fixture",
                "",
                "application/octet-stream",
                "local",
                "knowledge://spaces/space_kb_default/assets/asset_fixture",
                digest,
                digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()


def _write_manifest(path: Path, candidate: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-restore-review/v1",
                "status": "REVIEW_REQUIRED",
                "activation": "not-activated",
                "items": [
                    {
                        "review_id": "sha256:" + "1" * 64,
                        "decision": "human-review-required-content-matched-single-candidate",
                        "candidate_verification": {
                            "candidate_count": 1,
                            "content_digest_confirmed_candidate_count": 1,
                            "status": "unique-content-digest-confirmed-review-required",
                        },
                        "candidates": [{"path": str(candidate), "sha256": digest}],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def test_http_and_mcp_reads_use_explicit_binding_and_preserve_canonical_catalog(tmp_path: Path) -> None:
    content = b"HTTP shadow fixture"
    candidate = tmp_path / "fixture.bin"
    candidate.write_bytes(content)
    digest = _digest(content)
    catalog = tmp_path / "catalog.sqlite3"
    _write_catalog(catalog, digest)
    manifest = tmp_path / "review.json"
    _write_manifest(manifest, candidate, digest)
    before = catalog.read_bytes()

    report = run_shadow(
        review_manifest_path=manifest,
        catalog_path=catalog,
        output_path=tmp_path / "report.json",
    )

    assert report["status"] == "PHASE9_LOCAL_ASSET_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["activation"] == "not-activated"
    assert report["summary"]["verified_asset_count"] == 1
    assert report["assets"][0]["rest_portable_verified"] is True
    assert report["assets"][0]["mcp_portable_verified"] is True
    assert catalog.read_bytes() == before
    serialized = (tmp_path / "report.json").read_text(encoding="utf-8")
    assert str(candidate) not in serialized
    assert "/Users/" not in serialized
