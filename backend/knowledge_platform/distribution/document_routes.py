"""Pure document route rebinding using caller verified filesystem facts.

This module never inspects the filesystem.  Verification facts come from the
caller; model supplied fields cannot become route authority here.
"""
from __future__ import annotations
import copy
import posixpath
from collections.abc import Mapping, Set
from pathlib import PurePosixPath
from typing import Any

class DocumentRouteError(ValueError):
    """Invalid document route inputs or verification facts."""

_SELECTORS = (
    "knowledge_documents.virtual_path",
    "knowledge_assets.metadata_json.legacy_virtual_path",
    "knowledge_assets.metadata_json.assets[*].virtual_path",
    "knowledge_assets.metadata_json.multimodal.text_artifact",
    "knowledge_assets.metadata_json.multimodal.image_assets_virtual_prefix",
)

def _absolute(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DocumentRouteError(f"{label} must be a canonical POSIX path")
    if not value.startswith("/") or value.startswith("//") or value == "/":
        raise DocumentRouteError(f"{label} must be an absolute non-root path")
    if ".." in value.split("/") or posixpath.normpath(value) != value:
        raise DocumentRouteError(f"{label} must be canonical")
    return value

def _relative(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise DocumentRouteError(f"{label} must be a relative POSIX path")
    if value in ("", ".") or value.startswith("/") or ".." in value.split("/") or posixpath.normpath(value) != value:
        raise DocumentRouteError(f"{label} must be canonical and relative")
    return value

def _verified(values: Any, label: str) -> set[str]:
    if isinstance(values, (str, bytes)) or not isinstance(values, Set):
        raise DocumentRouteError(f"{label} must be a set-like collection")
    return {_relative(item, label) for item in values}

def _relative_to(path: str, root: str, label: str) -> str:
    try:
        value = PurePosixPath(path).relative_to(PurePosixPath(root)).as_posix()
    except ValueError as exc:
        raise DocumentRouteError(f"{label} is outside knowledge_root") from exc
    return _relative(value, label)

def _body_route(path: Any, root: str, files: set[str], label: str) -> str:
    relative = _relative_to(_absolute(path, label), root, label)
    if relative not in files:
        raise DocumentRouteError(f"{label} lacks a verified file")
    return "/knowledge/" + relative

def _metadata(value: Any, label: str) -> dict[str, Any]:
    if value is None: return {}
    if not isinstance(value, Mapping): raise DocumentRouteError(f"{label} must be an object")
    return copy.deepcopy(dict(value))

def _maps(prepared: Mapping[str, Any], normalized: Mapping[str, Any], identities: Mapping[str, str]):
    try: source_rows, asset_rows = prepared["knowledge_documents"], normalized["knowledge_assets"]
    except (KeyError, TypeError) as exc: raise DocumentRouteError("required rows are missing") from exc
    if not isinstance(source_rows, list) or not isinstance(asset_rows, list): raise DocumentRouteError("rows must be lists")
    if not isinstance(identities, Mapping) or len(identities) != len(set(identities)) or len(identities) != len(set(identities.values())):
        raise DocumentRouteError("identities must be a unique native-to-legacy mapping")
    source = {row.get("id"): row for row in source_rows if isinstance(row, Mapping)}
    if len(source) != len(source_rows): raise DocumentRouteError("prepared identities are invalid")
    assets = {}
    for row in asset_rows:
        metadata = row.get("metadata_json") if isinstance(row, Mapping) else None
        legacy = metadata.get("legacy_document_id") if isinstance(metadata, Mapping) else None
        if not isinstance(legacy, str) or legacy in assets: raise DocumentRouteError("asset legacy_document_id is invalid")
        assets[legacy] = row
    if set(identities.values()) != set(assets) or not set(identities.values()) <= set(source):
        raise DocumentRouteError("document identities do not match rows")
    return source, assets

def _rewrite_metadata(metadata: dict[str, Any], *, root: str, files: set[str], directories: set[str], strict: bool, body_route: str, label: str) -> dict[str, Any]:
    result = copy.deepcopy(metadata)
    raw_assets = metadata.get("assets", [])
    if raw_assets is not None and not isinstance(raw_assets, list): raise DocumentRouteError(f"{label}.assets must be a list")
    for index, item in enumerate(raw_assets or []):
        if not isinstance(item, Mapping): raise DocumentRouteError(f"{label}.assets entries must be objects")
        if item.get("virtual_path") in (None, ""): continue
        try: route = _body_route(item.get("path"), root, files, f"{label}.assets[{index}].path")
        except DocumentRouteError:
            if strict: raise
            continue
        result["assets"][index]["virtual_path"] = route
    multimodal = metadata.get("multimodal")
    if multimodal is not None and not isinstance(multimodal, Mapping): raise DocumentRouteError(f"{label}.multimodal must be an object")
    if isinstance(multimodal, Mapping):
        mm = result.setdefault("multimodal", {})
        if multimodal.get("text_artifact") not in (None, ""): mm["text_artifact"] = body_route
        prefix, image_dir = multimodal.get("image_assets_virtual_prefix"), multimodal.get("image_assets_dir")
        if prefix not in (None, ""):
            try:
                relative = _relative_to(_absolute(image_dir, f"{label}.multimodal.image_assets_dir"), root, f"{label}.multimodal.image_assets_dir")
                if relative not in directories: raise DocumentRouteError(f"{label}.multimodal.image_assets_dir lacks a verified directory")
            except DocumentRouteError:
                if strict: raise
            else: mm["image_assets_virtual_prefix"] = "/knowledge/" + relative
    return result

def rebind_document_routes(prepared_legacy: Mapping[str, Any], normalized_after: Mapping[str, Any], identities: Mapping[str, str], *, knowledge_root: str, verified_files: Set[str], verified_directories: Set[str]) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    """Rebind current routes from verified physical paths and return inactive evidence.

    Current metadata is strict.  Unverified old source attachment paths are
    retained so a later inverse projection can delete them safely.
    """
    root = _absolute(knowledge_root, "knowledge_root")
    files, directories = _verified(verified_files, "verified_files"), _verified(verified_directories, "verified_directories")
    prepared, normalized = copy.deepcopy(dict(prepared_legacy)), copy.deepcopy(dict(normalized_after))
    source, assets = _maps(prepared, normalized, identities)
    source_out = {row["id"]: row for row in prepared["knowledge_documents"]}
    asset_out = {row["metadata_json"]["legacy_document_id"]: row for row in normalized["knowledge_assets"]}
    for _native, legacy in identities.items():
        source_row = source_out[legacy]
        body_route = _body_route(source_row.get("storage_path"), root, files, f"document {legacy}.storage_path")
        source_row["virtual_path"] = body_route
        source_row["doc_metadata"] = _rewrite_metadata(_metadata(source_row.get("doc_metadata"), f"document {legacy}.doc_metadata"), root=root, files=files, directories=directories, strict=False, body_route=body_route, label=f"document {legacy}.doc_metadata")
        asset = asset_out[legacy]
        metadata = _metadata(asset.get("metadata_json"), f"asset {legacy}.metadata_json")
        metadata["legacy_virtual_path"] = body_route
        asset["metadata_json"] = _rewrite_metadata(metadata, root=root, files=files, directories=directories, strict=True, body_route=body_route, label=f"asset {legacy}.metadata_json")
    receipt = {"format": "knowledge-document-routes/v1", "native_ids": sorted(identities), "legacy_ids": sorted(identities.values()), "selectors": list(_SELECTORS), "counts": {"documents": len(identities), "assets": len(identities), "selectors": len(_SELECTORS)}, "activation_allowed": False}
    return prepared, normalized, receipt
