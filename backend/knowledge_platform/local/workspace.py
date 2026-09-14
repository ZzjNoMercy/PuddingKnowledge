"""Persistent, process-owned state for the local Knowledge runtime.

The ordinary local CLI creates a disposable snapshot.  ``state-dir`` adds a
small durable boundary around that snapshot: the first start copies the
explicit Catalog and Wiki into an owned directory, and later starts reuse
those owned files without reading the external sources again.
"""
from __future__ import annotations

import fcntl
import json
import os
from pathlib import Path
import secrets
import shutil
import stat
from typing import Any

from knowledge_platform.local.catalog import _MAX_PAGE_BYTES, _materialize_catalog, _safe_pages, _snapshot_catalog
from knowledge_platform.wiki.local import _open_source


_MANIFEST = "workspace.json"
_LOCK = ".workspace.lock"
_INITIALIZING = ".initializing"
_FREEZE = ".workspace-freeze-v1.json"
_PROCESSING = "processing"
_SQLITE_AUXILIARY = {"catalog.sqlite3-wal", "catalog.sqlite3-shm", "catalog.sqlite3-journal",
    "retrieval-traces.sqlite3", "retrieval-traces.sqlite3-wal", "retrieval-traces.sqlite3-shm", "retrieval-traces.sqlite3-journal"}
_MAX_MANIFEST_BYTES = 1024 * 1024


class WorkspaceError(RuntimeError):
    """Raised when a persistent local workspace is unsafe or incomplete."""


def _path(value: Path | str) -> Path:
    candidate = Path(value).expanduser()
    if not candidate.is_absolute():
        raise WorkspaceError("state-dir and source paths must be absolute")
    candidate = candidate.absolute()
    cursor = candidate
    while True:
        if cursor.is_symlink():
            raise WorkspaceError("state-dir contains a symlinked path component")
        if cursor.parent == cursor:
            break
        cursor = cursor.parent
    return candidate


def _regular(path: Path, *, label: str, mode: int | None = None) -> None:
    if path.is_symlink() or not path.exists():
        raise WorkspaceError(f"{label} is missing or symlinked")
    information = path.stat()
    if not stat.S_ISREG(information.st_mode):
        raise WorkspaceError(f"{label} must be a regular file")
    if information.st_mode & 0o077:
        raise WorkspaceError(f"{label} permissions are too broad")
    if mode is not None and information.st_size > mode:
        raise WorkspaceError(f"{label} is too large")


