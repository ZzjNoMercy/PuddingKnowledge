"""Independent encrypted credentials for the local Knowledge runtime.

The local runtime deliberately owns this small vault implementation. It is
not a compatibility layer for PuddingClaw's runtime identity package: the
home directory, owner namespace, key file and encrypted payload are all
scoped by this module.

Only opaque references leave :class:`LocalCredentialStore`. Values are
stored in an authenticated Fernet envelope and the on-disk representation is
never a JSON document containing credential values.
"""

from __future__ import annotations

import base64
from contextlib import contextmanager
import hashlib
import hmac
import json
import os
import re
import secrets
import stat
import threading
from pathlib import Path
from typing import Any, Final

from cryptography.fernet import Fernet, InvalidToken

try:  # pragma: no cover - all supported deployment runtimes are POSIX
    import fcntl
except ImportError:  # pragma: no cover - keeps import diagnostics portable
    fcntl = None  # type: ignore[assignment]

__all__ = ["CredentialVault", "CredentialVaultError", "LocalCredentialStore"]

_HOME_ENV: Final = "PUDDINGKNOWLEDGE_HOME"
_DEFAULT_HOME: Final = "~/.puddingknowledge"
_FORMAT: Final = "puddingknowledge-local-vault/v1"
_MAX_PLAINTEXT_BYTES: Final = 4 * 1024 * 1024
_MAX_CIPHERTEXT_BYTES: Final = 8 * 1024 * 1024
_MAX_VALUE_BYTES: Final = 1024 * 1024
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class CredentialVaultError(RuntimeError):
    """Raised when an encrypted local vault cannot be opened safely."""


def _as_key(value: bytes | bytearray | memoryview) -> bytes:
    if not isinstance(value, (bytes, bytearray, memoryview)):
        raise TypeError("vault key must be bytes-like")
    key = bytes(value)
    if len(key) < 16:
        raise ValueError("vault key must contain at least 16 bytes")
    return key


class CredentialVault:
    """Authenticated encryption primitive with redacted object representation."""

    __slots__ = ("_key",)

    def __init__(self, key: bytes | bytearray | memoryview) -> None:
        self._key = _as_key(key)

    def _fernet(self, context: str) -> Fernet:
        context_bytes = str(context).encode("utf-8")
        derived = hmac.new(self._key, b"puddingknowledge-local-vault\0" + context_bytes, hashlib.sha256).digest()
        return Fernet(base64.urlsafe_b64encode(derived))

    def encrypt(self, plaintext: bytes | bytearray | memoryview, *, context: str = "default") -> bytes:
        if not isinstance(plaintext, (bytes, bytearray, memoryview)):
            raise TypeError("vault plaintext must be bytes-like")
        return self._fernet(context).encrypt(bytes(plaintext))

    def decrypt(self, ciphertext: bytes | bytearray | memoryview, *, context: str = "default") -> bytes:
        if not isinstance(ciphertext, (bytes, bytearray, memoryview)):
            raise TypeError("vault ciphertext must be bytes-like")
        try:
            return self._fernet(context).decrypt(bytes(ciphertext))
        except InvalidToken as error:
            raise CredentialVaultError("local credential vault could not be decrypted") from error

    seal = encrypt
    open = decrypt

    def __repr__(self) -> str:  # pragma: no cover - defensive secret hygiene
        return "<CredentialVault redacted>"


def _validated_owner(value: Any) -> str:
    owner = str(value or "")
    if not _SAFE_OWNER.fullmatch(owner):
        raise ValueError("owner_user_id must be a safe local identifier")
    return owner


def _absolute_without_resolving(path: Path) -> Path:
    candidate = path.expanduser()
    if not candidate.is_absolute():
        raise ValueError("local vault Home must be an absolute path")
    return candidate.absolute()


def _reject_symlink_ancestors(path: Path) -> None:
    """Reject every existing component, including the final component, if linked."""

    current = path
    components: list[Path] = []
    while current != current.parent:
        components.append(current)
        current = current.parent
    components.append(current)
    for component in reversed(components):
        if component.is_symlink():
            raise ValueError(f"local vault path must not contain a symlink: {component}")


