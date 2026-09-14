"""Pure, bounded handling for document attachment metadata during distribution.

This module deliberately does not inspect the filesystem.  The caller supplies
the facts established by its verifier and this module only validates and
rewrites the three attachment selectors owned by the document contract.
"""

from __future__ import annotations

import copy
import json
import posixpath
import re
from collections.abc import Iterable, Mapping
from typing import Any


class DocumentAttachmentMetadataError(ValueError):
    """The attachment metadata or verification facts are not trustworthy."""


_SHA256 = re.compile(r"^[0-9a-fA-F]{64}$")


def _metadata(row: Any) -> Mapping[str, Any]:
    value = row.get("metadata_json", row) if isinstance(row, Mapping) else row
    if value is None:
        return {}
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except (TypeError, ValueError) as exc:
            raise DocumentAttachmentMetadataError("metadata_json must be an object") from exc
    if not isinstance(value, Mapping):
        raise DocumentAttachmentMetadataError("metadata_json must be an object")
    return value


def _path(value: Any, *, kind: str, selector: str) -> str:
    if not isinstance(value, str):
        raise DocumentAttachmentMetadataError(f"{selector} must be an absolute path string")
    if not value or value == "<redacted>" or "\\" in value or "\x00" in value:
        raise DocumentAttachmentMetadataError(f"{selector} contains an unsupported path")
    if not value.startswith("/") or value.startswith("//") or value == "/":
        raise DocumentAttachmentMetadataError(f"{selector} must be absolute")
    parts = value.split("/")
    if ".." in parts or posixpath.normpath(value) != value:
        raise DocumentAttachmentMetadataError(f"{selector} must be canonical")
    if kind not in {"file", "directory"}:
        raise DocumentAttachmentMetadataError("unknown attachment kind")
    return value


def _selectors(metadata: Mapping[str, Any]) -> Iterable[tuple[str, str, str]]:
    """Yield (selector, value, kind), validating known containers strictly."""
    if "assets" in metadata:
        assets = metadata["assets"]
        if not isinstance(assets, list):
            raise DocumentAttachmentMetadataError("metadata_json.assets must be a list")
        for index, asset in enumerate(assets):
            if not isinstance(asset, Mapping):
                raise DocumentAttachmentMetadataError("metadata_json.assets entries must be objects")
            if "path" in asset and asset["path"] is not None and asset["path"] != "":
                selector = f"metadata_json.assets[{index}].path"
                yield selector, _path(asset["path"], kind="file", selector=selector), "file"
    if "original_path" in metadata and metadata["original_path"] not in (None, ""):
        selector = "metadata_json.original_path"
        yield selector, _path(metadata["original_path"], kind="file", selector=selector), "file"
    if "multimodal" in metadata:
        multimodal = metadata["multimodal"]
        if not isinstance(multimodal, Mapping):
            raise DocumentAttachmentMetadataError("metadata_json.multimodal must be an object")
        if "image_assets_dir" in multimodal and multimodal["image_assets_dir"] not in (None, ""):
            selector = "metadata_json.multimodal.image_assets_dir"
            yield selector, _path(multimodal["image_assets_dir"], kind="directory", selector=selector), "directory"


def collect_attachment_references(assets: Iterable[Any]) -> dict[str, str]:
    """Collect verified attachment selectors from asset rows.

    Empty or null optional selector values are absent.  Every other known
    value is strict, and a path occurring once as both a file and directory is
    rejected rather than silently choosing a type.
    """
    result: dict[str, str] = {}
    for row in assets:
        for _selector, reference, kind in _selectors(_metadata(row)):
            previous = result.get(reference)
            if previous is not None and previous != kind:
                raise DocumentAttachmentMetadataError("attachment reference has conflicting kinds")
            result[reference] = kind
    return dict(sorted(result.items()))


def _set_selector(metadata: dict[str, Any], selector: str, value: str) -> None:
    if selector == "metadata_json.original_path":
        metadata["original_path"] = value
    elif selector == "metadata_json.multimodal.image_assets_dir":
        metadata.setdefault("multimodal", {})["image_assets_dir"] = value
    else:
        index = int(selector.split("[")[1].split("]")[0])
        metadata["assets"][index]["path"] = value


def _replace_metadata(metadata, bindings, *, require_all):
    result = copy.deepcopy(dict(metadata))
    changes = []
    for selector, old, kind in _selectors(metadata):
        binding = bindings.get(old)
        if binding is None:
            if require_all:
                raise DocumentAttachmentMetadataError('Current attachment lacks verified binding')
            continue
        if binding['kind'] != kind:
            raise DocumentAttachmentMetadataError('Attachment binding kind differs')
        _set_selector(result, selector, binding['output_path'])
        changes.append({'selector': selector, 'kind': kind, 'changed': old != binding['output_path']})
    if require_all:
        multimodal = metadata.get('multimodal', {})
        directory = multimodal.get('image_assets_dir') if isinstance(multimodal, Mapping) else None
        if directory not in (None, ''):
            output = bindings[directory]['output_path']
            prefix = directory.rstrip('/') + '/'
            for asset in metadata.get('assets', []):
                original = asset.get('path')
                if original in (None, ''):
                    continue
                if not original.startswith(prefix):
                    raise DocumentAttachmentMetadataError('Image path is outside declared directory')
                expected = output.rstrip('/') + '/' + original[len(prefix):]
                if bindings[original]['output_path'] != expected:
                    raise DocumentAttachmentMetadataError('Image and directory bindings disagree')
    return result, changes


