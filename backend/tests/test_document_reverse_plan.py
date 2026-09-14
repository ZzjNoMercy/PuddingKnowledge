import hashlib
import copy

import pytest

from knowledge_platform.distribution.core_catalog_reverse import _projection
from knowledge_platform.catalog.rehearsal_runner import _digest
from knowledge_platform.distribution.document_reverse_plan import prepare_document_reverse


REVISION = "legacy-1"


def _legacy():
    return {"knowledge_bases": [{"id": "kb-1", "name": "Docs", "description": "", "created_at": None, "updated_at": None}],
            "knowledge_documents": [{
                "id": "doc-1", "knowledge_base_id": "kb-1", "title": "Read me",
                "mime_type": "text/markdown", "source_type": "local",
                "source_path": "old/readme.md", "storage_path": "old/readme.md",
                "virtual_path": "readme.md", "status": "published", "content_sha256": "a" * 64,
                "doc_metadata": {}, "origin_url": "", "created_at": None, "updated_at": None,
            }]}


def test_verified_relocation_updates_only_body_fields_and_maps_ids():
    legacy = _legacy()
    before = _projection(legacy, REVISION)
    digest = hashlib.sha256(b"new").hexdigest()
    after = copy.deepcopy(before)
    after["knowledge_assets"][0]["content_digest"] = "sha256:" + digest
    after["knowledge_assets"][0]["revision"] = "sha256:" + digest
    prepared, _old, normalized, identity = prepare_document_reverse(
        legacy, before, after, REVISION,
        {after["knowledge_assets"][0]["id"]: {"storage_path": "/stage/new.md", "sha256": digest, "size_bytes": 3}},
    )
    assert prepared["knowledge_documents"][0]["storage_path"] == "/stage/new.md"
    assert prepared["knowledge_documents"][0]["size_bytes"] == 3
    assert identity[after["knowledge_assets"][0]["id"]] == "doc-1"
    assert normalized["knowledge_assets"][0]["source_uri"].endswith(normalized["knowledge_assets"][0]["id"])


def test_tampered_old_provenance_is_rejected():
    legacy = _legacy()
    before = _projection(legacy, REVISION)
    after = copy.deepcopy(before)
    after["knowledge_assets"][0]["metadata_json"]["legacy_document_id"] = "other"
    with pytest.raises(ValueError, match="provenance|projection"):
        prepare_document_reverse(legacy, before, after, REVISION, {})


def test_new_document_uses_stable_hashed_legacy_id():
    legacy = _legacy()
    before = _projection(legacy, REVISION)
    after = copy.deepcopy(before)
    native = "native-document-42"
    digest = hashlib.sha256(b"new").hexdigest()
    new = {"id": native, "space_id": "space_kb-1", "kind": "document", "title": "New",
           "description": "", "mime_type": "text/plain", "source_type": "upload",
           "source_uri": "knowledge://spaces/space_kb-1/assets/native-document-42", "revision": "sha256:" + digest,
           "content_digest": "sha256:" + digest, "permissions_json": {}, "metadata_json": {},
           "created_at": None, "updated_at": None}
    after["knowledge_assets"].append(new)
    after["knowledge_datasets"][0]["asset_ids"].append(native)
    dataset = after["knowledge_datasets"][0]
    dataset["manifest_digest"] = _digest({"asset_ids": dataset["asset_ids"], "source_revision": REVISION, "version": dataset["version"]})
    prepared, _old, _normalized, identity = prepare_document_reverse(
        legacy, before, after, REVISION,
        {native: {"storage_path": "/stage/new.txt", "sha256": digest, "size_bytes": 3}},
    )
    assert identity[native].startswith("reverse-")
    assert any(row["id"] == identity[native] and row["status"] == "ready" for row in prepared["knowledge_documents"])
