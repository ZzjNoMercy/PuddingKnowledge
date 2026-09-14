from __future__ import annotations

import pytest

from knowledge_platform.distribution.document_attachment_metadata import (
    DocumentAttachmentMetadataError,
    collect_attachment_references,
    rebind_attachment_metadata,
)


def test_collects_known_file_and_directory_selectors() -> None:
    rows = [{"metadata_json": {"assets": [{"path": "/raw/a.png"}], "original_path": "/raw/a.pdf", "multimodal": {"image_assets_dir": "/raw/images"}}}]
    assert collect_attachment_references(rows) == {
        "/raw/a.pdf": "file",
        "/raw/a.png": "file",
        "/raw/images": "directory",
    }


@pytest.mark.parametrize("value", ["relative/a", "/raw/../a", r"/raw\\a", "/raw/\x00a", "<redacted>"])
def test_rejects_unsafe_paths(value: str) -> None:
    with pytest.raises(DocumentAttachmentMetadataError):
        collect_attachment_references([{"metadata_json": {"original_path": value}}])


def test_rejects_conflicting_reference_kinds() -> None:
    with pytest.raises(DocumentAttachmentMetadataError, match="conflicting"):
        collect_attachment_references([
            {"metadata_json": {"original_path": "/same"}},
            {"metadata_json": {"multimodal": {"image_assets_dir": "/same"}}},
        ])


def test_rebinds_verified_fields_and_preserves_unrelated_metadata() -> None:
    old = {"doc_metadata": {"title": "secret", "assets": [{"path": "/raw/a.png", "size_bytes": 3, "sha256": "a" * 64}], "original_path": "/raw/a.pdf"}}
    after = {"doc_metadata": {"title": "changed", "assets": [{"path": "/raw/a.png", "size_bytes": 3, "sha256": "a" * 64}], "original_path": "/raw/a.pdf"}}
    facts = {
        "/raw/a.png": {"kind": "file", "output_path": "/out/a.png", "size_bytes": 3, "sha256": "a" * 64},
        "/raw/a.pdf": {"kind": "file", "output_path": "/out/a.pdf", "size_bytes": 4, "sha256": "b" * 64},
    }
    legacy = {'knowledge_documents': [{'id':'legacy', **old}]}
    current = {'knowledge_assets':[{'metadata_json':{**after['doc_metadata'],'legacy_document_id':'legacy'}}]}
    prepared, normalized, receipt = rebind_attachment_metadata(legacy, current, {"native": "legacy"}, facts, native_assets=[{"id":"native"}])
    prepared = prepared['knowledge_documents'][0]
    normalized = {'doc_metadata': normalized['knowledge_assets'][0]['metadata_json']}
    assert prepared["doc_metadata"]["title"] == "secret"
    assert prepared["doc_metadata"]["assets"][0]["path"] == "/out/a.png"
    assert normalized["doc_metadata"]["original_path"] == "/out/a.pdf"
    assert receipt["raw_paths_included"] is False


def test_rejects_stale_claimed_hash() -> None:
    with pytest.raises(DocumentAttachmentMetadataError, match="disagrees"):
        rebind_attachment_metadata(
            {'knowledge_documents':[{'id':'legacy','doc_metadata':{}}]},
            {'knowledge_assets':[{'metadata_json':{'legacy_document_id':'legacy','assets':[{'path':'/raw/a.png','sha256':'a'*64}]}}]},
            {'native':'legacy'},
            {'/raw/a.png':{'kind':'file','output_path':'/out/a.png','sha256':'b'*64,'size_bytes':3}},
            native_assets=[{'id':'native'}],
        )


def test_receipt_native_identity_is_checked_against_actual_current_assets():
    with pytest.raises(DocumentAttachmentMetadataError,match='Native attachment identities'):
        rebind_attachment_metadata(
            {'knowledge_documents':[{'id':'legacy','doc_metadata':{}}]},
            {'knowledge_assets':[{'metadata_json':{'legacy_document_id':'legacy'}}]},
            {'fake-native':'legacy'}, {}, native_assets=[{'id':'actual-native'}])