def _validate_bindings(verified_refs: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    bindings: dict[str, dict[str, Any]] = {}
    for old, raw in verified_refs.items():
        kind = raw.get("kind") if isinstance(raw, Mapping) else None
        output = raw.get("output_path") if isinstance(raw, Mapping) else None
        if not isinstance(old, str) or not isinstance(raw, Mapping):
            raise DocumentAttachmentMetadataError("verified attachment binding is malformed")
        old = _path(old, kind=kind, selector="verified_refs")
        output = _path(output, kind=kind, selector="verified_refs.output_path")
        if kind == "file":
            if "size_bytes" not in raw or type(raw["size_bytes"]) is not int or raw["size_bytes"] < 0:
                raise DocumentAttachmentMetadataError("verified file size is invalid")
            if not isinstance(raw.get("sha256"), str) or not _SHA256.fullmatch(raw["sha256"]):
                raise DocumentAttachmentMetadataError("verified file sha256 is invalid")
        elif kind == "directory":
            if not isinstance(raw.get("inventory_digest"), str) or not _SHA256.fullmatch(raw["inventory_digest"]):
                raise DocumentAttachmentMetadataError("verified directory inventory digest is required")
        else:
            raise DocumentAttachmentMetadataError("verified attachment kind is invalid")
        bindings[old] = dict(raw, output_path=output, kind=kind)
    return bindings


def rebind_attachment_metadata(
    prepared_legacy: Mapping[str, Any],
    normalized_after: Mapping[str, Any],
    identities: Mapping[str, str] | None,
    verified_refs: Mapping[str, Any],
    *, native_assets: Iterable[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebind only verified attachment fields and return a non-sensitive receipt."""
    bindings = _validate_bindings(verified_refs)
    if not isinstance(identities, Mapping) or len(set(identities.values())) != len(identities):
        raise DocumentAttachmentMetadataError('Invalid document identity mapping')
    original_assets = list(native_assets)
    actual_ids = {row['id'] for row in original_assets}
    if len(actual_ids) != len(original_assets) or actual_ids != set(identities):
        raise DocumentAttachmentMetadataError('Native attachment identities differ from current Catalog')
    prepared = copy.deepcopy(dict(prepared_legacy))
    normalized = copy.deepcopy(dict(normalized_after))
    documents = {row['id']: row for row in prepared['knowledge_documents']}
    assets = {row['metadata_json']['legacy_document_id']: row for row in normalized['knowledge_assets']}
    if len(assets) != len(normalized['knowledge_assets']) or set(assets) != set(identities.values()):
        raise DocumentAttachmentMetadataError('Attachment document identities differ')
    references = collect_attachment_references(normalized['knowledge_assets'])
    if set(references) != set(bindings):
        raise DocumentAttachmentMetadataError('Verified attachment coverage differs')
    result = {}
    for native, legacy_id in identities.items():
        source_metadata = documents[legacy_id].get('doc_metadata') or {}
        current_metadata = assets[legacy_id]['metadata_json']
        # Source metadata may correctly describe older attachment bytes. Only
        # current claims must equal the newly verified snapshot facts.
        _validate_claimed_facts(current_metadata, bindings)
        updated_source, _ = _replace_metadata(source_metadata, bindings, require_all=False)
        updated_current, changed = _replace_metadata(current_metadata, bindings, require_all=True)
        documents[legacy_id]['doc_metadata'] = updated_source
        assets[legacy_id]['metadata_json'] = updated_current
        result[native] = {'legacy_document_id': legacy_id, 'selectors': changed,
                          'activation_allowed': False}
    receipt = {'format': 'knowledge-document-attachment-rebind/v1',
               'verified_reference_count': len(bindings), 'documents': result,
               'raw_paths_included': False}
    return prepared, normalized, receipt


def _validate_claimed_facts(metadata: Mapping[str, Any], bindings: Mapping[str, Mapping[str, Any]]) -> None:
    """Reject user claims that disagree with verifier facts; never repair them."""
    assets = metadata.get("assets", [])
    if isinstance(assets, list):
        for index, asset in enumerate(assets):
            if not isinstance(asset, Mapping) or asset.get("path") in (None, ""):
                continue
            path = _path(asset["path"], kind="file", selector=f"metadata_json.assets[{index}].path")
            binding = bindings.get(path)
            if binding is None:
                continue
            if 'size_bytes' in asset and (type(asset['size_bytes']) is not int or asset['size_bytes'] != binding['size_bytes']):
                raise DocumentAttachmentMetadataError('Attachment size disagrees with verified facts')
            for field in ('sha256', 'digest', 'content_digest'):
                if field in asset:
                    value = asset[field]
                    if not isinstance(value, str) or not _SHA256.fullmatch(value.removeprefix('sha256:')) or value.removeprefix('sha256:').lower() != binding['sha256'].lower():
                        raise DocumentAttachmentMetadataError('Attachment digest disagrees with verified facts')
    original = metadata.get("original_path")
    if original not in (None, ""):
        binding = bindings.get(_path(original, kind="file", selector="metadata_json.original_path"))
        if binding is not None and "original_sha256" in metadata and (not isinstance(metadata["original_sha256"], str) or not _SHA256.fullmatch(metadata["original_sha256"].removeprefix("sha256:")) or metadata["original_sha256"].removeprefix("sha256:").lower() != binding["sha256"].lower()):
            raise DocumentAttachmentMetadataError("metadata_json.original_sha256 disagrees with verified facts")
