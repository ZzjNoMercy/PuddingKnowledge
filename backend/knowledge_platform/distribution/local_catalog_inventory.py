"""Read-only, digest-verified inventory of the local Catalog stage."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path

_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_OBJECT_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@+-]{0,159}$")


class LocalCatalogInventoryError(ValueError):
    """Raised when the local Catalog stage cannot be verified read-only."""


def _digest(value: object) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True, slots=True)
class LocalCatalogInventory:
    """Stable identity projection for staged Catalog assets and Collections."""

    object_ids: tuple[str, ...]
    database_digest: str
    asset_count: int
    collection_count: int

    def __post_init__(self) -> None:
        if tuple(sorted(self.object_ids)) != self.object_ids or len(set(self.object_ids)) != len(self.object_ids):
            raise LocalCatalogInventoryError("local Catalog object IDs must be sorted and unique")
        if any(not _OBJECT_ID.fullmatch(value) for value in self.object_ids):
            raise LocalCatalogInventoryError("local Catalog object ID is unsafe")
        if not _SHA256.fullmatch(self.database_digest):
            raise LocalCatalogInventoryError("local Catalog database digest is invalid")
        if not isinstance(self.asset_count, int) or self.asset_count < 0:
            raise LocalCatalogInventoryError("local Catalog asset count is invalid")
        if not isinstance(self.collection_count, int) or self.collection_count < 0:
            raise LocalCatalogInventoryError("local Catalog Collection count is invalid")
        if len(self.object_ids) != self.asset_count + self.collection_count:
            raise LocalCatalogInventoryError("local Catalog object inventory count is inconsistent")

    @property
    def object_digest(self) -> str:
        return _digest(self.object_ids)


def _read_json(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        raise LocalCatalogInventoryError("local Catalog stage report is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise LocalCatalogInventoryError("local Catalog stage report is not readable") from error
    if not isinstance(value, dict):
        raise LocalCatalogInventoryError("local Catalog stage report must be an object")
    return value


def read_local_catalog_inventory(stage_report: Path) -> LocalCatalogInventory:
    """Load only real IDs from the stage SQLite, after digest verification."""

    report = _read_json(stage_report)
    try:
        targets = report["targets"]
        platform = targets["platform"]
        counts = platform["table_counts"]
        files = platform["files"]
        asset_count = counts["knowledge_assets"]
        collection_count = counts["knowledge_datasets"]
    except (KeyError, TypeError) as error:
        raise LocalCatalogInventoryError("local Catalog stage report shape is invalid") from error
    if (
        isinstance(asset_count, bool)
        or not isinstance(asset_count, int)
        or asset_count < 0
        or isinstance(collection_count, bool)
        or not isinstance(collection_count, int)
        or collection_count < 0
    ):
        raise LocalCatalogInventoryError("local Catalog stage counts are invalid")
    if not isinstance(platform, dict) or not isinstance(files, dict) or set(files) != {"knowledge-platform.sqlite3"}:
        raise LocalCatalogInventoryError("local Catalog stage must contain one allowlisted SQLite file")
    descriptor = files["knowledge-platform.sqlite3"]
    if not isinstance(descriptor, dict):
        raise LocalCatalogInventoryError("local Catalog stage database descriptor is invalid")
    declared_digest = descriptor.get("sha256")
    platform_digest = platform.get("sha256")
    if not isinstance(declared_digest, str) or not _SHA256.fullmatch(declared_digest):
        raise LocalCatalogInventoryError("local Catalog stage database digest is invalid")
    if platform_digest != declared_digest:
        raise LocalCatalogInventoryError("local Catalog stage database digests disagree")

    database_path = stage_report.parent / "knowledge-platform.sqlite3"
    if database_path.is_symlink() or not database_path.is_file():
        raise LocalCatalogInventoryError("local Catalog stage database is unavailable")
    try:
        actual_digest = "sha256:" + hashlib.sha256(database_path.read_bytes()).hexdigest()
    except OSError as error:
        raise LocalCatalogInventoryError("local Catalog stage database is unreadable") from error
    if actual_digest != declared_digest:
        raise LocalCatalogInventoryError("local Catalog stage database digest does not match its report")

    try:
        connection = sqlite3.connect(f"file:{database_path}?mode=ro", uri=True)
        try:
            asset_rows = connection.execute("SELECT id FROM knowledge_assets ORDER BY id").fetchall()
            collection_rows = connection.execute("SELECT id FROM knowledge_datasets ORDER BY id").fetchall()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        raise LocalCatalogInventoryError("local Catalog stage database cannot be read") from error

    rows = (*asset_rows, *collection_rows)
    if any(not isinstance(row, tuple) or len(row) != 1 or not isinstance(row[0], str) or not row[0] for row in rows):
        raise LocalCatalogInventoryError("local Catalog stage contains an invalid object ID")
    if any(not _OBJECT_ID.fullmatch(row[0]) for row in rows):
        raise LocalCatalogInventoryError("local Catalog stage contains an unsafe object ID")
    asset_ids = tuple(f"asset:{row[0]}" for row in asset_rows)
    collection_ids = tuple(f"collection:{row[0]}" for row in collection_rows)
    object_ids = tuple(sorted((*asset_ids, *collection_ids)))
    if len(asset_ids) != asset_count or len(collection_ids) != collection_count:
        raise LocalCatalogInventoryError("local Catalog object inventory does not match its counts")
    return LocalCatalogInventory(
        object_ids=object_ids,
        database_digest=actual_digest,
        asset_count=asset_count,
        collection_count=collection_count,
    )


__all__ = ["LocalCatalogInventory", "LocalCatalogInventoryError", "read_local_catalog_inventory"]
