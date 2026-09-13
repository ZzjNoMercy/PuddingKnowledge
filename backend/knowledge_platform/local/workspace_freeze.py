"""Persistently freeze an already-owned local workspace.

This module is deliberately narrower than workspace initialization.  It only
accepts an existing, valid ``workspace.json`` and publishes one immutable
private marker while holding the normal workspace lock.  The marker is an
observation/fence record; it never authorizes activation.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Callable

from . import workspace


FORMAT = "puddingknowledge-workspace-freeze/v1"
FREEZE_NAME = ".workspace-freeze-v1.json"
PART_NAME = FREEZE_NAME + ".part"
MAX_BYTES = 4096
_OPERATION_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}\Z")


def _canonical(value: dict[str, Any]) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n").encode("utf-8")


def _has(path: Path) -> bool:
    return path.exists() or path.is_symlink()


def _owned_root(value: Path | str) -> Path:
    root = workspace._path(value)
    if root.parent == root or root.is_symlink() or not root.is_dir():
        raise workspace.WorkspaceError("state-dir must be an existing real directory")
    info = root.stat()
    if info.st_uid != os.getuid() or not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
        raise workspace.WorkspaceError("state-dir must be private and owned")
    if not (root / "workspace.json").exists():
        raise workspace.WorkspaceError("state-dir manifest is required")
    return root


def _read_record(path: Path, *, links: int, limit: int = MAX_BYTES) -> tuple[bytes, os.stat_result]:
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError as error:
        raise workspace.WorkspaceError("freeze record is unavailable") from error
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != links
            or info.st_mode & 0o077
            or info.st_size > limit
        ):
            raise workspace.WorkspaceError("freeze record must be private and bounded")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise workspace.WorkspaceError("freeze record is too large")
        current = os.stat(path, follow_symlinks=False)
        if (info.st_dev, info.st_ino, info.st_size) != (current.st_dev, current.st_ino, current.st_size):
            raise workspace.WorkspaceError("freeze record changed")
        return bytes(data), info
    finally:
        os.close(descriptor)


def _sync_directory(root: Path) -> None:
    descriptor = os.open(root, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _validate_record(data: bytes, expected: bytes) -> None:
    if data != expected:
        raise workspace.WorkspaceError("freeze operation or state identity changed")


def freeze_workspace(
    root: Path | str,
    operation_id: str,
    *,
    _after_part: Callable[[], None] | None = None,
    _after_link: Callable[[], None] | None = None,
) -> dict[str, Any]:
    """Freeze an existing persistent workspace and return a path-free receipt."""
    if not isinstance(operation_id, str) or not _OPERATION_RE.fullmatch(operation_id):
        raise ValueError("Invalid freeze operation ID")
    state_dir = _owned_root(root)
    lock_fd = workspace._open_lock(state_dir)
    try:
        # This is intentionally read-only validation: no bootstrap arguments,
        # catalog writes, or manifest updates are possible on this path.
        target = state_dir / FREEZE_NAME
        part = state_dir / PART_NAME
        manifest_bytes, _ = _read_record(state_dir / "workspace.json", links=1, limit=workspace._MAX_MANIFEST_BYTES)
        # Domain loaders reject control markers as unexpected entries. Validate
        # before first publication; retries bind exact original manifest bytes.
        if not _has(target) and not _has(part):workspace._load_manifest(state_dir)
        if _read_record(state_dir / "workspace.json", links=1, limit=workspace._MAX_MANIFEST_BYTES)[0] != manifest_bytes:
            raise workspace.WorkspaceError("workspace manifest changed during freeze validation")
        identity = state_dir.stat()
        value = {
            "activation_allowed": False,
            "directory_identity": {"device": identity.st_dev, "inode": identity.st_ino},
            "format": FORMAT,
            "operation_id": operation_id,
            "workspace_manifest_sha256": hashlib.sha256(manifest_bytes).hexdigest(),
            "root_path_sha256": hashlib.sha256(str(state_dir).encode("utf-8")).hexdigest(),
            "state": "workspace_frozen",
        }
        encoded = _canonical(value)
        target = state_dir / FREEZE_NAME
        part = state_dir / PART_NAME

        target_present = _has(target)
        part_present = _has(part)
        if target_present:
            target_data, target_info = _read_record(target, links=2 if part_present else 1)
            _validate_record(target_data, encoded)
            if part_present:
                part_data, part_info = _read_record(part, links=2)
                _validate_record(part_data, encoded)
                if (target_info.st_dev, target_info.st_ino) != (part_info.st_dev, part_info.st_ino):
                    raise workspace.WorkspaceError("freeze publication link pair mismatch")
                part.unlink()
                _sync_directory(state_dir)
        else:
            if part_present:
                part_data, _ = _read_record(part, links=1)
                _validate_record(part_data, encoded)
            else:
                try:
                    descriptor = os.open(
                        part,
                        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                        0o600,
                    )
                except OSError as error:
                    raise workspace.WorkspaceError("freeze publication staging is unavailable") from error
                try:
                    offset = 0
                    while offset < len(encoded):
                        offset += os.write(descriptor, encoded[offset:])
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                _sync_directory(state_dir)
                if _after_part is not None:
                    _after_part()
            os.link(part, target)
            _sync_directory(state_dir)
            if _after_link is not None:
                _after_link()
            part.unlink()
            _sync_directory(state_dir)

        _sync_directory(state_dir)
        final, _ = _read_record(target, links=1)
        _validate_record(final, encoded)
        if _read_record(state_dir / "workspace.json", links=1, limit=workspace._MAX_MANIFEST_BYTES)[0] != manifest_bytes:
            raise workspace.WorkspaceError("workspace manifest changed during freeze publication")
        receipt = dict(value)
        receipt["receipt_sha256"] = hashlib.sha256(encoded).hexdigest()
        return receipt
    finally:
        os.close(lock_fd)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--operation-id", required=True)
    args = parser.parse_args(argv)
    try:
        result = freeze_workspace(args.state_dir, args.operation_id)
    except Exception:
        print(json.dumps({
            "activation_allowed": False,
            "error_code": "workspace_freeze_rejected",
            "format": FORMAT,
            "status": "error",
        }, sort_keys=True))
        return 1
    print(json.dumps({"status": "workspace_frozen", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
