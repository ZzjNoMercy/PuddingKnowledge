"""Pure planning step for reversing document bodies into the legacy catalog.

This module deliberately does not read files.  A caller which has verified a
body supplies its immutable facts (path, digest and size); publication and
file security remain the materializer's responsibility.
"""

from __future__ import annotations

import copy
import hashlib
import re
from typing import Any, Mapping

from .core_catalog_reverse import _canonical_asset, _projection, _same, _verify_projection
from ..catalog.rehearsal_runner import _digest


_HEX = re.compile(r"^[0-9a-fA-F]{64}$")
_PROVENANCE = {
    "legacy_document_id", "legacy_status", "legacy_virtual_path",
    "legacy_source_type", "source_reference_digest", "origin_url_digest",
}


def _asset_id(document_id: str, revision: str) -> str:
    return "asset_" + document_id + "_" + hashlib.sha256(revision.encode("utf-8")).hexdigest()[:12]


def _safe_document_id(native_id: str, used: set[str]) -> str:
    # A hash avoids leaking arbitrary native identifiers and is portable in
    # every legacy schema.  The longer suffix also makes accidental collisions
    # practically impossible while remaining comfortably below common widths.
    base = "reverse-" + hashlib.sha256(native_id.encode("utf-8")).hexdigest()[:48]
    candidate = base
    counter = 1
    while candidate in used:
        candidate = base[: 63 - len(str(counter))] + "-" + str(counter)
        counter += 1
    return candidate


