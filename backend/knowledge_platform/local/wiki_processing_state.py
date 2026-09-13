"""Read-only, fail-closed projection of local Wiki processing state."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Any
from urllib.parse import quote

from knowledge_contracts import Principal, is_valid_knowledge_uri
from knowledge_platform.catalog.wiki_lineage import public_wiki_lineage


_ASSET_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_MARKDOWN_BYTES = 8 * 1024 * 1024
_MAX_TOTAL_MARKDOWN_BYTES = 64 * 1024 * 1024
_MAX_ROWS = 1000
_RECEIPT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_KEY_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


def _fingerprint(*, snapshot_id: str, source_revision: str, source_uri: str, content_digest: str) -> str:
    encoded = json.dumps(
        {
            "content_digest": content_digest,
            "snapshot_id": snapshot_id,
            "source_revision": source_revision,
            "source_uri": source_uri,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _has_table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _has_column(connection: sqlite3.Connection, table: str, column: str) -> bool:
    return any(str(row[1]) == column for row in connection.execute(f"PRAGMA table_info({table})"))


def _scope_authorized(principal: Principal) -> bool:
    scopes = set(principal.scopes)
    if principal.tenant_id is not None:
        return False
    if not ({"knowledge.read", "knowledge:read", "knowledge.admin", "knowledge:admin"} & scopes):
        return False
    # The target Space is only learned from the Catalog row, so require a
    # concrete Space grant before opening the database at all.
    return any(scope.startswith(("knowledge.space:", "knowledge:space:")) for scope in scopes)


def _space_authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return f"knowledge.space:{space_id}" in scopes or f"knowledge:space:{space_id}" in scopes


class WikiProcessingStateService:
    """Expose one immutable Catalog snapshot of a Raw asset's Wiki state."""

    def __init__(self, database_path: Path) -> None:
        self._database_path = database_path.expanduser().absolute()
        if self._database_path.is_symlink() or not self._database_path.is_file():
            raise FileNotFoundError("Wiki Catalog database is unavailable")

    def _connect(self) -> sqlite3.Connection:
        uri = f"file:{quote(str(self._database_path), safe='/')}?mode=ro"
        connection = sqlite3.connect(uri, uri=True)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        return connection

    def read(self, asset_id: str, principal: Principal) -> dict[str, Any]:
        if not isinstance(asset_id, str) or not _ASSET_ID_RE.fullmatch(asset_id):
            raise ValueError("Wiki asset id is invalid")
        if not _scope_authorized(principal):
            raise PermissionError("knowledge.read Space scope is required")

        connection = self._connect()
        try:
            connection.execute("BEGIN")
            if not _has_table(connection, "knowledge_assets"):
                raise ValueError("Wiki Catalog is unavailable")
            metadata_select = ", metadata_json" if _has_column(connection, "knowledge_assets", "metadata_json") else ""
            source = connection.execute(
                "SELECT id, space_id, kind, source_type, source_uri, revision, content_digest" + metadata_select
                + " FROM knowledge_assets WHERE id = ?",
                (asset_id,),
            ).fetchone()
            if source is None:
                raise PermissionError("Wiki asset is unavailable")

            space_id = str(source["space_id"] or "")
            if not _space_authorized(principal, space_id):
                raise PermissionError("Wiki asset is unavailable")
            if source["kind"] != "raw_snapshot" or source["source_type"] != "local_wiki_raw":
                raise ValueError("Unsupported Wiki source Asset")

            source_uri = str(source["source_uri"] or "")
            source_revision = str(source["revision"] or "")
            content_digest = str(source["content_digest"] or "")
            expected_uri = f"knowledge://spaces/{space_id}/assets/{asset_id}"
            if (
                source_uri != expected_uri
                or not is_valid_knowledge_uri(source_uri)
                or not _DIGEST_RE.fullmatch(source_revision)
                or source_revision != content_digest
                or not _DIGEST_RE.fullmatch(content_digest)
            ):
                raise ValueError("Wiki source Asset identity is invalid")

            metadata = None
            if metadata_select:
                raw_metadata = source["metadata_json"]
                try:
                    metadata = json.loads(raw_metadata) if raw_metadata is not None else {}
                except (TypeError, json.JSONDecodeError) as error:
                    raise ValueError("Wiki source metadata is invalid") from error
                if not isinstance(metadata, dict):
                    raise ValueError("Wiki source metadata is invalid")
            history = public_wiki_lineage(metadata) if metadata is not None else None

            unsettled = 0
            publications: dict[str, str] = {}
            if _has_table(connection, "knowledge_local_wiki_compilations"):
                rows = connection.execute(
                    """SELECT key_digest, space_id, fingerprint, snapshot_id,
                              source_revision, source_uri, content_digest, status,
                              resource_uri, receipt_id,
                              length(markdown) AS markdown_length, typeof(markdown) AS markdown_type
                         FROM knowledge_local_wiki_compilations
                        WHERE space_id = ? AND snapshot_id = ?
                        ORDER BY key_digest LIMIT ?""",
                    (space_id, asset_id, _MAX_ROWS + 1),
                ).fetchall()
                if len(rows) > _MAX_ROWS:
                    raise ValueError("Wiki compilation state exceeds row limit")
                if any(
                    str(row["space_id"] or "") != space_id
                    or str(row["source_uri"] or "") != source_uri
                    or not _DIGEST_RE.fullmatch(str(row["content_digest"] or ""))
                    or row["source_revision"] != row["content_digest"]
                    for row in rows
                ):
                    raise ValueError("Malformed Wiki compilation source binding")
                total_markdown_bytes = sum(
                    int(row["markdown_length"] or 0) for row in rows
                )
                if total_markdown_bytes > _MAX_TOTAL_MARKDOWN_BYTES:
                    raise ValueError("Wiki compilation state exceeds byte limit")
                for row in rows:
                    row_revision = row["source_revision"]
                    current_revision = row_revision == source_revision
                    expected_fingerprint = _fingerprint(
                        snapshot_id=asset_id, source_revision=row_revision,
                        source_uri=source_uri, content_digest=row["content_digest"],
                    )
                    expected_output_id = "wiki_" + expected_fingerprint.removeprefix("sha256:")[:48]
                    expected_resource_uri = f"knowledge://spaces/{space_id}/assets/{expected_output_id}"
                    if row["fingerprint"] != expected_fingerprint or not _KEY_DIGEST_RE.fullmatch(str(row["key_digest"] or "")):
                        raise ValueError("Malformed Wiki compilation request")
                    status = str(row["status"] or "")
                    if status == "running":
                        unsettled += int(current_revision)
                        continue
                    if status != "succeeded":
                        raise ValueError("Unknown Wiki compilation status")
                    if (
                        str(row["fingerprint"] or "") != expected_fingerprint
                        or str(row["resource_uri"] or "") != expected_resource_uri
                        or not _KEY_DIGEST_RE.fullmatch(str(row["key_digest"] or ""))
                    ):
                        raise ValueError("Malformed succeeded Wiki compilation")
                    receipt_id = row["receipt_id"]
                    if (
                        not isinstance(receipt_id, str)
                        or not _RECEIPT_RE.fullmatch(receipt_id)
                    ):
                        raise ValueError("Malformed Wiki publication receipt")
                    length = row["markdown_length"]
                    if not isinstance(length, int) or length <= 0 or length > _MAX_MARKDOWN_BYTES:
                        raise ValueError("Malformed Wiki markdown")
                    if row["markdown_type"] != "blob":
                        raise ValueError("Malformed Wiki markdown")
                    markdown_row = connection.execute(
                        "SELECT markdown FROM knowledge_local_wiki_compilations WHERE key_digest = ?",
                        (row["key_digest"],),
                    ).fetchone()
                    if markdown_row is None or row["markdown_type"] != "blob" or not isinstance(markdown_row["markdown"], (bytes, bytearray, memoryview)):
                        raise ValueError("Malformed Wiki markdown")
                    markdown = bytes(markdown_row["markdown"])
                    if not markdown or len(markdown) > _MAX_MARKDOWN_BYTES:
                        raise ValueError("Malformed Wiki markdown")
                    try:
                        markdown.decode("utf-8")
                    except UnicodeDecodeError as error:
                        raise ValueError("Malformed Wiki markdown") from error
                    published_digest = "sha256:" + hashlib.sha256(markdown).hexdigest()

                    output = connection.execute(
                        """SELECT space_id, kind, source_type, source_uri, revision,
                                  content_digest, metadata_json
                             FROM knowledge_assets WHERE id = ?""",
                        (expected_output_id,),
                    ).fetchone()
                    if output is None:
                        raise ValueError("Succeeded Wiki compilation has no publication")
                    if (
                        str(output["space_id"] or "") != space_id
                        or output["kind"] != "wiki_page"
                        or output["source_type"] != "local_wiki_compilation"
                        or output["source_uri"] != expected_resource_uri
                        or output["revision"] != row_revision
                        or output["content_digest"] != published_digest
                    ):
                        raise ValueError("Malformed Wiki publication Asset")
                    try:
                        output_metadata = json.loads(output["metadata_json"])
                    except (TypeError, json.JSONDecodeError) as error:
                        raise ValueError("Malformed Wiki publication metadata") from error
                    if (
                        not isinstance(output_metadata, dict)
                        or output_metadata.get("published") is not True
                        or output_metadata.get("source_snapshot_id") != asset_id
                        or output_metadata.get("receipt_id") != receipt_id
                    ):
                        raise ValueError("Malformed Wiki publication metadata")
                    if current_revision:
                        publications[expected_resource_uri] = published_digest

            coverage = "no_consumption_record"
            if publications:
                coverage = "current_revision_compiled"
            elif history is not None and history.get("historical_consumed") is True:
                coverage = "historical_consumption_recorded"
            return {
                "asset_id": asset_id,
                "space_id": space_id,
                "source_revision": source_revision,
                "coverage": coverage,
                "historical": history,
                "current_publications": [
                    {"resource_uri": uri, "content_digest": publications[uri]}
                    for uri in sorted(publications)
                ],
                "unsettled_attempts": unsettled,
                "live_execution_known": False,
                "automatic_scheduling_allowed": False,
            }
        finally:
            connection.close()