def _read_json(path: Path, *, label: str) -> dict[str, Any]:
    _regular(path, label=label, mode=_MAX_MANIFEST_BYTES)
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_size > _MAX_MANIFEST_BYTES:
            raise WorkspaceError(f"{label} is invalid")
        payload = b""
        while len(payload) <= _MAX_MANIFEST_BYTES:
            chunk = os.read(descriptor, min(65536, _MAX_MANIFEST_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        if len(payload) > _MAX_MANIFEST_BYTES:
            raise WorkspaceError(f"{label} is too large")
    finally:
        os.close(descriptor)
    try:
        result = json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as error:
        raise WorkspaceError(f"{label} is invalid JSON") from error
    if not isinstance(result, dict):
        raise WorkspaceError(f"{label} must contain an object")
    return result


def _write_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(8)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", closefd=True) as stream:
            json.dump(value, stream, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    finally:
        temporary.unlink(missing_ok=True)


def _open_lock(state_dir: Path) -> int:
    lock_path = state_dir / _LOCK
    if lock_path.exists() and (lock_path.is_symlink() or not lock_path.is_file()):
        raise WorkspaceError("state-dir lock is not a regular file")
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_nlink != 1 or information.st_uid != os.getuid() or information.st_mode & 0o077:
            raise WorkspaceError("state-dir lock must be private, owned and unlinked")
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        current=lock_path.stat(follow_symlinks=False)
        if (information.st_dev,information.st_ino)!=(current.st_dev,current.st_ino):
            raise WorkspaceError("state-dir lock changed during admission")
    except (OSError, WorkspaceError) as error:
        os.close(descriptor)
        if isinstance(error, WorkspaceError):
            raise
        raise WorkspaceError("state-dir is already owned by another process") from error
    return descriptor


def _validate_source_catalog(path: Path) -> Path:
    source = _path(path)
    if source.is_symlink() or not source.is_file() or not stat.S_ISREG(source.stat().st_mode):
        raise WorkspaceError("Catalog source must be a regular file")
    return source


def _copy_wiki(source: Path, destination: Path) -> list[Path]:
    pages = _safe_pages(_path(source))
    destination.mkdir(mode=0o700)
    copied: list[Path] = []
    for page in pages:
        relative = page.relative_to(_path(source))
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with _open_source(page, max_bytes=_MAX_PAGE_BYTES) as (stream, _size):
            with target.open("wb") as output:
                shutil.copyfileobj(stream, output)
        os.chmod(target, 0o600)
        copied.append(target)
    return copied


def _relative_binding(root: Path, value: Path) -> str:
    try:
        relative = value.relative_to(root)
    except ValueError as error:
        raise WorkspaceError("file binding escaped owned Wiki") from error
    if relative.is_absolute() or ".." in relative.parts:
        raise WorkspaceError("file binding escaped owned Wiki")
    return relative.as_posix()


def _initialise(state_dir: Path, catalog: Path, wiki_root: Path) -> dict[str, Any]:
    source_catalog = _validate_source_catalog(catalog)
    source_wiki = _path(wiki_root)
    _safe_pages(source_wiki)
    marker = state_dir / _INITIALIZING
    marker_fd = os.open(marker, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
    os.close(marker_fd)
    staging = state_dir / f".staging-{secrets.token_hex(12)}"
    owned_catalog = state_dir / "catalog.sqlite3"
    owned_wiki = state_dir / "wiki"
    try:
        staging.mkdir(mode=0o700)
        staged_source = staging / "source.sqlite3"
        staged_catalog = staging / "catalog.sqlite3"
        staged_wiki = staging / "wiki"
        _snapshot_catalog(source_catalog, staged_source)
        os.chmod(staged_source, 0o600)
        _copy_wiki(source_wiki, staged_wiki)
        materialized = _materialize_catalog(staged_source, staged_catalog, staged_wiki)
        os.chmod(staged_catalog, 0o600)
        os.replace(staged_catalog, owned_catalog)
        os.replace(staged_wiki, owned_wiki)
        bindings = {
            asset_id: _relative_binding(staged_wiki, path)
            for asset_id, path in materialized["file_bindings"].items()
        }
        _write_json(state_dir / _MANIFEST, {
            "version": 1,
            "owner": "puddingknowledge-local",
            "catalog": owned_catalog.name,
            "wiki_root": owned_wiki.name,
            "pages": materialized["pages"],
            "collection_version": materialized["collection_version"],
            "file_bindings": bindings,
        })
        marker.unlink()
        return _load_manifest(state_dir)
    except Exception:
        # Keep the marker as durable evidence of an incomplete initialization;
        # a later process must fail closed instead of treating partial files as
        # a valid persistent workspace.
        raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _load_manifest(state_dir: Path) -> dict[str, Any]:
    marker = state_dir / _INITIALIZING
    if marker.exists() or marker.is_symlink():
        raise WorkspaceError("state-dir contains an incomplete initialization")
    manifest = _read_json(state_dir / _MANIFEST, label="state-dir manifest")
    if manifest.get("owner") != "puddingknowledge-local" or type(manifest.get("version")) is not int:
        raise WorkspaceError("state-dir manifest is invalid")
    if manifest.get("version") == 4 and manifest.get("kind") == "migrated_knowledge":
        from .combined_workspace import load_combined_workspace
        try:
            return load_combined_workspace(state_dir, manifest)
        except (ValueError, OSError, RuntimeError) as error:
            raise WorkspaceError(str(error)) from error
    if manifest.get("version") in (3, 5, 6, 7) and manifest.get("kind") == "migrated_wiki":
        from .migrated_wiki import load_migrated_wiki_workspace
        try:
            return load_migrated_wiki_workspace(state_dir, manifest)
        except (ValueError, OSError, RuntimeError) as error:
            raise WorkspaceError(str(error)) from error
    if manifest.get("version") in (2, 3) and manifest.get("kind") == "migrated_documents":
        for name in _SQLITE_AUXILIARY:
            auxiliary = state_dir / name
            if auxiliary.exists() or auxiliary.is_symlink():
                _regular(auxiliary, label="owned SQLite auxiliary")
                if auxiliary.stat().st_nlink != 1:
                    raise WorkspaceError("owned SQLite auxiliary is hardlinked")
        processing = state_dir / _PROCESSING
        if (processing.exists() or processing.is_symlink()) and (processing.is_symlink() or not processing.is_dir()):
            raise WorkspaceError("state-dir processing root must be a real directory")
        from .migrated_documents import load_migrated_workspace
        try:
            return load_migrated_workspace(state_dir, manifest)
        except (ValueError, OSError, RuntimeError) as error:
            raise WorkspaceError(str(error)) from error
    if manifest.get("version") != 1 or manifest.get("catalog") != "catalog.sqlite3" or manifest.get("wiki_root") != "wiki":
        raise WorkspaceError("state-dir manifest is invalid")
    catalog = state_dir / "catalog.sqlite3"
    wiki_root = state_dir / "wiki"
    processing = state_dir / _PROCESSING
    if processing.exists() or processing.is_symlink():
        if processing.is_symlink() or not processing.is_dir():
            raise WorkspaceError("state-dir processing root must be a real directory")
    for name in _SQLITE_AUXILIARY:
        auxiliary = state_dir / name
        if auxiliary.exists() or auxiliary.is_symlink():
            _regular(auxiliary, label=f"owned SQLite auxiliary {name}")
    _regular(catalog, label="owned Catalog")
    if wiki_root.is_symlink() or not wiki_root.is_dir():
        raise WorkspaceError("owned Wiki must be a real directory")
    pages = _safe_pages(wiki_root)
    raw_bindings = manifest.get("file_bindings")
    if not isinstance(raw_bindings, dict):
        raise WorkspaceError("state-dir bindings are invalid")
    file_bindings: dict[str, Path] = {}
    for asset_id, relative_value in raw_bindings.items():
        if not isinstance(asset_id, str) or not isinstance(relative_value, str):
            raise WorkspaceError("state-dir binding is invalid")
        relative = Path(relative_value)
        if relative.is_absolute() or ".." in relative.parts:
            raise WorkspaceError("state-dir binding escaped owned Wiki")
        target = wiki_root / relative
        if target.is_symlink() or not target.is_file() or not stat.S_ISREG(target.stat().st_mode):
            raise WorkspaceError("state-dir binding target is invalid")
        file_bindings[asset_id] = target
    if not file_bindings or not pages:
        raise WorkspaceError("state-dir contains no owned Wiki pages")
    return {
        "catalog": catalog,
        "wiki_root": wiki_root,
        "file_bindings": file_bindings,
        "pages": int(manifest.get("pages", len(pages))),
        "collection_version": str(manifest.get("collection_version", "")),
    }


def _allowed_entries():
    return {".workspace-authority-v1.json", ".workspace-authority-v1.json.part", _LOCK, _MANIFEST, _INITIALIZING, "catalog.sqlite3", "wiki", "blobs", "wiki-evidence", "wiki-schema.json", _PROCESSING, *_SQLITE_AUXILIARY}


class PersistentWorkspace:
    """An owned state directory with its lifecycle lock held."""

    def __init__(self, state_dir: Path, lock_fd: int, payload: dict[str, Any], authority_fd: int | None = None):
        self.state_dir = state_dir
        self.lock_fd = lock_fd
        self.authority_fd = authority_fd
        self.payload = payload

    def __enter__(self) -> dict[str, Any]:
        return self.payload

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        if self.authority_fd is not None:
            os.close(self.authority_fd)
            self.authority_fd = None
        os.close(self.lock_fd)


def _check_not_frozen(root):
    for name in (_FREEZE, _FREEZE + ".part"):
        try:(root/name).lstat()
        except FileNotFoundError:continue
        raise WorkspaceError("state-dir is persistently frozen")


def open_persistent_workspace(
    state_dir: Path | str,
    *,
    catalog: Path | None = None,
    wiki_root: Path | None = None,
    document_migration: Path | None = None,
    wiki_archive: Path | None = None,
    schema_evidence: Path | None = None,
) -> PersistentWorkspace:
    """Open or atomically initialize a persistent owned workspace."""

    if schema_evidence is not None and wiki_archive is None:
        raise WorkspaceError("schema evidence requires Wiki archive bootstrap")
    if wiki_archive is not None and any(v is not None for v in (catalog, wiki_root)):
        raise WorkspaceError("Wiki archive requires its own new workspace")
    if document_migration is not None and (catalog is not None or wiki_root is not None):
        raise WorkspaceError("document migration cannot accompany Catalog/Wiki inputs")
    root = _path(state_dir)
    from .writer_authority import load_binding, acquire_writer
    # Validate enrolled/partial authority before chmod or business initialization.
    load_binding(root)
    if root == root.parent:
        raise WorkspaceError("state-dir must not be the filesystem root")
    allowed = _allowed_entries()
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceError("state-dir must be a real directory")
        _check_not_frozen(root)
        if any(entry.name not in allowed for entry in root.iterdir()):
            raise WorkspaceError("state-dir contains unexpected or partial entries")
    else:
        root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_fd = _open_lock(root)
    authority_fd = None
    try:
        _check_not_frozen(root)
        authority_fd = acquire_writer(root)
        manifest = root / _MANIFEST
        marker = root / _INITIALIZING
        persistent_entries = [root / name for name in ("catalog.sqlite3", "wiki", "blobs", "wiki-evidence", "wiki-schema.json", "retrieval-traces.sqlite3")]
        if marker.exists() or marker.is_symlink():
            raise WorkspaceError("state-dir contains an incomplete initialization")
        allowed = _allowed_entries()
        unexpected = [entry for entry in root.iterdir() if entry.name not in allowed]
        if unexpected:
            raise WorkspaceError("state-dir contains unexpected or partial entries")
        if manifest.exists():
            if document_migration is not None or wiki_archive is not None:
                raise WorkspaceError("migration only initializes a new workspace")
            payload = _load_manifest(root)
        elif any(path.exists() or path.is_symlink() for path in persistent_entries):
            raise WorkspaceError("state-dir is partially initialized")
        else:
            if wiki_archive is not None and document_migration is not None:
                from .combined_workspace import bootstrap_combined_workspace
                try:
                    bootstrap_combined_workspace(document_migration, wiki_archive, root, schema_evidence=schema_evidence)
                except Exception as error:
                    raise WorkspaceError(str(error)) from error
                payload = _load_manifest(root)
            elif wiki_archive is not None:
                from .migrated_wiki import bootstrap_migrated_wiki
                try:
                    bootstrap_migrated_wiki(wiki_archive, root, schema_evidence=schema_evidence)
                except Exception as error:
                    raise WorkspaceError(str(error)) from error
                payload = _load_manifest(root)
            elif document_migration is not None:
                from .migrated_documents import bootstrap_migrated_documents
                try:
                    bootstrap_migrated_documents(document_migration, root)
                except Exception as error:
                    raise WorkspaceError(str(error)) from error
                payload = _load_manifest(root)
            else:
                if catalog is None or wiki_root is None:
                    raise WorkspaceError("first state-dir start requires catalog and wiki-root")
                payload = _initialise(root, catalog, wiki_root)
        return PersistentWorkspace(root, lock_fd, payload, authority_fd)
    except BaseException:
        if authority_fd is not None:os.close(authority_fd)
        os.close(lock_fd)
        raise