def _binding(asset_id: str, bindings: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    value = bindings.get(asset_id)
    if not isinstance(value, Mapping):
        raise ValueError(f"Missing verified body binding for {asset_id}")
    digest = value.get("sha256")
    if not isinstance(digest, str) or not _HEX.fullmatch(digest):
        raise ValueError(f"Invalid verified body digest for {asset_id}")
    path = value.get("storage_path")
    if not isinstance(path, str) or not path or not path.startswith("/"):
        raise ValueError(f"Invalid verified body storage path for {asset_id}")
    size = value.get("size_bytes")
    if type(size) is not int or size < 0:
        raise ValueError(f"Invalid verified body size for {asset_id}")
    return {"storage_path": path, "sha256": digest.lower(), "size_bytes": size}


def prepare_document_reverse(
    legacy: Mapping[str, list[Mapping[str, Any]]],
    target_before: Mapping[str, list[Mapping[str, Any]]],
    target_after: Mapping[str, list[Mapping[str, Any]]],
    source_revision: str,
    verified_body_bindings: Mapping[str, Mapping[str, Any]],
) -> tuple[dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, list[dict[str, Any]]], dict[str, str]]:
    """Return inputs suitable for :func:`core_catalog_reverse._inverse`.

    ``identity_map`` maps every native asset id in ``target_after`` to the
    stable legacy document id.  The planner verifies the original forward
    projection before making body relocation changes.
    """
    if not isinstance(source_revision, str) or not source_revision:
        raise ValueError("Invalid source revision")
    original = {k: [dict(row) for row in v] for k, v in legacy.items()}
    before = {k: [dict(row) for row in v] for k, v in target_before.items()}
    after = {k: [dict(row) for row in v] for k, v in target_after.items()}
    _verify_projection(original, before, source_revision)
    prepared = copy.deepcopy(original)
    old_docs = {str(row["id"]): row for row in prepared["knowledge_documents"]}
    old_assets = {str(row["id"]): row for row in before["knowledge_assets"]}
    old_by_native = {str(asset["id"]): asset for asset in before["knowledge_assets"]}
    used = set(old_docs)
    identity: dict[str, str] = {}

    # Apply only body facts explicitly committed by the caller.
    for native, asset in old_by_native.items():
        metadata = asset.get("metadata_json") or {}
        document_id = str(metadata.get("legacy_document_id") or "")
        if document_id not in old_docs:
            continue
        old_canonical = _canonical_asset(old_docs[document_id], source_revision=source_revision,
                                         space_id="space_" + str(old_docs[document_id]["knowledge_base_id"]))
        if not _same(metadata, old_canonical["metadata_json"]):
            raise ValueError("Tampered old document provenance")
        identity[native] = document_id
        if native in verified_body_bindings:
            binding = _binding(native, verified_body_bindings)
            row = old_docs[document_id]
            row["content_sha256"] = binding["sha256"]
            row["size_bytes"] = binding["size_bytes"]
            row["storage_path"] = binding["storage_path"]

    assets_after = after.get("knowledge_assets", [])
    for asset in assets_after:
        native = str(asset.get("id"))
        if native in identity:
            continue
        # A new native document needs an explicit body and a portable legacy id.
        if str(asset.get("kind")) != "document":
            raise ValueError("Unsupported new asset kind")
        binding = _binding(native, verified_body_bindings)
        document_id = _safe_document_id(native, used)
        used.add(document_id)
        identity[native] = document_id
        space_id = str(asset.get("space_id") or "")
        if not space_id.startswith("space_"):
            raise ValueError("New document requires a portable parent space")
        base = {
            "id": document_id, "knowledge_base_id": space_id[6:],
            "title": asset.get("title"), "mime_type": asset.get("mime_type"),
            "source_type": asset.get("source_type"),
            "source_path": binding["storage_path"], "storage_path": binding["storage_path"],
            "virtual_path": "/knowledge/imported/reverse/" + document_id,
            "status": "ready", "content_sha256": binding["sha256"],
            "size_bytes": binding["size_bytes"], "doc_metadata": {},
            "origin_url": "", "publish_targets": [],
            "created_at": asset.get("created_at"), "updated_at": asset.get("updated_at"),
            "source_connection_id": None, "source_item_id": None, "source_revision": None,
        }
        if not isinstance(base["title"], str) or not isinstance(base["mime_type"], str) or not isinstance(base["source_type"], str):
            raise ValueError("New document has invalid identity fields")
        if asset.get("description") not in (None, "") or asset.get("permissions_json") not in (None, {}):
            raise ValueError("Unsupported new document fields")
        prepared["knowledge_documents"].append(base)

    # Add newly introduced spaces to the legacy side before deriving the
    # baseline.  _inverse must see the same parent identities as the prepared
    # legacy rows.
    existing_spaces = {str(row["id"]) for row in prepared["knowledge_bases"]}
    for space in after.get("knowledge_spaces", []):
        sid = str(space.get("id") or "")
        if sid.startswith("space_") and sid[6:] not in existing_spaces:
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,63}", sid[6:]):
                raise ValueError("New space requires a portable legacy identity")
            prepared["knowledge_bases"].append({"id": sid[6:], "name": space.get("name"),
                "description": space.get("description"), "created_at": space.get("created_at"),
                "updated_at": space.get("updated_at")})
            existing_spaces.add(sid[6:])
    # Native ids, URIs and dataset manifests are generated fields.  Keep all
    # user metadata and reject provenance edits rather than silently replacing.
    normalized = copy.deepcopy(after)
    canonical_by_id = {
        _asset_id(str(row["id"]), source_revision): _canonical_asset(
            row, source_revision=source_revision,
            space_id="space_" + str(row["knowledge_base_id"]),
        ) for row in prepared["knowledge_documents"]
    }
    for asset in normalized.get("knowledge_assets", []):
        native = str(asset["id"])
        if native not in identity:
            raise ValueError("Unmapped target document identity")
        legacy_id = identity[native]
        expected_native_uri = f"knowledge://spaces/{asset['space_id']}/assets/{native}"
        if asset.get("source_uri") != expected_native_uri:
            raise ValueError("Tampered native document URI")
        asset["id"] = _asset_id(legacy_id, source_revision)
        asset["source_uri"] = f"knowledge://spaces/{asset['space_id']}/assets/{asset['id']}"
        expected = canonical_by_id[asset["id"]]
        expected_metadata = expected["metadata_json"]
        actual_metadata = asset.get("metadata_json")
        if not isinstance(actual_metadata, dict):
            raise ValueError("Invalid document metadata")
        old_metadata = old_assets[native].get("metadata_json") if native in old_assets else None
        for key in _PROVENANCE:
            actual = actual_metadata.get(key)
            expected_value = expected_metadata.get(key)
            if native in old_assets:
                old_value = old_metadata.get(key) if isinstance(old_metadata, dict) else None
                if actual != old_value:
                    if key == "legacy_status":
                        old_docs[legacy_id]["status"] = actual
                    elif key == "legacy_virtual_path":
                        old_docs[legacy_id]["virtual_path"] = actual
                    elif key == "legacy_source_type":
                        old_docs[legacy_id]["source_type"] = actual
                    else:
                        raise ValueError("Tampered old document provenance")
            elif key not in actual_metadata:
                # New document provenance is generated by this planner.
                pass
            if key not in actual_metadata or (native in verified_body_bindings and key == "source_reference_digest"):
                actual_metadata[key] = expected_value
        digest = str(expected["content_digest"])
        if asset.get("content_digest") != digest or asset.get("revision") != digest:
            raise ValueError("Verified body digest does not match target asset")
    for dataset in normalized.get("knowledge_datasets", []):
        native_ids = [str(value) for value in dataset.get("asset_ids", [])]
        expected_manifest = {"asset_ids": native_ids,
                             "source_revision": source_revision,
                             "version": dataset.get("version")}
        if dataset.get("manifest_digest") != _digest(expected_manifest):
            raise ValueError("Tampered dataset manifest")
        dataset["asset_ids"] = [_asset_id(identity[str(value)], source_revision)
                                 if str(value) in identity else str(value)
                                 for value in dataset.get("asset_ids", [])]
        dataset["manifest_digest"] = _digest({"asset_ids": dataset["asset_ids"],
                                               "source_revision": source_revision,
                                               "version": dataset.get("version")})
    prepared_before = _projection(prepared, source_revision)
    current_ids = {row['id'] for row in normalized['knowledge_assets']}
    current_identity = {native: legacy_id for native, legacy_id in identity.items()
                        if _asset_id(legacy_id, source_revision) in current_ids}
    return prepared, prepared_before, normalized, current_identity


# Name used by some callers during the staged migration.
plan_document_reverse = prepare_document_reverse
