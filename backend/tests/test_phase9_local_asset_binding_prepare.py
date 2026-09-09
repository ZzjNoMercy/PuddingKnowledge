from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from scripts.phase9_local_asset_binding_prepare import (
    LocalAssetBindingManifestError,
    build_binding_manifest,
    load_binding_manifest,
)
from scripts.phase9_local_asset_binding_review_queue import _portable_label, build_review_queue
from scripts.phase9_local_asset_manifest_http_shadow import run_shadow

REVIEW_ID = "sha256:" + "1" * 64
REVIEW_ID_2 = "sha256:" + "2" * 64


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
            """ ,
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


def _write_review_manifest(path: Path, candidate: Path, digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-restore-review/v1",
                "status": "REVIEW_REQUIRED",
                "activation": "not-activated",
                "items": [
                    {
                        "review_id": REVIEW_ID,
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


def _write_ambiguous_review_manifest(path: Path, candidates: list[Path], digest: str) -> None:
    path.write_text(
        json.dumps(
            {
                "format": "agent-knowledge-platform-restore-review/v1",
                "status": "REVIEW_REQUIRED",
                "activation": "not-activated",
                "items": [
                    {
                        "review_id": REVIEW_ID,
                        "decision": "human-review-required-select-one-candidate",
                        "candidate_verification": {
                            "candidate_count": len(candidates),
                            "content_digest_confirmed_candidate_count": len(candidates),
                            "status": "ambiguous-content-digest-confirmed-review-required",
                        },
                        "candidates": [{"path": str(candidate), "sha256": digest} for candidate in candidates],
                    }
                ],
            }
        )
        + "\n",
        encoding="utf-8",
    )


def _prepare(tmp_path: Path) -> tuple[Path, Path, Path, str]:
    content = b"approved local binding"
    candidate = tmp_path / "fixture.bin"
    candidate.write_bytes(content)
    digest = _digest(content)
    catalog = tmp_path / "catalog.sqlite3"
    _write_catalog(catalog, digest)
    review = tmp_path / "review.json"
    _write_review_manifest(review, candidate, digest)
    return catalog, review, candidate, digest


def test_prepare_requires_explicit_approval_and_load_revalidates_binding(tmp_path: Path) -> None:
    catalog, review, candidate, digest = _prepare(tmp_path)
    output = tmp_path / "bindings.json"

    result = build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
        approval_review_ids=[REVIEW_ID],
        approved_by="pet-local",
        approved_at="2026-09-05T00:00:00Z",
    )

    assert result["status"] == "APPROVED_HOST_BINDINGS"
    assert result["activation"] == "not-activated"
    assert result["execution_allowed"] is False
    assert result["persistence"] == "host-manifest-only"
    assert result["summary"]["binding_asset_count"] == 1
    assert result["bindings"][0]["content_digest"] == digest
    assert load_binding_manifest(manifest_path=output, catalog_path=catalog) == {
        "asset_fixture": candidate.absolute()
    }


def test_prepare_requires_explicit_selection_for_ambiguous_candidates(tmp_path: Path) -> None:
    content = b"ambiguous local binding"
    first = tmp_path / "first.bin"
    second = tmp_path / "second.bin"
    first.write_bytes(content)
    second.write_bytes(content)
    digest = _digest(content)
    catalog = tmp_path / "catalog.sqlite3"
    _write_catalog(catalog, digest)
    review = tmp_path / "review.json"
    _write_ambiguous_review_manifest(review, [first, second], digest)

    with pytest.raises(LocalAssetBindingManifestError, match="select-review-candidate"):
        build_binding_manifest(
            review_manifest_path=review,
            catalog_path=catalog,
            output_path=tmp_path / "without-selection.json",
            approval_review_ids=[REVIEW_ID],
        )

    output = tmp_path / "with-selection.json"
    build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
        approval_review_ids=[REVIEW_ID],
        selected_candidates={REVIEW_ID: 1},
    )
    assert load_binding_manifest(manifest_path=output, catalog_path=catalog) == {
        "asset_fixture": second.absolute()
    }


def test_prepare_rejects_duplicate_review_ids_and_candidate_paths(tmp_path: Path) -> None:
    catalog, review, candidate, digest = _prepare(tmp_path)
    document = json.loads(review.read_text(encoding="utf-8"))
    document["items"].append(json.loads(json.dumps(document["items"][0])))
    review.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(LocalAssetBindingManifestError, match="review ID is duplicated"):
        build_binding_manifest(
            review_manifest_path=review,
            catalog_path=catalog,
            output_path=tmp_path / "duplicate-review.json",
            approval_review_ids=[REVIEW_ID],
        )

    _write_ambiguous_review_manifest(review, [candidate, candidate], digest)
    with pytest.raises(LocalAssetBindingManifestError, match="candidate paths are duplicated"):
        build_binding_manifest(
            review_manifest_path=review,
            catalog_path=catalog,
            output_path=tmp_path / "duplicate-candidate.json",
            approval_review_ids=[REVIEW_ID],
            selected_candidates={REVIEW_ID: 0},
        )


def test_load_rejects_catalog_revision_drift(tmp_path: Path) -> None:
    catalog, review, _candidate, _digest_value = _prepare(tmp_path)
    output = tmp_path / "bindings.json"
    build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
        approval_review_ids=[REVIEW_ID],
    )

    connection = sqlite3.connect(catalog)
    try:
        connection.execute("UPDATE knowledge_assets SET title = ? WHERE id = ?", ("Changed", "asset_fixture"))
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LocalAssetBindingManifestError, match="Catalog revision changed"):
        load_binding_manifest(manifest_path=output, catalog_path=catalog)


def test_load_rejects_file_mutation_and_prepare_rejects_unknown_review_id(tmp_path: Path) -> None:
    catalog, review, candidate, _digest_value = _prepare(tmp_path)
    with pytest.raises(LocalAssetBindingManifestError, match="unknown or non-confirmed"):
        build_binding_manifest(
            review_manifest_path=review,
            catalog_path=catalog,
            output_path=tmp_path / "bindings.json",
            approval_review_ids=["sha256:" + "2" * 64],
        )

    output = tmp_path / "bindings.json"
    build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
        approval_review_ids=[REVIEW_ID],
    )
    candidate.write_bytes(b"mutated local binding")
    with pytest.raises(LocalAssetBindingManifestError, match="candidate evidence is no longer valid"):
        load_binding_manifest(manifest_path=output, catalog_path=catalog)


def test_review_queue_rejects_symlinked_review_manifest(tmp_path: Path) -> None:
    catalog, review, _candidate, _digest_value = _prepare(tmp_path)
    linked_review = tmp_path / "linked-review.json"
    linked_review.symlink_to(review)
    with pytest.raises(ValueError, match="must not be a symlink"):
        build_review_queue(
            review_manifest_path=linked_review,
            catalog_path=catalog,
            output_path=tmp_path / "queue.json",
        )


def test_load_rejects_approval_evidence_bound_to_another_asset(tmp_path: Path) -> None:
    catalog, review, _candidate, digest = _prepare(tmp_path)
    other_candidate = tmp_path / "other.bin"
    other_candidate.write_bytes(b"another approved local binding")
    other_digest = _digest(other_candidate.read_bytes())
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "asset_other",
                "space_kb_default",
                "document",
                "Other",
                "",
                "application/octet-stream",
                "local",
                "knowledge://spaces/space_kb_default/assets/asset_other",
                other_digest,
                other_digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()
    review_document = json.loads(review.read_text(encoding="utf-8"))
    review_document["items"].append(
        {
            "review_id": REVIEW_ID_2,
            "decision": "human-review-required-content-matched-single-candidate",
            "candidate_verification": {
                "candidate_count": 1,
                "content_digest_confirmed_candidate_count": 1,
                "status": "unique-content-digest-confirmed-review-required",
            },
            "candidates": [{"path": str(other_candidate), "sha256": other_digest}],
        }
    )
    review.write_text(json.dumps(review_document), encoding="utf-8")
    output = tmp_path / "bindings.json"
    build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
        approval_review_ids=[REVIEW_ID, REVIEW_ID_2],
    )
    document = json.loads(output.read_text(encoding="utf-8"))
    document["bindings"][0]["approval_review_ids"] = [REVIEW_ID_2]
    document["bindings"][1]["approval_review_ids"] = [REVIEW_ID]
    output.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(LocalAssetBindingManifestError, match="does not match the binding digest"):
        load_binding_manifest(manifest_path=output, catalog_path=catalog)


def test_prepare_rejects_digest_matching_multiple_catalog_assets(tmp_path: Path) -> None:
    catalog, review, _candidate, digest = _prepare(tmp_path)
    connection = sqlite3.connect(catalog)
    try:
        connection.execute(
            "INSERT INTO knowledge_assets VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "asset_duplicate",
                "space_kb_default",
                "document",
                "Duplicate",
                "",
                "application/octet-stream",
                "local",
                "knowledge://spaces/space_kb_default/assets/asset_duplicate",
                digest,
                digest,
            ),
        )
        connection.commit()
    finally:
        connection.close()

    with pytest.raises(LocalAssetBindingManifestError, match="multiple Catalog Assets"):
        build_binding_manifest(
            review_manifest_path=review,
            catalog_path=catalog,
            output_path=tmp_path / "bindings.json",
            approval_review_ids=[REVIEW_ID],
        )


def test_prepared_manifest_reaches_public_rest_and_mcp_edges_without_path_leak(tmp_path: Path) -> None:
    catalog, review, candidate, _digest_value = _prepare(tmp_path)
    binding_manifest = tmp_path / "bindings.json"
    build_binding_manifest(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=binding_manifest,
        approval_review_ids=[REVIEW_ID],
    )

    output = tmp_path / "http-shadow.json"
    report = run_shadow(
        binding_manifest_path=binding_manifest,
        catalog_path=catalog,
        output_path=output,
    )

    assert report["status"] == "PHASE9_LOCAL_ASSET_BINDING_HTTP_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["summary"]["verified_asset_count"] == 1
    assert report["catalog"]["canonical_unchanged"] is True
    serialized = output.read_text(encoding="utf-8")
    assert str(candidate) not in serialized
    assert "activation_allowed" not in serialized


def test_review_queue_is_path_free_and_does_not_approve_binding(tmp_path: Path) -> None:
    catalog, review, candidate, digest = _prepare(tmp_path)
    output = tmp_path / "approval-queue.json"

    report = build_review_queue(
        review_manifest_path=review,
        catalog_path=catalog,
        output_path=output,
    )

    assert report["status"] == "PHASE9_LOCAL_ASSET_BINDING_REVIEW_QUEUE_READY_NOT_ACTIVATABLE"
    assert report["execution_allowed"] is False
    assert report["summary"]["approval_required"] is True
    assert report["items"][0]["content_digest"] == digest
    assert report["items"][0]["catalog_asset_ids"] == ["asset_fixture"]
    assert report["items"][0]["catalog_assets"][0]["title"] == "Fixture"
    serialized = output.read_text(encoding="utf-8")
    assert str(candidate) not in serialized
    assert "APPROVED_HOST_BINDINGS" not in serialized


def test_review_queue_redacts_windows_style_path_labels() -> None:
    assert _portable_label(r"C:\\Users\\pet\\Documents\\fixture.xlsx") == "<redacted-label>"
    assert _portable_label(r"\\server\\share\\fixture.xlsx") == "<redacted-label>"
