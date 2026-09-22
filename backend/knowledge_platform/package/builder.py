"""Deterministic, content-addressed Knowledge Package builder and validator."""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import sys
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_contracts import is_valid_knowledge_uri

_PACKAGE_FORMAT = "agent-knowledge-package/v1"
_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._:-")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_HOST_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_])(?:/Users/|/private/|/tmp/|/var/|/home/|/etc/|/opt/|/usr/|/root/|/mnt/|/Applications/|/System/|/Volumes/|~/|[A-Za-z]:[\\/])"
)
_FILE_URI_RE = re.compile(r"(?i)file://|(?:^|[\s(=,])//[^/\s]+/")
_ABSOLUTE_PATH_RE = re.compile(r"(?<![A-Za-z0-9_.])/(?:[^/\\\s]+/){1,}[^\\\s]*")
_KNOWLEDGE_URI_RE = re.compile(r"knowledge://[^\s]+")
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)(?:bearer\s+|password\s*[=:]|secret\s*[=:]|token\s*[=:]|authorization\s*[=:]|"
    r"api[_-]?key\s*[=:]|access[_-]?token\s*[=:]|private[_-]?key\s*[=:])"
)
_SECRET_KEY_RE = re.compile(r"(?i)(?:credential|password|secret|token|private[_-]?key|authorization|api[_-]?key)")
_MAX_PACKAGE_UNCOMPRESSED_BYTES = 1024 * 1024 * 1024


class PackageBuildError(ValueError):
    """The source data cannot produce a complete portable package."""


class PackageValidationError(ValueError):
    """The package is malformed, unsafe, or has a checksum mismatch."""


@dataclass(frozen=True, slots=True)
class PackageBuildResult:
    package_root: Path
    package_revision: str
    file_count: int
    asset_count: int


@dataclass(frozen=True, slots=True)
class PackageValidationResult:
    package_root: Path
    package_revision: str
    file_count: int
    asset_count: int
    entry_names: tuple[str, ...] = ()
    entry_digests: tuple[tuple[str, str], ...] = ()


def _sha256_bytes(value: bytes) -> str:
    return f"sha256:{hashlib.sha256(value).hexdigest()}"


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(_read_secure_file(path))


def _read_secure_file(path: Path) -> bytes:
    """Read one regular file without following a final-component symlink."""

    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError(f"file is not regular: {path}")
        with os.fdopen(descriptor, "rb", closefd=True) as stream:
            descriptor = -1
            return stream.read()
    finally:
        if descriptor != -1:
            os.close(descriptor)


def _read_secure_relative_file(root: Path, relative: str) -> bytes:
    """Read a validated package entry without following any path component."""

    parts = _safe_relative_path(relative, field="package entry").split("/")
    root_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    directory_descriptor = root_descriptor
    try:
        for part in parts[:-1]:
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        descriptor = os.open(parts[-1], os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=directory_descriptor)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise OSError(f"file is not regular: {relative}")
            with os.fdopen(descriptor, "rb", closefd=True) as stream:
                descriptor = -1
                return stream.read()
        finally:
            if descriptor != -1:
                os.close(descriptor)
    finally:
        os.close(directory_descriptor)


def _open_or_create_secure_directory(root: Path, relative: str = "") -> int:
    """Open a directory below root, creating each missing component without symlink traversal."""

    parts = [] if not relative else _safe_relative_path(relative, field="package directory").split("/")
    directory_descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    try:
        for part in parts:
            try:
                os.mkdir(part, 0o700, dir_fd=directory_descriptor)
            except FileExistsError:
                pass
            next_descriptor = os.open(
                part,
                os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
                dir_fd=directory_descriptor,
            )
            os.close(directory_descriptor)
            directory_descriptor = next_descriptor
        return directory_descriptor
    except Exception:
        os.close(directory_descriptor)
        raise


