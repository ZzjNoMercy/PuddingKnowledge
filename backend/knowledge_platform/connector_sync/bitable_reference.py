"""Strict, network-free parsing and bounded resolution of Feishu Bitable links.

The resolver only discovers table metadata.  It never requests records and it
does not turn a URL into an authorization scope; callers must bind the
returned ``app_token`` to their configured connector.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit

from .feishu_source import FeishuSourceError, token


_TENANT_SUFFIXES = (".feishu.cn", ".larksuite.com")
_TENANT_ROOTS = {"feishu.cn", "larksuite.com"}
_QUERY_KEYS = {"table", "view"}
_VALUE_KEYS = {"records", "rows", "values", "row_values", "samples", "sample_values"}


@dataclass(frozen=True, slots=True)
class BitableReference:
    original_url: str
    entry_kind: str
    token: str
    app_token: str = ""
    table_id: str = ""
    view_id: str = ""
    node_token: str = ""


def _clean_token(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        raise FeishuSourceError(f"{label} is invalid")
    value = value.strip()
    if not value and not required:
        return ""
    try:
        return token(value)
    except FeishuSourceError as exc:
        raise FeishuSourceError(f"{label} is invalid") from exc


def _tenant(host: str) -> bool:
    host = host.lower().rstrip(".")
    return host in _TENANT_ROOTS or any(host.endswith(suffix) for suffix in _TENANT_SUFFIXES)


def parse_bitable_reference(url: str) -> BitableReference:
    """Parse exactly one official ``/base`` or ``/wiki`` Bitable URL."""

    if not isinstance(url, str) or not url or len(url) > 2_000:
        raise FeishuSourceError("Bitable URL is invalid")
    if any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in url):
        raise FeishuSourceError("Bitable URL contains control characters")
    try:
        parsed = urlsplit(url)
        host = parsed.hostname or ""
        port = parsed.port
    except ValueError as exc:
        raise FeishuSourceError("Bitable URL is invalid") from exc
    if parsed.scheme.lower() != "https" or parsed.username or parsed.password or not _tenant(host):
        raise FeishuSourceError("Bitable URL must use an official HTTPS tenant")
    if port not in (None, 443) or parsed.fragment:
        raise FeishuSourceError("Bitable URL has an invalid port or fragment")
    parts = parsed.path.split("/")
    if len(parts) != 3 or parts[0] or parts[2] == "" or parts[1] not in {"base", "wiki"}:
        raise FeishuSourceError("Bitable URL path must be exactly /base/<token> or /wiki/<token>")
    kind, locator = parts[1], _clean_token(parts[2], "Bitable token")
    try:
        pairs = parse_qsl(parsed.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as exc:
        raise FeishuSourceError("Bitable URL query is invalid") from exc
    query: dict[str, str] = {}
    for key, value in pairs:
        if key not in _QUERY_KEYS or key in query or not value:
            raise FeishuSourceError("Bitable URL query may contain one non-empty table/view parameter")
        query[key] = _clean_token(value, f"Bitable {key}")
    return BitableReference(
        original_url=url,
        entry_kind="direct_bitable" if kind == "base" else "wiki_bitable",
        token=locator,
        app_token=locator if kind == "base" else "",
        table_id=query.get("table", ""),
        view_id=query.get("view", ""),
        node_token=locator if kind == "wiki" else "",
    )


def _metadata_table(table: Mapping[str, Any]) -> dict[str, Any]:
    return {str(key): value for key, value in table.items() if str(key).lower() not in _VALUE_KEYS}


async def resolve_bitable_reference(api: Any, url: str) -> tuple[BitableReference, list[dict[str, Any]]]:
    """Resolve a link through ``api`` and return reference plus table metadata.

    ``api`` must expose ``get_node(node_token=...)`` and
    ``list_bitable_tables(app_token=...)``.  The caller remains responsible for
    checking that the resolved app token equals its configured source root.
    """

    reference = parse_bitable_reference(url)
    if reference.entry_kind == "wiki_bitable":
        node = await api.get_node(node_token=reference.node_token)
        if not isinstance(node, Mapping) or str(node.get("obj_type") or "").lower() != "bitable":
            raise FeishuSourceError("Wiki node does not point to a Bitable")
        app_token = _clean_token(node.get("obj_token"), "Bitable app token")
        reference = replace(reference, app_token=app_token)
    tables = await api.list_bitable_tables(app_token=reference.app_token)
    if not isinstance(tables, list) or not all(isinstance(item, Mapping) for item in tables):
        raise FeishuSourceError("Bitable table metadata is invalid")
    result = [_metadata_table(item) for item in tables]
    ids = {str(item.get("table_id") or "") for item in result}
    if reference.table_id and reference.table_id not in ids:
        raise FeishuSourceError("Requested Bitable table is not visible")
    return reference, result
