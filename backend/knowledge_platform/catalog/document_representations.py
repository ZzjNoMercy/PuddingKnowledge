"""Pure legacy document representation checks for converted PDF assets.

The catalog stores the source PDF and a derived Markdown body separately.  A
representation is exposed only when the catalog row proves that those two
objects are bound together.  This module deliberately does not read either
path: these are declarations; migration must independently verify both files.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any


_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-fA-F]{64})$")


def _normalized_sha256(value: Any) -> str | None:
    """Return a bare lowercase SHA-256 digest, accepting the local prefix."""

    if not isinstance(value, str):
        return None
    match = _SHA256_RE.fullmatch(value)
    return match.group(1).lower() if match else None


def legacy_document_representation(row: Mapping[str, Any]) -> dict[str, str] | None:
    """Return the bound Markdown/PDF representation for one legacy row.

    Ordinary documents return ``None``.  Rows identifying a PDF conversion
    raise ``ValueError`` unless every field in the conversion contract is
    present and internally consistent.  No filesystem access or model-supplied
    validation marker is used.
    """

    if not isinstance(row, Mapping):
        return None

    source_type = row.get("source_type")
    doc_metadata = row.get("doc_metadata")
    is_pdf_conversion = (
        isinstance(source_type, str)
        and source_type.startswith("pdf_")
    ) or (
        isinstance(doc_metadata, Mapping)
        and doc_metadata.get("mode") == "multimodal_pdf"
    )
    if not is_pdf_conversion:
        return None

    if source_type is None or not isinstance(source_type, str) or not source_type.startswith("pdf_"):
        raise ValueError("PDF representation requires source_type pdf_*")
    if row.get("mime_type") != "text/markdown":
        raise ValueError("PDF representation requires mime_type text/markdown")
    if not isinstance(doc_metadata, Mapping) or doc_metadata.get("mode") != "multimodal_pdf":
        raise ValueError("PDF representation requires doc_metadata.mode multimodal_pdf")

    original_path = doc_metadata.get("original_path")
    source_path = row.get("source_path")
    storage_path = row.get("storage_path")
    if not isinstance(original_path, str) or not original_path:
        raise ValueError("PDF representation requires non-empty doc_metadata.original_path")
    if source_path != original_path:
        raise ValueError("PDF representation requires source_path to equal original_path")
    if not isinstance(storage_path, str) or not storage_path or storage_path == original_path:
        raise ValueError("PDF representation requires distinct non-empty storage_path")

    content_sha256 = _normalized_sha256(row.get("content_sha256"))
    original_sha256 = _normalized_sha256(doc_metadata.get("original_sha256"))
    markdown_sha256 = _normalized_sha256(doc_metadata.get("markdown_sha256"))
    if content_sha256 is None or original_sha256 is None or markdown_sha256 is None:
        raise ValueError("PDF representation requires valid SHA-256 digests")
    if content_sha256 != original_sha256:
        raise ValueError("PDF representation content_sha256 does not match original_sha256")

    return {
        "body_sha256": markdown_sha256,
        "original_sha256": original_sha256,
        "original_path": original_path,
    }