def _bounded_read(path: Path, limit: int, *, label: str) -> bytes:
    _reject_symlink_ancestors(path)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        raise CredentialVaultError(f"local credential vault {label} is unreadable") from error
    try:
        information = os.fstat(descriptor)
        if not stat.S_ISREG(information.st_mode) or information.st_size > limit:
            raise CredentialVaultError(f"local credential vault {label} is invalid")
        chunks: list[bytes] = []
        remaining = limit + 1
        while remaining:
            chunk = os.read(descriptor, min(1024 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        result = b"".join(chunks)
        if len(result) > limit:
            raise CredentialVaultError(f"local credential vault {label} is too large")
        return result
    finally:
        os.close(descriptor)


def _write_all_secure(path: Path, content: bytes, *, mode: int = 0o600) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, mode)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        # fdopen owns the descriptor once entered; this close is harmless if
        # the error happened before ownership was transferred.
        try:
            os.close(descriptor)
        except OSError:
            pass
        raise
    os.chmod(path, mode)


class LocalCredentialStore:
    """Owner- and home-scoped encrypted credential references."""

    __slots__ = (
        "root",
        "owner_user_id",
        "path",
        "_key_path",
        "_lock_path",
        "_context",
        "_vault",
        "_thread_lock",
        "_lock_state",
    )

    def __init__(self, root: str | os.PathLike[str] | None = None, *, owner_user_id: str) -> None:
        self.root = _absolute_without_resolving(
            Path(root if root is not None else os.environ.get(_HOME_ENV, _DEFAULT_HOME))
        )
        _reject_symlink_ancestors(self.root)
        self.owner_user_id = _validated_owner(owner_user_id)
        owner_dir = self.root / "credentials" / self.owner_user_id
        self.path = owner_dir / "vault.enc"
        self._key_path = owner_dir / "vault.key"
        self._lock_path = owner_dir / ".vault.lock"
        self._context = f"{self.root}\0{self.owner_user_id}"
        self._vault: CredentialVault | None = None
        self._thread_lock = threading.RLock()
        self._lock_state = threading.local()

    def _ensure_owner_dir(self) -> None:
        _reject_symlink_ancestors(self.root)
        owner_dir = self.path.parent
        owner_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        _reject_symlink_ancestors(owner_dir)
        try:
            os.chmod(owner_dir, 0o700)
        except OSError:
            pass

    @property
    def vault(self) -> CredentialVault:
        if self._vault is None:
            if getattr(self._lock_state, "depth", 0):
                self._vault = CredentialVault(self._load_or_create_key())
            else:
                with self._exclusive_lock():
                    if self._vault is None:
                        self._vault = CredentialVault(self._load_or_create_key())
        return self._vault

    @vault.setter
    def vault(self, value: CredentialVault) -> None:
        if not isinstance(value, CredentialVault):
            raise TypeError("vault must be an independent CredentialVault")
        self._vault = value

    @property
    def reference_prefix(self) -> str:
        return f"vault://users/{self.owner_user_id}/credentials/"

    def _load_or_create_key(self) -> bytes:
        self._ensure_owner_dir()
        _reject_symlink_ancestors(self._key_path)
        if self._key_path.exists():
            return _as_key(_bounded_read(self._key_path, 128, label="key"))
        key = secrets.token_bytes(32)
        try:
            _write_all_secure(self._key_path, key)
        except FileExistsError:
            return _as_key(_bounded_read(self._key_path, 128, label="key"))
        return key

    def _key_for(self, reference_or_key: str) -> str:
        text = str(reference_or_key or "")
        prefix = self.reference_prefix
        if text.startswith(prefix):
            key = text[len(prefix) :]
        elif "://" in text:
            raise ValueError("credential reference is outside the local owner namespace")
        else:
            key = text
        if not _SAFE_KEY.fullmatch(key):
            raise ValueError("credential key is unsafe")
        return key

    def _reference_for(self, key: str) -> str:
        return self.reference_prefix + self._key_for(key)

    def _entries(self) -> dict[str, str]:
        _reject_symlink_ancestors(self.path)
        if not self.path.exists():
            return {}
        encrypted = _bounded_read(self.path, _MAX_CIPHERTEXT_BYTES, label="payload")
        if not encrypted:
            raise CredentialVaultError("local credential vault payload is invalid")
        try:
            plaintext = self.vault.decrypt(encrypted, context=self._context)
            if len(plaintext) > _MAX_PLAINTEXT_BYTES:
                raise CredentialVaultError("local credential vault payload is too large")
            document = json.loads(plaintext.decode("utf-8"))
        except (CredentialVaultError, UnicodeDecodeError, json.JSONDecodeError, TypeError) as error:
            if isinstance(error, CredentialVaultError):
                raise
            raise CredentialVaultError("local credential vault payload is invalid") from error
        if not isinstance(document, dict) or document.get("format") != _FORMAT or document.get("owner") != self.owner_user_id:
            raise CredentialVaultError("local credential vault metadata is invalid")
        entries = document.get("entries")
        if not isinstance(entries, dict):
            raise CredentialVaultError("local credential vault entries are invalid")
        result: dict[str, str] = {}
        for key, value in entries.items():
            if not isinstance(key, str) or not _SAFE_KEY.fullmatch(key) or not isinstance(value, str):
                raise CredentialVaultError("local credential vault entry is invalid")
            if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
                raise CredentialVaultError("local credential vault entry is too large")
            result[key] = value
        return result

    def _write_entries(self, entries: dict[str, str]) -> None:
        self._ensure_owner_dir()
        document = {"format": _FORMAT, "owner": self.owner_user_id, "entries": dict(sorted(entries.items()))}
        plaintext = json.dumps(document, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        if len(plaintext) > _MAX_PLAINTEXT_BYTES:
            raise CredentialVaultError("local credential vault payload is too large")
        encrypted = self.vault.encrypt(plaintext, context=self._context)
        if len(encrypted) > _MAX_CIPHERTEXT_BYTES:
            raise CredentialVaultError("local credential vault payload is too large")
        _reject_symlink_ancestors(self.path.parent)
        temporary = self.path.with_name(f".{self.path.name}.{secrets.token_hex(8)}.tmp")
        try:
            _write_all_secure(temporary, encrypted)
            if self.path.is_symlink():
                raise CredentialVaultError("local credential vault payload must not be a symlink")
            os.replace(temporary, self.path)
            directory_fd = os.open(self.path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @contextmanager
    def _exclusive_lock(self):
        """Serialize read-modify-write updates without exposing lock contents."""

        if fcntl is None:
            raise CredentialVaultError("local credential vault requires a file-locking platform")
        self._thread_lock.acquire()
        depth = getattr(self._lock_state, "depth", 0)
        if depth:
            self._lock_state.depth = depth + 1
            try:
                yield
            finally:
                self._lock_state.depth = depth
                self._thread_lock.release()
            return

        descriptor: int | None = None
        acquired = False
        try:
            self._ensure_owner_dir()
            _reject_symlink_ancestors(self._lock_path)
            descriptor = os.open(
                self._lock_path,
                os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise CredentialVaultError("local credential vault lock must be a regular file")
            fcntl.flock(descriptor, fcntl.LOCK_EX)
            acquired = True
            self._lock_state.depth = 1
            yield
        finally:
            try:
                if acquired:
                    self._lock_state.depth = 0
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            finally:
                try:
                    if descriptor is not None:
                        os.close(descriptor)
                finally:
                    self._thread_lock.release()

    def put(self, key: str, value: str) -> str:
        normalized = self._key_for(key)
        if not isinstance(value, str):
            raise TypeError("credential value must be text")
        if len(value.encode("utf-8")) > _MAX_VALUE_BYTES:
            raise ValueError("credential value is too large")
        with self._exclusive_lock():
            entries = self._entries()
            entries[normalized] = value
            self._write_entries(entries)
        return self._reference_for(normalized)

    def get(self, reference: str) -> str:
        key = self._key_for(reference)
        return self._entries().get(key, "")

    def delete(self, reference_or_key: str) -> None:
        key = self._key_for(reference_or_key)
        with self._exclusive_lock():
            entries = self._entries()
            if key not in entries:
                return
            del entries[key]
            self._write_entries(entries)

    def __repr__(self) -> str:  # pragma: no cover - defensive secret hygiene
        return f"<LocalCredentialStore owner={self.owner_user_id!r} path={str(self.path)!r}>"
