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
_PROCESSING = "processing"
_SQLITE_AUXILIARY = {"catalog.sqlite3-wal", "catalog.sqlite3-shm", "catalog.sqlite3-journal"}
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
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode):
            raise WorkspaceError("state-dir lock is not a regular file")
        os.chmod(lock_path, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
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
    if manifest.get("owner") != "puddingknowledge-local" or type(manifest.get("version")) is not int or manifest.get("version") != 1 or manifest.get("catalog") != "catalog.sqlite3" or manifest.get("wiki_root") != "wiki":
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


class PersistentWorkspace:
    """An owned state directory with its lifecycle lock held."""

    def __init__(self, state_dir: Path, lock_fd: int, payload: dict[str, Any]):
        self.state_dir = state_dir
        self.lock_fd = lock_fd
        self.payload = payload

    def __enter__(self) -> dict[str, Any]:
        return self.payload

    def __exit__(self, _type: object, _value: object, _traceback: object) -> None:
        os.close(self.lock_fd)


def open_persistent_workspace(
    state_dir: Path | str,
    *,
    catalog: Path | None = None,
    wiki_root: Path | None = None,
) -> PersistentWorkspace:
    """Open or atomically initialize a persistent owned workspace."""

    root = _path(state_dir)
    if root == root.parent:
        raise WorkspaceError("state-dir must not be the filesystem root")
    allowed = {_LOCK, _MANIFEST, _INITIALIZING, "catalog.sqlite3", "wiki", _PROCESSING, *_SQLITE_AUXILIARY}
    if root.exists():
        if root.is_symlink() or not root.is_dir():
            raise WorkspaceError("state-dir must be a real directory")
        if any(entry.name not in allowed for entry in root.iterdir()):
            raise WorkspaceError("state-dir contains unexpected or partial entries")
    else:
        root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)
    lock_fd = _open_lock(root)
    try:
        manifest = root / _MANIFEST
        marker = root / _INITIALIZING
        persistent_entries = [root / name for name in ("catalog.sqlite3", "wiki")]
        if marker.exists() or marker.is_symlink():
            raise WorkspaceError("state-dir contains an incomplete initialization")
        allowed = {_LOCK, _MANIFEST, _INITIALIZING, "catalog.sqlite3", "wiki", _PROCESSING, *_SQLITE_AUXILIARY}
        unexpected = [entry for entry in root.iterdir() if entry.name not in allowed]
        if unexpected:
            raise WorkspaceError("state-dir contains unexpected or partial entries")
        if manifest.exists():
            payload = _load_manifest(root)
        elif any(path.exists() or path.is_symlink() for path in persistent_entries):
            raise WorkspaceError("state-dir is partially initialized")
        else:
            if catalog is None or wiki_root is None:
                raise WorkspaceError("first state-dir start requires catalog and wiki-root")
            payload = _initialise(root, catalog, wiki_root)
        return PersistentWorkspace(root, lock_fd, payload)
    except Exception:
        os.close(lock_fd)
        raise
