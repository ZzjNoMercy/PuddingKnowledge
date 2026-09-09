"""Content-addressed immutable local objects with directory-FD anchoring."""
from __future__ import annotations

import errno
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import uuid

_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
MAX_BYTES = 8 * 1024 * 1024
_IDENTITY_NAME = "identity.json"


def _open_directory_tree(root: Path) -> int:
    """Open/create every component without following a symlink."""
    if not root.is_absolute() or root == Path("/") or ".." in root.parts:
        raise ValueError("Object root must be a safe absolute directory")
    current = os.open("/", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for component in root.parts[1:]:
            try:
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            except FileNotFoundError:
                try:
                    os.mkdir(component, mode=0o700, dir_fd=current)
                except FileExistsError:
                    pass
                child = os.open(component, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=current)
            os.close(current)
            current = child
        return current
    except BaseException:
        os.close(current)
        raise


def _read_fd(fd: int, *, limit: int) -> bytes:
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
        raise ValueError("Object file must be a bounded regular file")
    result = bytearray()
    while len(result) <= limit:
        part = os.read(fd, min(65536, limit + 1 - len(result)))
        if not part:
            break
        result.extend(part)
    if len(result) > limit:
        raise ValueError("Object file exceeds size bound")
    return bytes(result)


class LocalObjectStore:
    def __init__(self, root: Path):
        root = root.expanduser()
        if not root.is_absolute():
            raise ValueError("Object root must be absolute")
        self._root_fd = _open_directory_tree(root)
        self.root = root
        try:
            self._identity = self._load_or_create_identity()
        except BaseException:
            self.close()
            raise

    @property
    def identity(self) -> str:
        return self._identity

    @property
    def store_id(self) -> str:
        return self._identity

    def close(self) -> None:
        fd = getattr(self, "_root_fd", -1)
        if fd >= 0:
            self._root_fd = -1
            os.close(fd)

    def __del__(self):
        self.close()

    def __enter__(self) -> "LocalObjectStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _load_or_create_identity(self) -> str:
        try:
            fd = os.open(_IDENTITY_NAME, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root_fd)
        except FileNotFoundError:
            value = str(uuid.uuid4())
            payload = (json.dumps({"store_id": value}, sort_keys=True) + "\n").encode("ascii")
            temporary_name = ".identity-" + uuid.uuid4().hex
            fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=self._root_fd,
            )
            try:
                view = memoryview(payload)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("identity write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            try:
                try:
                    os.link(
                        temporary_name,
                        _IDENTITY_NAME,
                        src_dir_fd=self._root_fd,
                        dst_dir_fd=self._root_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    return self._load_or_create_identity()
                os.fsync(self._root_fd)
                return value
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=self._root_fd)
                except FileNotFoundError:
                    pass
        try:
            raw = _read_fd(fd, limit=512)
        finally:
            os.close(fd)
        try:
            value = json.loads(raw.decode("ascii"))
            identity = value["store_id"] if isinstance(value, dict) else None
            parsed = uuid.UUID(str(identity))
        except (UnicodeError, ValueError, KeyError, TypeError, json.JSONDecodeError) as error:
            raise ValueError("Object store identity is invalid") from error
        return str(parsed)

    def _validate_digest(self, digest: str) -> None:
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValueError("Invalid object digest")

    def _open_object(self, digest: str) -> int:
        self._validate_digest(digest)
        try:
            return os.open(digest[7:], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=self._root_fd)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise ValueError("Object file must not be a symlink") from error
            raise

    def read(self, digest: str) -> bytes:
        fd = self._open_object(digest)
        try:
            data = _read_fd(fd, limit=MAX_BYTES)
        finally:
            os.close(fd)
        if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
            raise ValueError("Object integrity mismatch")
        return data

    def put(self, data: bytes) -> str:
        if not isinstance(data, bytes) or len(data) > MAX_BYTES:
            raise ValueError("Object exceeds size bound")
        digest = "sha256:" + hashlib.sha256(data).hexdigest()
        name = digest[7:]
        try:
            fd = self._open_object(digest)
        except FileNotFoundError:
            fd = None
        if fd is not None:
            try:
                existing = _read_fd(fd, limit=MAX_BYTES)
            finally:
                os.close(fd)
            if "sha256:" + hashlib.sha256(existing).hexdigest() != digest:
                raise ValueError("Object integrity mismatch")
            return digest
        temporary_name = ".object-" + uuid.uuid4().hex
        fd = os.open(temporary_name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=self._root_fd)
        try:
            view = memoryview(data)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("object write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            try:
                os.link(temporary_name, name, src_dir_fd=self._root_fd, dst_dir_fd=self._root_fd, follow_symlinks=False)
            except FileExistsError:
                self.read(digest)
            os.fsync(self._root_fd)
        finally:
            try:
                os.unlink(temporary_name, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass
        return digest
