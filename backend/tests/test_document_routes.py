import copy

import pytest

from knowledge_platform.distribution.document_routes import DocumentRouteError, rebind_document_routes


ROOT = "/vault"


def rows():
    prepared = {"knowledge_documents": [{
        "id": "legacy-1", "virtual_path": "old", "storage_path": "/vault/docs/body.md",
        "doc_metadata": {"assets": [{"path": "/old/removed.png"}], "secret": "keep"},
    }]}
    normalized = {
        "knowledge_assets": [{"id": "asset-1", "metadata_json": {
            "legacy_document_id": "legacy-1", "legacy_virtual_path": "stale",
            "assets": [{"path": "/vault/images/cover.png", "virtual_path": "old"}],
            "multimodal": {
                "text_artifact": "/vault/docs/body.md",
                "image_assets_virtual_prefix": "old-prefix",
                "image_assets_dir": "/vault/images",
            },
        }}],
    }
    return prepared, normalized


def test_rebinds_current_and_own_verified_source_selectors_without_mutating_inputs():
    prepared, normalized = rows()
    before = (copy.deepcopy(prepared), copy.deepcopy(normalized))
    result_prepared, result_normalized, receipt = rebind_document_routes(
        prepared, normalized, {"native-1": "legacy-1"},
        knowledge_root=ROOT,
        verified_files={"docs/body.md", "images/cover.png"},
        verified_directories={"images"},
    )
    assert (prepared, normalized) == before
    metadata = result_normalized["knowledge_assets"][0]["metadata_json"]
    assert metadata["legacy_virtual_path"] == "/knowledge/docs/body.md"
    assert metadata["assets"][0]["path"] == "/vault/images/cover.png"
    assert metadata["assets"][0]["virtual_path"] == "/knowledge/images/cover.png"
    assert metadata["multimodal"]["text_artifact"] == "/knowledge/docs/body.md"
    assert metadata["multimodal"]["image_assets_virtual_prefix"] == "/knowledge/images"
    assert result_prepared["knowledge_documents"][0]["virtual_path"] == "/knowledge/docs/body.md"
    assert result_prepared["knowledge_documents"][0]["doc_metadata"]["assets"][0]["path"] == "/old/removed.png"
    assert result_prepared["knowledge_documents"][0]["doc_metadata"]["secret"] == "keep"
    assert set(receipt) == {"format", "native_ids", "legacy_ids", "selectors", "counts", "activation_allowed"}
    assert receipt["activation_allowed"] is False
    assert "/vault" not in str(receipt) and "cover.png" not in str(receipt)


@pytest.mark.parametrize("bad", ["/vault/../x.md", "/vault\\docs\\body.md", "/vault/docs/../body.md", "/vault/docs\x00body.md"])
def test_rejects_noncanonical_current_storage_path(bad):
    prepared, normalized = rows()
    prepared["knowledge_documents"][0]["storage_path"] = bad
    with pytest.raises(DocumentRouteError):
        rebind_document_routes(prepared, normalized, {"native-1": "legacy-1"}, knowledge_root=ROOT,
                               verified_files={"docs/body.md", "images/cover.png"}, verified_directories={"images"})


def test_current_attachment_requires_verified_file_but_deleted_source_attachment_is_allowed():
    prepared, normalized = rows()
    normalized["knowledge_assets"][0]["metadata_json"]["assets"][0]["path"] = "/vault/images/missing.png"
    with pytest.raises(DocumentRouteError, match="verified file"):
        rebind_document_routes(prepared, normalized, {"native-1": "legacy-1"}, knowledge_root=ROOT,
                               verified_files={"docs/body.md", "images/cover.png"}, verified_directories={"images"})


def test_rejects_duplicate_or_mismatched_identities():
    prepared, normalized = rows()
    with pytest.raises(DocumentRouteError):
        rebind_document_routes(prepared, normalized, {"native-1": "legacy-1", "native-2": "legacy-1"}, knowledge_root=ROOT,
                               verified_files={"docs/body.md", "images/cover.png"}, verified_directories={"images"})


def test_requires_image_directory_when_virtual_prefix_is_present():
    prepared, normalized = rows()
    with pytest.raises(DocumentRouteError, match="verified directory"):
        rebind_document_routes(prepared, normalized, {"native-1": "legacy-1"}, knowledge_root=ROOT,
                               verified_files={"docs/body.md", "images/cover.png"}, verified_directories=set())