def _write_secure_relative_file(root: Path, relative: str, content: bytes) -> None:
    """Create one file below root without following any directory or final symlink."""

    parts = _safe_relative_path(relative, field="package entry").split("/")
    directory_descriptor = _open_or_create_secure_directory(root, "/".join(parts[:-1]))
    descriptor = -1
    try:
        descriptor = os.open(
            parts[-1],
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=directory_descriptor,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            descriptor = -1
            stream.write(content)
    finally:
        if descriptor != -1:
            os.close(descriptor)
        os.close(directory_descriptor)


def _create_private_temp_directory(parent_descriptor: int, prefix: str) -> str:
    """Create a private staging directory anchored to an already-open parent fd."""

    for _ in range(100):
        name = f"{prefix}{secrets.token_hex(10)}"
        try:
            os.mkdir(name, 0o700, dir_fd=parent_descriptor)
        except FileExistsError:
            continue
        return name
    raise OSError("could not allocate a unique staging directory")


def _publish_noreplace(
    source_name: str, destination_name: str, parent_descriptor: int, *, directory: bool
) -> None:
    """Publish a staged output atomically, failing if the destination already exists."""

    if not directory:
        # A hard link is an atomic, no-replace publication for a regular file.
        os.link(
            source_name,
            destination_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        os.unlink(source_name, dir_fd=parent_descriptor)
        return
    if sys.platform == "darwin":
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        renameatx_np = libc.renameatx_np
        renameatx_np.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameatx_np.restype = ctypes.c_int
        result = renameatx_np(
            parent_descriptor,
            source_name.encode("utf-8"),
            parent_descriptor,
            destination_name.encode("utf-8"),
            0x00000004,  # RENAME_EXCL
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    if sys.platform.startswith("linux"):
        import ctypes

        libc = ctypes.CDLL(None, use_errno=True)
        try:
            renameat2 = libc.renameat2
        except AttributeError as error:
            raise OSError("atomic no-replace directory publication is unavailable in this libc") from error
        renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
        renameat2.restype = ctypes.c_int
        result = renameat2(
            parent_descriptor,
            source_name.encode("utf-8"),
            parent_descriptor,
            destination_name.encode("utf-8"),
            0x00000001,  # RENAME_NOREPLACE
        )
        if result == 0:
            return
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))
    raise OSError("atomic no-replace directory publication is unsupported on this platform")


def _json_bytes(value: Any) -> bytes:
    return (json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")


def _safe_id(value: Any, *, field: str) -> str:
    if not isinstance(value, str):
        raise PackageBuildError(f"{field} must be a portable identifier")
    result = value
    if not result or result in {".", ".."} or len(result) > 160 or any(char not in _ID_CHARS for char in result):
        raise PackageBuildError(f"{field} must be a portable identifier")
    return result


def _validated_id(value: Any, *, field: str) -> str:
    try:
        return _safe_id(value, field=field)
    except PackageBuildError as error:
        raise PackageValidationError(str(error)) from error


def _safe_relative_path(value: str, *, field: str) -> str:
    path = Path(value)
    if not value or path.is_absolute() or "\\" in value or any(part in {"", ".", ".."} for part in path.parts):
        raise PackageValidationError(f"{field} must be a safe relative path")
    return path.as_posix()


def _portable_text(
    value: Any, *, field: str, max_length: int = 100_000, error_type: type[ValueError] = PackageBuildError
) -> str:
    text = str(value or "")
    metadata_scan_text = _KNOWLEDGE_URI_RE.sub("", text)
    if (
        len(text) > max_length
        or "\x00" in text
        or _HOST_PATH_RE.search(metadata_scan_text)
        or _FILE_URI_RE.search(metadata_scan_text)
        or _ABSOLUTE_PATH_RE.search(metadata_scan_text)
        or _SECRET_ASSIGNMENT_RE.search(metadata_scan_text)
    ):
        raise error_type(f"{field} contains a host path or secret-bearing value")
    return text


def _portable_value(value: Any, *, field: str, error_type: type[ValueError] = PackageBuildError) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            key_text = _portable_text(key, field=f"{field}.key", max_length=160, error_type=error_type)
            if _SECRET_KEY_RE.search(key_text):
                raise error_type(f"{field} contains a secret-bearing field")
            result[key_text] = _portable_value(item, field=f"{field}.{key_text}", error_type=error_type)
        return result
    if isinstance(value, (list, tuple)):
        return [_portable_value(item, field=f"{field}[]", error_type=error_type) for item in value]
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _portable_text(value, field=field, error_type=error_type)


def _provider_versions(
    value: Mapping[str, Any] | None,
    *,
    field: str,
    error_type: type[ValueError],
) -> dict[str, str]:
    """Normalize provider provenance without accepting secrets or host facts."""

    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise error_type(f"{field} must be a mapping")
    result: dict[str, str] = {}
    for raw_key, raw_version in value.items():
        try:
            key = _safe_id(raw_key, field=f"{field}.key")
        except PackageBuildError as error:
            raise error_type(str(error)) from error
        if _SECRET_KEY_RE.search(key):
            raise error_type(f"{field} contains a secret-bearing field")
        if not isinstance(raw_version, str) or not raw_version.strip():
            raise error_type(f"{field}.{key} must be a non-empty version")
        result[key] = _portable_text(raw_version, field=f"{field}.{key}", max_length=256, error_type=error_type)
    return dict(sorted(result.items()))


def _package_sbom(*, package_id: str, version: str, provider_versions: Mapping[str, str]) -> dict[str, Any]:
    """Build a deterministic CycloneDX provenance SBOM for packaged providers."""

    serial_material = json.dumps(
        {"id": package_id, "version": version, "providers": dict(provider_versions)},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    serial = "urn:uuid:" + hashlib.sha256(serial_material).hexdigest()[:32]
    components = [
        {
            "bom-ref": f"provider:{name}",
            "name": name,
            "properties": [{"name": "puddingai:provenance", "value": "package-provider"}],
            "type": "library",
            "version": provider_version,
        }
        for name, provider_version in sorted(provider_versions.items())
    ]
    return {
        "bomFormat": "CycloneDX",
        "components": components,
        "metadata": {
            "component": {"name": package_id, "type": "application", "version": version},
            "tools": [{"name": "puddingai-knowledge-package-builder", "version": "agent-knowledge-package/v1"}],
        },
        "serialNumber": serial,
        "specVersion": "1.5",
        "version": 1,
    }


def _ensure_safe_parent_chain(path: Path, *, error_type: type[ValueError] = PackageValidationError) -> None:
    cursor = path.parent
    while True:
        if cursor.is_symlink():
            raise error_type(f"output parent must not contain a symlink: {cursor}")
        if cursor.parent == cursor:
            return
        cursor = cursor.parent


def _open_safe_parent_directory(path: Path, *, error_type: type[ValueError] = PackageValidationError) -> int:
    _ensure_safe_parent_chain(path, error_type=error_type)
    try:
        descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0))
    except OSError as error:
        raise error_type(f"output parent is not a stable directory: {path.parent}") from error
    if not stat.S_ISDIR(os.fstat(descriptor).st_mode):
        os.close(descriptor)
        raise error_type(f"output parent is not a directory: {path.parent}")
    return descriptor


def _write(root: Path, relative_path: str, content: bytes) -> None:
    _write_secure_relative_file(root, relative_path, content)


def _canonical_package_revision(manifest: Mapping[str, Any]) -> str:
    material = {
        "format": manifest["format"],
        "id": manifest["id"],
        "version": manifest["version"],
        "catalog_revision": manifest["catalog_revision"],
        "capabilities": manifest["capabilities"],
        "files": manifest["files"],
    }
    return _sha256_bytes(_json_bytes(material))


def _asset_record(asset: Mapping[str, Any], *, package_path: str) -> dict[str, Any]:
    asset_id = _safe_id(asset.get("id"), field="asset.id")
    source_uri = _portable_text(asset.get("source_uri"), field=f"asset {asset_id}.source_uri")
    if not is_valid_knowledge_uri(source_uri):
        raise PackageBuildError(f"asset {asset_id} has an invalid knowledge URI")
    content_digest = str(asset.get("content_digest") or "")
    if not _DIGEST_RE.fullmatch(content_digest):
        raise PackageBuildError(f"asset {asset_id} has an invalid content digest")
    record = {
        "id": asset_id,
        "space_id": _safe_id(asset.get("space_id"), field="asset.space_id"),
        "kind": _portable_text(asset.get("kind"), field=f"asset {asset_id}.kind"),
        "title": _portable_text(asset.get("title"), field=f"asset {asset_id}.title"),
        "description": _portable_text(asset.get("description"), field=f"asset {asset_id}.description"),
        "mime_type": _portable_text(asset.get("mime_type") or "application/octet-stream", field=f"asset {asset_id}.mime_type"),
        "source_type": _portable_text(asset.get("source_type"), field=f"asset {asset_id}.source_type"),
        "sheet_name": (
            _portable_text(asset.get("sheet_name"), field=f"asset {asset_id}.sheet_name", max_length=300)
            if asset.get("sheet_name") is not None
            else None
        ),
        "source_uri": source_uri,
        "revision": _portable_text(asset.get("revision") or content_digest, field=f"asset {asset_id}.revision"),
        "content_digest": content_digest,
        "package_path": package_path,
    }
    # Relations are explicit Catalog metadata.  Never infer them from paths or
    # filename conventions; absent fields remain absent for old packages.
    if "original_asset_id" in asset:
        original = asset["original_asset_id"]
        if not isinstance(original, str) or not original:
            raise PackageBuildError(f"asset {asset_id}.original_asset_id is invalid")
        record["original_asset_id"] = _safe_id(original, field=f"asset {asset_id}.original_asset_id")
    if "derivatives" in asset:
        derivatives = asset["derivatives"]
        if not isinstance(derivatives, Mapping):
            raise PackageBuildError(f"asset {asset_id}.derivatives is invalid")
        normalized: dict[str, str] = {}
        for kind, target in derivatives.items():
            if not isinstance(kind, str) or not kind.strip() or not isinstance(target, str) or not target:
                raise PackageBuildError(f"asset {asset_id}.derivatives is invalid")
            normalized[_portable_text(kind, field=f"asset {asset_id}.derivative kind", max_length=160)] = _safe_id(
                target, field=f"asset {asset_id}.derivative target"
            )
        record["derivatives"] = dict(sorted(normalized.items()))
    if "published_asset_ids" in asset:
        published = asset["published_asset_ids"]
        if not isinstance(published, (list, tuple)) or any(not isinstance(item, str) or not item for item in published):
            raise PackageBuildError(f"asset {asset_id}.published_asset_ids is invalid")
        if len(set(published)) != len(published):
            raise PackageBuildError(f"asset {asset_id}.published_asset_ids contains duplicates")
        record["published_asset_ids"] = sorted(
            _safe_id(item, field=f"asset {asset_id}.published_asset_id") for item in published
        )
    return record


def _validate_asset_relations(
    assets: Mapping[str, Mapping[str, Any]], *, error_type: type[ValueError]
) -> None:
    """Validate explicit asset relation targets and Space ownership."""
    for asset_id, asset in assets.items():
        targets: list[str] = []
        if "original_asset_id" in asset:
            targets.append(str(asset["original_asset_id"]))
        derivatives = asset.get("derivatives")
        if derivatives is not None:
            if not isinstance(derivatives, Mapping):
                raise error_type(f"asset {asset_id}.derivatives is invalid")
            targets.extend(str(target) for target in derivatives.values())
        published = asset.get("published_asset_ids")
        if published is not None:
            if not isinstance(published, list) or any(not isinstance(target, str) for target in published):
                raise error_type(f"asset {asset_id}.published_asset_ids is invalid")
            targets.extend(published)
        for target_id in targets:
            if target_id == asset_id:
                raise error_type(f"asset {asset_id} relation cannot target itself")
            target = assets.get(target_id)
            if target is None:
                raise error_type(f"asset {asset_id} relation targets unknown Asset: {target_id}")
            if str(target.get("space_id")) != str(asset.get("space_id")):
                raise error_type(f"asset {asset_id} relation crosses Space boundary")


def _collection_record(collection: Mapping[str, Any]) -> dict[str, Any]:
    collection_id = _safe_id(collection.get("id"), field="collection.id")
    asset_ids = collection.get("asset_ids", [])
    if not isinstance(asset_ids, (list, tuple)) or any(not isinstance(item, str) for item in asset_ids):
        raise PackageBuildError(f"collection {collection_id} has invalid asset_ids")
    if len(asset_ids) != len(set(asset_ids)):
        raise PackageBuildError(f"collection {collection_id} has duplicate asset_ids")
    database_source_ids = collection.get("database_source_ids", [])
    if not isinstance(database_source_ids, (list, tuple)) or any(not isinstance(item, str) or not item for item in database_source_ids):
        raise PackageBuildError(f"collection {collection_id} has invalid database_source_ids")
    if len(database_source_ids) != len(set(database_source_ids)):
        raise PackageBuildError(f"collection {collection_id} has duplicate database_source_ids")
    semantic_asset_ids = collection.get("semantic_asset_ids", [])
    if not isinstance(semantic_asset_ids, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in semantic_asset_ids
    ):
        raise PackageBuildError(f"collection {collection_id} has invalid semantic_asset_ids")
    if len(semantic_asset_ids) != len(set(semantic_asset_ids)):
        raise PackageBuildError(f"collection {collection_id} has duplicate semantic_asset_ids")
    capabilities = collection.get("capabilities", [])
    if not isinstance(capabilities, (list, tuple)) or any(not isinstance(item, str) or not item for item in capabilities):
        raise PackageBuildError(f"collection {collection_id} has invalid capabilities")
    freshness = collection.get("freshness", {})
    if not isinstance(freshness, Mapping):
        raise PackageBuildError(f"collection {collection_id} has invalid freshness")
    return {
        "id": collection_id,
        "space_id": _safe_id(collection.get("space_id"), field="collection.space_id"),
        "name": _portable_text(collection.get("name"), field=f"collection {collection_id}.name"),
        "version": _portable_text(collection.get("version"), field=f"collection {collection_id}.version"),
        "kind": _portable_text(collection.get("kind"), field=f"collection {collection_id}.kind"),
        "asset_ids": sorted(asset_ids),
        "database_source_ids": sorted(database_source_ids),
        "semantic_asset_ids": sorted(semantic_asset_ids),
        "capabilities": sorted(set(capabilities)),
        "freshness": _portable_value(freshness, field=f"collection {collection_id}.freshness"),
    }


def _database_source_record(source: Mapping[str, Any], *, space_ids: set[str]) -> dict[str, Any]:
    """Normalize portable Vanna rebuild inputs without carrying connections."""

    source_id = _safe_id(source.get("id"), field="database source.id")
    if set(source) - {"id", "space_id", "dataset_id", "dialect", "ddl", "documentation", "sql_examples", "entities"}:
        raise PackageBuildError(f"database source {source_id} has an unknown field")
    space_id = _safe_id(source.get("space_id"), field=f"database source {source_id}.space_id")
    if space_id not in space_ids:
        raise PackageBuildError(f"database source belongs to an unknown Space: {source_id}")
    dataset_id = _safe_id(source.get("dataset_id"), field=f"database source {source_id}.dataset_id")

    def records(field: str, required: tuple[str, ...]) -> list[dict[str, Any]]:
        values = source.get(field, [])
        if not isinstance(values, (list, tuple)):
            raise PackageBuildError(f"database source {source_id}.{field} must be an array")
        normalized: list[dict[str, Any]] = []
        seen: set[str] = set()
        for item in values:
            if not isinstance(item, Mapping):
                raise PackageBuildError(f"database source {source_id}.{field} has an invalid record")
            item_id = _safe_id(item.get("id"), field=f"database source {source_id}.{field}.id")
            if item_id in seen:
                raise PackageBuildError(f"database source {source_id}.{field} IDs must be unique")
            seen.add(item_id)
            allowed_fields = {"id", *required}
            if field == "entities":
                allowed_fields.add("aliases")
            if set(item) - allowed_fields:
                raise PackageBuildError(f"database source {source_id}.{field} has an unknown field")
            normalized_item: dict[str, Any] = {"id": item_id}
            for key in required:
                if key not in item:
                    raise PackageBuildError(f"database source {source_id}.{field}.{key} is required")
                if not isinstance(item.get(key), str) or not item[key].strip():
                    raise PackageBuildError(f"database source {source_id}.{field}.{key} must be non-empty text")
                normalized_item[key] = _portable_text(
                    item.get(key), field=f"database source {source_id}.{field}.{key}", max_length=256 * 1024
                )
            if field == "entities":
                aliases = item.get("aliases", [])
                if not isinstance(aliases, (list, tuple)) or any(not isinstance(alias, str) for alias in aliases):
                    raise PackageBuildError(f"database source {source_id}.entities.aliases are invalid")
                normalized_item["aliases"] = sorted(
                    {
                        _portable_text(alias, field=f"database source {source_id}.entities.alias", max_length=500)
                        for alias in aliases
                    }
                )
            normalized.append(normalized_item)
        return sorted(normalized, key=lambda item: item["id"])

    return {
        "id": source_id,
        "space_id": space_id,
        "dataset_id": dataset_id,
        "dialect": _portable_text(
            source.get("dialect") or "postgresql", field=f"database source {source_id}.dialect", max_length=64
        ),
        "ddl": records("ddl", ("content",)),
        "documentation": records("documentation", ("content",)),
        "sql_examples": records("sql_examples", ("question", "sql")),
        "entities": records("entities", ("canonical_name", "entity_type", "table_column")),
    }


def _semantic_asset_record(asset: Mapping[str, Any], *, space_id: str) -> tuple[dict[str, Any], bytes]:
    asset_id = _safe_id(asset.get("id"), field="semantic_asset.id")
    asset_type = str(asset.get("type") or "")
    if not asset_type or len(asset_type) > 64 or any(char not in _ID_CHARS for char in asset_type):
        raise PackageBuildError(f"semantic asset {asset_id} has an invalid type")
    body = _portable_text(asset.get("body") or asset.get("markdown"), field=f"semantic asset {asset_id}.body")
    if not body.strip():
        raise PackageBuildError(f"semantic asset {asset_id} has an empty Markdown body")
    aliases = asset.get("aliases", [])
    tags = asset.get("tags", [])
    if not isinstance(aliases, (list, tuple)) or any(not isinstance(item, str) for item in aliases):
        raise PackageBuildError(f"semantic asset {asset_id} has invalid aliases")
    if not isinstance(tags, (list, tuple)) or any(not isinstance(item, str) for item in tags):
        raise PackageBuildError(f"semantic asset {asset_id} has invalid tags")
    aliases = [_portable_text(item, field=f"semantic asset {asset_id}.aliases[]") for item in aliases]
    tags = [_portable_text(item, field=f"semantic asset {asset_id}.tags[]") for item in tags]
    frontmatter = asset.get("frontmatter", {})
    if not isinstance(frontmatter, Mapping):
        raise PackageBuildError(f"semantic asset {asset_id} has invalid frontmatter")
    metadata = {
        "id": asset_id,
        "name": _portable_text(asset.get("name"), field=f"semantic asset {asset_id}.name"),
        "type": asset_type,
        "description": _portable_text(asset.get("description"), field=f"semantic asset {asset_id}.description"),
        "aliases": sorted(set(aliases)),
        "tags": sorted(set(tags)),
        "frontmatter": _portable_value(frontmatter, field=f"semantic asset {asset_id}.frontmatter"),
    }
    markdown = (
        "---\n"
        + json.dumps(metadata, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n---\n\n"
        + body.rstrip()
        + "\n"
    ).encode("utf-8")
    declared_digest = asset.get("content_digest")
    actual_digest = _sha256_bytes(markdown)
    if declared_digest is not None and declared_digest != actual_digest:
        raise PackageBuildError(f"semantic asset {asset_id} content digest does not match source")
    return (
        {
            **metadata,
            "space_id": _safe_id(space_id, field=f"semantic asset {asset_id}.space_id"),
            "package_path": f"semantics/assets/{asset_id}.md",
            "content_digest": actual_digest,
        },
        markdown,
    )


class KnowledgePackageBuilder:
    """Build a complete snapshot package from already-authorized inputs."""

    def build(
        self,
        *,
        output_dir: Path,
        package_id: str,
        version: str,
        spaces: list[Mapping[str, Any]],
        collections: list[Mapping[str, Any]],
        assets: list[Mapping[str, Any]],
        asset_files: Mapping[str, Path],
        capabilities: list[str],
        catalog_revision: str,
        semantic_assets: list[Mapping[str, Any]] | None = None,
        database_sources: list[Mapping[str, Any]] | None = None,
        provider_versions: Mapping[str, Any] | None = None,
    ) -> PackageBuildResult:
        package_id = _safe_id(package_id, field="package_id")
        if not isinstance(version, str) or not version.strip() or len(version) > 100:
            raise PackageBuildError("version must be a short non-empty value")
        version = _portable_text(version, field="package version", max_length=100)
        if (
            not isinstance(spaces, list)
            or not isinstance(collections, list)
            or not isinstance(assets, list)
            or not isinstance(capabilities, list)
            or not isinstance(asset_files, Mapping)
            or (semantic_assets is not None and not isinstance(semantic_assets, list))
            or (database_sources is not None and not isinstance(database_sources, list))
            or (provider_versions is not None and not isinstance(provider_versions, Mapping))
        ):
            raise PackageBuildError("package inputs have invalid shapes")
        if any(not isinstance(item, Mapping) for item in (*spaces, *collections, *assets)):
            raise PackageBuildError("package records must be mappings")
        if any(not isinstance(item, str) or not item for item in capabilities):
            raise PackageBuildError("capabilities must be non-empty strings")
        capabilities = [_portable_text(item, field="capability", max_length=160) for item in capabilities]
        normalized_provider_versions = _provider_versions(
            provider_versions,
            field="provider_versions",
            error_type=PackageBuildError,
        )
        if not isinstance(catalog_revision, str) or not _DIGEST_RE.fullmatch(catalog_revision):
            raise PackageBuildError("catalog_revision must be a sha256 digest")
        normalized_spaces = [
            {
                "id": _safe_id(space.get("id"), field="space.id"),
                "name": _portable_text(space.get("name"), field="space.name"),
                "description": _portable_text(space.get("description"), field="space.description"),
            }
            for space in spaces
        ]
        if len({item["id"] for item in normalized_spaces}) != len(normalized_spaces):
            raise PackageBuildError("space IDs must be unique")
        normalized_spaces.sort(key=lambda item: item["id"])
        source_inputs = database_sources or []
        if any(not isinstance(item, Mapping) for item in source_inputs):
            raise PackageBuildError("database source records must be mappings")
        normalized_database_sources = [
            _database_source_record(item, space_ids={space["id"] for space in normalized_spaces})
            for item in source_inputs
        ]
        if len({item["id"] for item in normalized_database_sources}) != len(normalized_database_sources):
            raise PackageBuildError("database source IDs must be unique")
        normalized_database_sources.sort(key=lambda item: item["id"])
        normalized_collections = [_collection_record(item) for item in collections]
        source_by_id = {item["id"]: item for item in normalized_database_sources}
        for collection in normalized_collections:
            for source_id in collection["database_source_ids"]:
                source = source_by_id.get(source_id)
                if source is None:
                    raise PackageBuildError(f"Collection {collection['id']} references an unknown database source")
                if source["space_id"] != collection["space_id"]:
                    raise PackageBuildError(f"Collection {collection['id']} references a database source from another Space")
        collection_keys = {(item["id"], item["version"]) for item in normalized_collections}
        if len(collection_keys) != len(normalized_collections):
            raise PackageBuildError("Collection id/version pairs must be unique")
        normalized_collections.sort(key=lambda item: (item["space_id"], item["id"], item["version"]))
        source_assets = {str(asset.get("id")): asset for asset in assets}
        if len(source_assets) != len(assets):
            raise PackageBuildError("asset IDs must be unique")
        if set(str(item.get("id")) for item in normalized_spaces) != {item["space_id"] for item in normalized_collections}:
            raise PackageBuildError("every Collection must belong to a declared Space")
        referenced_asset_ids = {asset_id for item in normalized_collections for asset_id in item["asset_ids"]}
        if referenced_asset_ids - set(source_assets):
            raise PackageBuildError("Collection references an undeclared Asset")
        semantic_inputs = semantic_assets or []
        if any(not isinstance(item, Mapping) for item in semantic_inputs):
            raise PackageBuildError("semantic asset records must be mappings")
        semantic_records: list[dict[str, Any]] = []
        semantic_payloads: dict[str, bytes] = {}
        semantic_space_by_id: dict[str, str] = {}
        for collection in normalized_collections:
            for semantic_id in collection["semantic_asset_ids"]:
                previous = semantic_space_by_id.setdefault(semantic_id, collection["space_id"])
                if previous != collection["space_id"]:
                    raise PackageBuildError(f"Semantic Asset crosses Space boundary: {semantic_id}")
        for semantic_asset in semantic_inputs:
            semantic_id = _safe_id(semantic_asset.get("id"), field="semantic_asset.id")
            declared_space_id = (
                semantic_asset["space_id"]
                if "space_id" in semantic_asset
                else semantic_space_by_id.get(semantic_id)
            )
            if declared_space_id is None:
                raise PackageBuildError(f"Semantic Asset has no owning Space: {semantic_id}")
            if semantic_id in semantic_space_by_id and declared_space_id != semantic_space_by_id[semantic_id]:
                raise PackageBuildError(f"Semantic Asset crosses Space boundary: {semantic_id}")
            record, markdown = _semantic_asset_record({**semantic_asset, "id": semantic_id}, space_id=str(declared_space_id))
            semantic_records.append(record)
            semantic_payloads[record["package_path"]] = markdown
        if len({record["id"] for record in semantic_records}) != len(semantic_records):
            raise PackageBuildError("semantic asset IDs must be unique")
        referenced_semantic_asset_ids = {
            semantic_id for item in normalized_collections for semantic_id in item["semantic_asset_ids"]
        }
        if referenced_semantic_asset_ids - {record["id"] for record in semantic_records}:
            raise PackageBuildError("Collection references an undeclared Semantic Asset")
        if {record["id"] for record in semantic_records} - referenced_semantic_asset_ids:
            raise PackageBuildError("package contains an unreferenced Semantic Asset")
        missing_files = sorted(referenced_asset_ids - set(asset_files))
        if missing_files:
            raise PackageBuildError(f"package export rejected; missing asset files: {missing_files}")

        normalized_assets: list[dict[str, Any]] = []
        file_payloads: dict[str, bytes] = {}
        for asset_id in sorted(source_assets):
            if asset_id not in referenced_asset_ids:
                continue
            source_path = Path(asset_files[asset_id]).expanduser()
            if source_path.is_symlink() or not source_path.is_file():
                raise PackageBuildError(f"package export rejected; asset file is not a regular file: {asset_id}")
            suffix = source_path.suffix.lower()
            if suffix and (len(suffix) > 16 or any(char not in ".abcdefghijklmnopqrstuvwxyz0123456789_-" for char in suffix)):
                raise PackageBuildError(f"asset {asset_id} has an unsafe file extension")
            package_path = f"assets/originals/{asset_id}{suffix}"
            try:
                content = _read_secure_file(source_path)
            except OSError as error:
                raise PackageBuildError(f"package export rejected; asset file is not a regular file: {asset_id}") from error
            record = _asset_record(source_assets[asset_id], package_path=package_path)
            actual_digest = _sha256_bytes(content)
            if actual_digest != record["content_digest"]:
                raise PackageBuildError(f"asset {asset_id} content digest does not match Catalog")
            normalized_assets.append(record)
            file_payloads[package_path] = content
        normalized_assets.sort(key=lambda item: item["id"])
        assets_by_id = {item["id"]: item for item in normalized_assets}
        _validate_asset_relations(assets_by_id, error_type=PackageBuildError)
        for collection in normalized_collections:
            if any(assets_by_id[asset_id]["space_id"] != collection["space_id"] for asset_id in collection["asset_ids"]):
                raise PackageBuildError(f"Collection {collection['id']} references an Asset from another Space")

        destination = output_dir.expanduser().absolute()
        if destination.exists() or destination.is_symlink():
            raise PackageBuildError(f"package output already exists: {destination}")
        _ensure_safe_parent_chain(destination, error_type=PackageBuildError)
        destination.parent.mkdir(parents=True, exist_ok=True)
        parent_descriptor = _open_safe_parent_directory(destination, error_type=PackageBuildError)
        temporary: Path | None = None
        try:
            temporary_name = _create_private_temp_directory(parent_descriptor, f".{package_id}-")
            temporary = destination.parent / temporary_name
            _write(temporary, "spaces/index.json", _json_bytes({"format": "agent-knowledge-spaces/v1", "spaces": normalized_spaces}))
            _write(temporary, "collections/index.json", _json_bytes({"format": "agent-knowledge-collections/v1", "collections": normalized_collections}))
            _write(temporary, "assets/index.json", _json_bytes({"format": "agent-knowledge-assets/v1", "assets": normalized_assets}))
            _write(
                temporary,
                "semantics/index.json",
                _json_bytes({"format": "agent-knowledge-semantics/v1", "assets": sorted(semantic_records, key=lambda item: item["id"])}),
            )
            _write(temporary, "evidence/index.json", _json_bytes({"format": "agent-knowledge-evidence/v1", "entries": []}))
            _write(
                temporary,
                "database/index.json",
                _json_bytes(
                    {
                        "format": "agent-knowledge-database-index/v1",
                        "sources": normalized_database_sources,
                    }
                ),
            )
            _write(temporary, "profiles/index.json", _json_bytes({"format": "agent-knowledge-profiles/v1", "entries": []}))
            _write(temporary, "evaluations/index.json", _json_bytes({"format": "agent-knowledge-evaluations/v1", "entries": []}))
            _write(
                temporary,
                "indexes/rebuild-manifest.json",
                _json_bytes(
                    {
                        "format": "agent-knowledge-index-rebuild/v1",
                        "inputs": [
                            "assets/index.json",
                            "semantics/index.json",
                            "evidence/index.json",
                            "database/index.json",
                        ],
                    }
                ),
            )
            bindings = {"format": "agent-knowledge-bindings/v1", "mode": "snapshot", "credentials": "not-in-package"}
            _write(temporary, "bindings.example.yaml", _json_bytes(bindings))
            _write(temporary, "README.md", f"# Knowledge Package {package_id}\n\nSnapshot package version `{version}`.\n".encode())
            package_document = {
                "format": _PACKAGE_FORMAT,
                "id": package_id,
                "version": version,
                "catalog_revision": catalog_revision,
                "spaces": ["./spaces/index.json"],
                "collections": ["./collections/index.json"],
                "semantic_assets": {"index": "./semantics/index.json"},
                "database": {"index": "./database/index.json"},
                "capabilities": sorted(set(capabilities)),
                "bindings": {"contract": "./bindings.example.yaml"},
                "indexes": {"rebuild_manifest": "./indexes/rebuild-manifest.json"},
                "evaluations": {"root": "./evaluations"},
                "provider_versions": normalized_provider_versions,
                "sbom": {"format": "CycloneDX", "path": "./sbom.json"},
            }
            _write(temporary, "knowledge-package.yaml", _json_bytes(package_document))
            _write(
                temporary,
                "sbom.json",
                _json_bytes(
                    _package_sbom(
                        package_id=package_id,
                        version=version,
                        provider_versions=normalized_provider_versions,
                    )
                ),
            )
            for package_path, content in file_payloads.items():
                _write(temporary, package_path, content)
            for package_path, content in semantic_payloads.items():
                _write(temporary, package_path, content)

            payload_files = sorted(
                path.relative_to(temporary).as_posix()
                for path in temporary.rglob("*")
                if path.is_file() and path.relative_to(temporary).as_posix() not in {"package-manifest.json", "checksums.json"}
            )
            manifest_files = {
                relative: {"bytes": (temporary / relative).stat().st_size, "sha256": _sha256_file(temporary / relative)}
                for relative in payload_files
            }
            manifest: dict[str, Any] = {
                "format": _PACKAGE_FORMAT,
                "id": package_id,
                "version": version,
                "catalog_revision": catalog_revision,
                "capabilities": sorted(set(capabilities)),
                "provider_versions": normalized_provider_versions,
                "sbom": "./sbom.json",
                "files": manifest_files,
            }
            manifest["package_revision"] = _canonical_package_revision(manifest)
            manifest_bytes = _json_bytes(manifest)
            _write(temporary, "package-manifest.json", manifest_bytes)
            checksums = {
                "format": "agent-knowledge-package-checksums/v1",
                "files": {
                    **manifest_files,
                    "package-manifest.json": {"bytes": len(manifest_bytes), "sha256": _sha256_bytes(manifest_bytes)},
                },
            }
            _write(temporary, "checksums.json", _json_bytes(checksums))
            _ensure_safe_parent_chain(destination, error_type=PackageBuildError)
            _publish_noreplace(temporary.name, destination.name, parent_descriptor, directory=True)
            return PackageBuildResult(destination, manifest["package_revision"], len(checksums["files"]), len(normalized_assets))
        except Exception:
            if temporary is not None and temporary.exists():
                shutil.rmtree(temporary)
            raise
        finally:
            os.close(parent_descriptor)


def _load_json(root: Path, relative_path: str) -> Any:
    try:
        return json.loads((root / relative_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
        raise PackageValidationError(f"invalid package JSON: {relative_path}") from error


def validate_package(package_root: Path) -> PackageValidationResult:
    root = package_root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise PackageValidationError("package root must be a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink():
            raise PackageValidationError(f"package contains a symlink: {path}")
        if path.is_file():
            _safe_relative_path(path.relative_to(root).as_posix(), field="package entry")
    manifest = _load_json(root, "package-manifest.json")
    if not isinstance(manifest, dict) or manifest.get("format") != _PACKAGE_FORMAT:
        raise PackageValidationError("package manifest format is invalid")
    files = manifest.get("files")
    if (
        not isinstance(files, dict)
        or not isinstance(manifest.get("catalog_revision"), str)
        or not _DIGEST_RE.fullmatch(manifest["catalog_revision"])
        or manifest.get("package_revision") != _canonical_package_revision(manifest)
    ):
        raise PackageValidationError("package manifest revision is invalid")
    _validated_id(manifest.get("id"), field="package id")
    if not isinstance(manifest.get("version"), str) or not manifest["version"].strip() or len(manifest["version"]) > 100:
        raise PackageValidationError("package version is invalid")
    normalized_provider_versions = _provider_versions(
        manifest.get("provider_versions"),
        field="package manifest provider_versions",
        error_type=PackageValidationError,
    )
    has_provenance_bundle = "provider_versions" in manifest or "sbom" in manifest
    if has_provenance_bundle:
        if manifest.get("provider_versions") != normalized_provider_versions:
            raise PackageValidationError("package manifest provider_versions are not normalized")
        if manifest.get("sbom") != "./sbom.json" or "sbom.json" not in files:
            raise PackageValidationError("package manifest SBOM reference is invalid")
    capabilities = manifest.get("capabilities")
    if not isinstance(capabilities, list) or any(
        not isinstance(item, str) or not item for item in capabilities
    ):
        raise PackageValidationError("package capabilities are invalid")
    expected_entry_names = set(str(relative) for relative in files) | {"package-manifest.json"}
    actual_entry_names = {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.relative_to(root).as_posix() != "checksums.json"
    }
    if actual_entry_names != expected_entry_names:
        raise PackageValidationError("package contains files outside its manifest")
    for relative, expected in files.items():
        relative = _safe_relative_path(str(relative), field="manifest file")
        target = root / relative
        if not target.is_file() or target.is_symlink() or not isinstance(expected, dict):
            raise PackageValidationError(f"package entry is missing or malformed: {relative}")
        if expected.get("bytes") != target.stat().st_size or expected.get("sha256") != _sha256_file(target):
            raise PackageValidationError(f"package entry checksum mismatch: {relative}")
    checksums = _load_json(root, "checksums.json")
    if (
        not isinstance(checksums, dict)
        or checksums.get("format") != "agent-knowledge-package-checksums/v1"
        or checksums.get("files")
        != {
            **files,
            "package-manifest.json": {
                "bytes": (root / "package-manifest.json").stat().st_size,
                "sha256": _sha256_file(root / "package-manifest.json"),
            },
        }
    ):
        raise PackageValidationError("checksums.json does not match package manifest")
    package_document = _load_json(root, "knowledge-package.yaml")
    if (
        not isinstance(package_document, dict)
        or package_document.get("format") != _PACKAGE_FORMAT
        or package_document.get("id") != manifest.get("id")
        or package_document.get("version") != manifest.get("version")
        or package_document.get("capabilities") != manifest.get("capabilities")
        or package_document.get("catalog_revision") != manifest.get("catalog_revision")
    ):
        raise PackageValidationError("knowledge-package.yaml is invalid")
    if has_provenance_bundle:
        if package_document.get("provider_versions") != normalized_provider_versions:
            raise PackageValidationError("knowledge-package.yaml provider_versions are invalid")
        if package_document.get("sbom") != {"format": "CycloneDX", "path": "./sbom.json"}:
            raise PackageValidationError("knowledge-package.yaml SBOM reference is invalid")
        sbom = _load_json(root, "sbom.json")
        if sbom != _package_sbom(
            package_id=str(manifest["id"]),
            version=str(manifest["version"]),
            provider_versions=normalized_provider_versions,
        ):
            raise PackageValidationError("package SBOM does not match provider provenance")
    spaces_references = package_document.get("spaces")
    if spaces_references != ["./spaces/index.json"] or "spaces/index.json" not in files:
        raise PackageValidationError("package manifest spaces references are invalid")
    collections_field = "collections" if "collections" in package_document else "datasets"
    collections_file = "collections/index.json" if collections_field == "collections" else "datasets/index.json"
    collections_references = package_document.get(collections_field)
    if collections_references != [f"./{collections_file}"] or collections_file not in files:
        raise PackageValidationError(f"package manifest {collections_field} references are invalid")
    if collections_field == "collections" and "datasets" in package_document:
        raise PackageValidationError("package manifest cannot mix collections and datasets references")
    bindings = package_document.get("bindings")
    indexes = package_document.get("indexes")
    database = package_document.get("database")
    evaluations = package_document.get("evaluations")
    semantic_assets_reference = package_document.get("semantic_assets")
    refs = (
        ("bindings.contract", bindings.get("contract") if isinstance(bindings, dict) else None),
        ("indexes.rebuild_manifest", indexes.get("rebuild_manifest") if isinstance(indexes, dict) else None),
        (
            "semantic_assets.index",
            semantic_assets_reference.get("index") if isinstance(semantic_assets_reference, dict) else None,
        ),
        ("database.index", database.get("index") if isinstance(database, dict) else None),
    )
    for name, reference in refs:
        relative = _safe_relative_path(str(reference or ""), field=f"package manifest {name} reference")
        if relative not in files:
            raise PackageValidationError(f"package manifest references an unknown file: {relative}")
    if not isinstance(bindings, dict) or bindings.get("contract") != "./bindings.example.yaml":
        raise PackageValidationError("package bindings contract reference is invalid")
    if not isinstance(indexes, dict) or indexes.get("rebuild_manifest") != "./indexes/rebuild-manifest.json":
        raise PackageValidationError("package rebuild manifest reference is invalid")
    if not isinstance(semantic_assets_reference, dict) or semantic_assets_reference.get("index") != "./semantics/index.json":
        raise PackageValidationError("package Semantic Asset index reference is invalid")
    if not isinstance(database, dict) or database.get("index") != "./database/index.json":
        raise PackageValidationError("package database index reference is invalid")
    rebuild_manifest = _load_json(root, "indexes/rebuild-manifest.json")
    rebuild_inputs = rebuild_manifest.get("inputs") if isinstance(rebuild_manifest, dict) else None
    if (
        not isinstance(rebuild_manifest, dict)
        or rebuild_manifest.get("format") != "agent-knowledge-index-rebuild/v1"
        or not isinstance(rebuild_inputs, list)
        or any(not isinstance(item, str) for item in rebuild_inputs)
        or len(rebuild_inputs) != len(set(rebuild_inputs))
        or rebuild_inputs != ["assets/index.json", "semantics/index.json", "evidence/index.json", "database/index.json"]
        or any(_safe_relative_path(str(item), field="index rebuild input") not in files for item in rebuild_inputs)
    ):
        raise PackageValidationError("package rebuild manifest inputs are invalid")
    if evaluations != {"root": "./evaluations"}:
        raise PackageValidationError("package evaluations root reference is invalid")
    evaluations_root = _safe_relative_path(
        str(evaluations.get("root") if isinstance(evaluations, dict) else ""),
        field="package manifest evaluations.root reference",
    )
    if not (root / evaluations_root).is_dir() or not any(
        path.relative_to(root).as_posix().startswith(f"{evaluations_root}/")
        for path in root.rglob("*")
        if path.is_file()
    ):
        raise PackageValidationError("package evaluations.root is missing")
    assets_document = _load_json(root, "assets/index.json")
    collections_document = _load_json(root, collections_file)
    spaces_document = _load_json(root, "spaces/index.json")
    semantics_document = _load_json(root, "semantics/index.json")
    database_document = _load_json(root, "database/index.json")
    for document_name, document in (
        ("package-manifest.json", manifest),
        ("knowledge-package.yaml", package_document),
        ("spaces/index.json", spaces_document),
        (collections_file, collections_document),
        ("assets/index.json", assets_document),
        ("semantics/index.json", semantics_document),
        ("database/index.json", database_document),
    ):
        try:
            _portable_value(document, field=document_name, error_type=PackageValidationError)
        except PackageValidationError:
            raise
    assets = assets_document.get("assets") if isinstance(assets_document, dict) else None
    collections = collections_document.get("collections") if isinstance(collections_document, dict) else None
    spaces = spaces_document.get("spaces") if isinstance(spaces_document, dict) else None
    semantic_assets = semantics_document.get("assets") if isinstance(semantics_document, dict) else None
    database_sources = database_document.get("sources") if isinstance(database_document, dict) else None
    if (
        not isinstance(assets, list)
        or not isinstance(collections, list)
        or not isinstance(spaces, list)
        or not isinstance(semantic_assets, list)
        or not isinstance(database_sources, list)
    ):
        raise PackageValidationError("package indexes have invalid shapes")
    if database_document.get("format") != "agent-knowledge-database-index/v1":
        raise PackageValidationError("package database index format is invalid")
    if any(not isinstance(item, dict) for item in spaces):
        raise PackageValidationError("package Space has invalid shape")
    for item in spaces:
        _validated_id(item.get("id"), field="package space.id")
    space_ids = {str(item.get("id")) for item in spaces}
    if len(space_ids) != len(spaces):
        raise PackageValidationError("package Space IDs are not unique")
    if any(not isinstance(item, dict) for item in assets):
        raise PackageValidationError("package Asset has invalid shape")
    for item in assets:
        _validated_id(item.get("id"), field="package asset.id")
        _validated_id(item.get("space_id"), field="package asset.space_id")
    asset_map = {str(item.get("id")): item for item in assets}
    if len(asset_map) != len(assets):
        raise PackageValidationError("package asset IDs are not unique")
    for item in assets:
        if "original_asset_id" in item:
            _validated_id(item["original_asset_id"], field="package asset.original_asset_id")
        if "derivatives" in item and (
            not isinstance(item["derivatives"], dict)
            or any(
                not isinstance(kind, str) or not kind or not isinstance(target, str)
                for kind, target in item["derivatives"].items()
            )
        ):
            raise PackageValidationError("package asset derivatives are invalid")
        if isinstance(item.get("derivatives"), dict):
            for kind, target in item["derivatives"].items():
                _validated_id(target, field=f"package asset derivative {kind}")
        if "published_asset_ids" in item and (
            not isinstance(item["published_asset_ids"], list)
            or any(not isinstance(target, str) for target in item["published_asset_ids"])
            or item["published_asset_ids"] != sorted(set(item["published_asset_ids"]))
        ):
            raise PackageValidationError("package asset published_asset_ids are invalid")
    _validate_asset_relations(asset_map, error_type=PackageValidationError)
    if any(not isinstance(item, dict) for item in semantic_assets):
        raise PackageValidationError("package Semantic Asset has invalid shape")
    for item in semantic_assets:
        _validated_id(item.get("id"), field="package semantic_asset.id")
        _validated_id(item.get("space_id"), field="package semantic_asset.space_id")
    semantic_asset_map = {str(item.get("id")): item for item in semantic_assets}
    if len(semantic_asset_map) != len(semantic_assets):
        raise PackageValidationError("package Semantic Asset IDs are not unique")
    database_source_ids: set[str] = set()
    database_source_order: list[str] = []
    for source in database_sources:
        if not isinstance(source, dict):
            raise PackageValidationError("package database source has invalid shape")
        source_id = _validated_id(source.get("id"), field="package database source.id")
        if set(source) - {"id", "space_id", "dataset_id", "dialect", "ddl", "documentation", "sql_examples", "entities"}:
            raise PackageValidationError(f"package database source {source_id} has an unknown field")
        if source_id in database_source_ids:
            raise PackageValidationError("package database source IDs are not unique")
        database_source_ids.add(source_id)
        database_source_order.append(source_id)
        if _validated_id(source.get("space_id"), field="package database source.space_id") not in space_ids:
            raise PackageValidationError("package database source belongs to an unknown Space")
        _validated_id(source.get("dataset_id"), field="package database source.dataset_id")
        if not isinstance(source.get("dialect"), str) or not source["dialect"].strip():
            raise PackageValidationError("package database source dialect is invalid")
        for field, required in (
            ("ddl", ("content",)),
            ("documentation", ("content",)),
            ("sql_examples", ("question", "sql")),
            ("entities", ("canonical_name", "entity_type", "table_column")),
        ):
            values = source.get(field)
            if not isinstance(values, list):
                raise PackageValidationError(f"package database source {field} is invalid")
            ids: set[str] = set()
            item_order: list[str] = []
            for item in values:
                if not isinstance(item, dict):
                    raise PackageValidationError(f"package database source {field} has an invalid record")
                item_id = _validated_id(item.get("id"), field=f"package database source {field}.id")
                if item_id in ids:
                    raise PackageValidationError(f"package database source {field} IDs are not unique")
                ids.add(item_id)
                item_order.append(item_id)
                allowed_fields = {"id", *required}
                if field == "entities":
                    allowed_fields.add("aliases")
                if set(item) - allowed_fields:
                    raise PackageValidationError(f"package database source {field} has an unknown field")
                if any(not isinstance(item.get(key), str) or not str(item.get(key)).strip() for key in required):
                    raise PackageValidationError(f"package database source {field} has a missing value")
                if field == "entities" and (
                    not isinstance(item.get("aliases", []), list)
                    or any(not isinstance(alias, str) for alias in item.get("aliases", []))
                    or item.get("aliases", []) != sorted(set(item.get("aliases", [])))
                ):
                    raise PackageValidationError("package database entity aliases are invalid")
            if item_order != sorted(item_order):
                raise PackageValidationError(f"package database source {field} is not canonicalized")
    if database_source_order != sorted(database_source_order):
        raise PackageValidationError("package database sources are not canonicalized")
    for semantic_asset in semantic_assets:
        if not isinstance(semantic_asset, dict):
            raise PackageValidationError("package Semantic Asset has invalid shape")
        package_path = _safe_relative_path(
            str(semantic_asset.get("package_path") or ""), field="semantic_asset.package_path"
        )
        if package_path != f"semantics/assets/{semantic_asset.get('id')}.md":
            raise PackageValidationError(f"package Semantic Asset path is not canonical: {semantic_asset.get('id')}")
        if package_path not in files or _sha256_file(root / package_path) != semantic_asset.get("content_digest"):
            raise PackageValidationError(f"package Semantic Asset content mismatch: {semantic_asset.get('id')}")
        if str(semantic_asset.get("space_id")) not in space_ids:
            raise PackageValidationError(f"package Semantic Asset belongs to an unknown Space: {semantic_asset.get('id')}")
        try:
            markdown = (root / package_path).read_text(encoding="utf-8")
            prefix, separator, _body = markdown.partition("\n---\n\n")
            frontmatter = json.loads(prefix.removeprefix("---\n")) if separator else None
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            frontmatter = None
        expected_frontmatter = {
            key: semantic_asset.get(key)
            for key in ("id", "name", "type", "description", "aliases", "tags", "frontmatter")
        }
        if frontmatter != expected_frontmatter:
            raise PackageValidationError(f"package Semantic Asset metadata mismatch: {semantic_asset.get('id')}")
    collection_keys = {
        (str(item.get("id")), str(item.get("version")))
        for item in collections
        if isinstance(item, dict)
    }
    if len(collection_keys) != len(collections):
        raise PackageValidationError("package Collection id/version pairs are not unique")
    for asset in assets:
        if not isinstance(asset, dict) or not is_valid_knowledge_uri(str(asset.get("source_uri") or "")):
            raise PackageValidationError("package asset URI is invalid")
        package_path = _safe_relative_path(str(asset.get("package_path") or ""), field="asset.package_path")
        asset_id = str(asset.get("id"))
        package_path_object = Path(package_path)
        if package_path_object.parent.as_posix() != "assets/originals" or package_path_object.stem != asset_id:
            raise PackageValidationError(f"package Asset path is not canonical: {asset_id}")
        if package_path not in files or _sha256_file(root / package_path) != asset.get("content_digest"):
            raise PackageValidationError(f"package asset content mismatch: {asset.get('id')}")
        if str(asset.get("space_id")) not in space_ids:
            raise PackageValidationError(f"package asset belongs to an unknown Space: {asset.get('id')}")
    for collection in collections:
        if not isinstance(collection, dict):
            raise PackageValidationError("package Collection has invalid shape")
        _validated_id(collection.get("id"), field="package collection.id")
        _validated_id(collection.get("space_id"), field="package collection.space_id")
        if str(collection.get("space_id")) not in space_ids:
            raise PackageValidationError("package Collection has an unknown Space")
        asset_ids = collection.get("asset_ids")
        if not isinstance(asset_ids, list) or any(not isinstance(asset_id, str) for asset_id in asset_ids) or len(asset_ids) != len(set(asset_ids)):
            raise PackageValidationError(f"package Collection has invalid asset_ids: {collection.get('id')}")
        if any(str(asset_id) not in asset_map for asset_id in asset_ids):
            raise PackageValidationError(f"package Collection references an unknown Asset: {collection.get('id')}")
        if any(asset_map[asset_id].get("space_id") != collection.get("space_id") for asset_id in asset_ids):
            raise PackageValidationError(f"package Collection crosses Space boundary: {collection.get('id')}")
        semantic_ids = collection.get("semantic_asset_ids", [])
        if (
            not isinstance(semantic_ids, list)
            or any(not isinstance(semantic_id, str) for semantic_id in semantic_ids)
            or len(semantic_ids) != len(set(semantic_ids))
            or any(semantic_id not in semantic_asset_map for semantic_id in semantic_ids)
            or any(semantic_asset_map[semantic_id].get("space_id") != collection.get("space_id") for semantic_id in semantic_ids)
        ):
            raise PackageValidationError(
                f"package Collection references an unknown or duplicate Semantic Asset: {collection.get('id')}"
            )
        database_ids = collection.get("database_source_ids", [])
        if database_ids is not None:
            if not isinstance(database_ids, list) or any(not isinstance(source_id, str) for source_id in database_ids) or len(database_ids) != len(set(database_ids)):
                raise PackageValidationError(f"package Collection has invalid database_source_ids: {collection.get('id')}")
            for source_id in database_ids:
                source = next((item for item in database_sources if item.get("id") == source_id), None)
                if source is None:
                    raise PackageValidationError(f"package Collection references an unknown database source: {collection.get('id')}")
                if source.get("space_id") != collection.get("space_id"):
                    raise PackageValidationError(f"package Collection crosses database source Space boundary: {collection.get('id')}")
    referenced_semantic_ids = {
        semantic_id for collection in collections for semantic_id in collection.get("semantic_asset_ids", [])
    }
    if referenced_semantic_ids != set(semantic_asset_map):
        raise PackageValidationError("package Semantic Asset references are incomplete")
    entry_digests = {
        relative: str(details["sha256"])
        for relative, details in files.items()
    }
    entry_digests["package-manifest.json"] = _sha256_file(root / "package-manifest.json")
    entry_digests["checksums.json"] = _sha256_file(root / "checksums.json")
    return PackageValidationResult(
        root,
        str(manifest["package_revision"]),
        len(files),
        len(assets),
        tuple(sorted(expected_entry_names | {"checksums.json"})),
        tuple(sorted(entry_digests.items())),
    )


def export_package_zip(package_root: Path, output_zip: Path) -> Path:
    validation = validate_package(package_root)
    package_files = validation.entry_names
    expected_digests = dict(validation.entry_digests)
    destination = output_zip.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise PackageValidationError(f"ZIP output already exists: {destination}")
    _ensure_safe_parent_chain(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = f".{destination.name}.tmp"
    parent_descriptor = _open_safe_parent_directory(destination)
    temporary_descriptor = -1
    try:
        temporary_descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        with os.fdopen(temporary_descriptor, "wb", closefd=True) as stream:
            temporary_descriptor = -1
            with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
                for relative in package_files:
                    info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
                    info.compress_type = zipfile.ZIP_DEFLATED
                    info.external_attr = 0o644 << 16
                    content = _read_secure_relative_file(validation.package_root, relative)
                    if _sha256_bytes(content) != expected_digests[relative]:
                        raise PackageValidationError(f"package entry changed during ZIP export: {relative}")
                    archive.writestr(info, content)
        _publish_noreplace(temporary_name, destination.name, parent_descriptor, directory=False)
    except Exception:
        if temporary_descriptor != -1:
            os.close(temporary_descriptor)
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        raise
    finally:
        os.close(parent_descriptor)
    return destination


def import_package_zip(package_zip: Path, output_dir: Path) -> PackageValidationResult:
    """Safely import and validate a ZIP package into a new directory."""

    archive_path = package_zip.expanduser().absolute()
    if archive_path.is_symlink() or not archive_path.is_file():
        raise PackageValidationError("package ZIP must be a regular file")
    destination = output_dir.expanduser().absolute()
    if destination.exists() or destination.is_symlink():
        raise PackageValidationError(f"package output already exists: {destination}")
    _ensure_safe_parent_chain(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    parent_descriptor = _open_safe_parent_directory(destination)
    temporary: Path | None = None
    try:
        temporary_name = _create_private_temp_directory(parent_descriptor, ".package-import-")
        temporary = destination.parent / temporary_name
        with zipfile.ZipFile(archive_path, "r") as archive:
            members = archive.infolist()
            names: set[str] = set()
            total_size = 0
            for member in members:
                relative = _safe_relative_path(member.filename, field="ZIP entry")
                if relative in names:
                    raise PackageValidationError(f"ZIP contains duplicate entry: {relative}")
                names.add(relative)
                mode = (member.external_attr >> 16) & 0o170000
                if mode == 0o120000:
                    raise PackageValidationError(f"ZIP contains a symlink: {relative}")
                if member.file_size < 0:
                    raise PackageValidationError(f"ZIP entry has an invalid size: {relative}")
                total_size += member.file_size
                if total_size > _MAX_PACKAGE_UNCOMPRESSED_BYTES:
                    raise PackageValidationError("ZIP uncompressed size exceeds the package limit")
                if member.is_dir():
                    _open_or_create_secure_directory(temporary, relative).close()
                    continue
                _write_secure_relative_file(temporary, relative, archive.read(member))
        validation = validate_package(temporary)
        _ensure_safe_parent_chain(destination)
        _publish_noreplace(temporary.name, destination.name, parent_descriptor, directory=True)
        return PackageValidationResult(
            destination,
            validation.package_revision,
            validation.file_count,
            validation.asset_count,
            validation.entry_names,
            validation.entry_digests,
        )
    except Exception:
        if temporary is not None and temporary.exists():
            shutil.rmtree(temporary)
        raise
    finally:
        os.close(parent_descriptor)
