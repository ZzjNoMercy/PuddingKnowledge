"""Explicit local bindings for Admin upload and Package import staging.

The HTTP contract carries logical identities only.  A host may bind an opaque
upload/package reference to a local file, but the caller never supplies a
filesystem path and the response never returns one.  These services stage
validated bytes; Catalog/index activation remains a separate operation.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import stat
import tempfile
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from knowledge_contracts import Correlation, Evidence, Principal, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.package import PackageValidationError, import_package_zip

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MIME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9.+-]{0,63}$")
_SECRET_RE = re.compile(
    r"(?i)(?:password|secret|token|authorization|cookie|api[_ -]?key|private[_ -]?key)\s*[:=]|"
    r"(?:https?://|file:|[A-Za-z]:[\\/]|\\\\|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/)"
)
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class AssetUploadRequest:
    asset_id: str
    space_id: str
    title: str
    filename: str
    mime_type: str
    binding_id: str
    content_digest: str
    idempotency_key: str

    @classmethod
    def from_mapping(cls, body: Mapping[str, Any]) -> AssetUploadRequest:
        required = {
            "asset_id",
            "space_id",
            "title",
            "filename",
            "mime_type",
            "binding_id",
            "content_digest",
            "idempotency_key",
        }
        if set(body) != required:
            raise ValueError("asset upload fields are invalid")
        values = {key: body.get(key) for key in required}
        if any(not isinstance(value, str) or not value.strip() for value in values.values()):
            raise ValueError("asset upload fields are invalid")
        request = cls(**values)
        if any(not _ID_RE.fullmatch(getattr(request, key)) for key in ("asset_id", "space_id", "binding_id")):
            raise ValueError("asset upload identity is invalid")
        if not _MIME_RE.fullmatch(request.mime_type):
            raise ValueError("asset upload MIME type is invalid")
        if (
            request.filename in {".", ".."}
            or "/" in request.filename
            or "\\" in request.filename
            or len(request.filename) > 255
            or _SECRET_RE.search(request.filename)
        ):
            raise ValueError("asset upload filename is invalid")
        if len(request.title) > 500 or _SECRET_RE.search(request.title):
            raise ValueError("asset upload title is invalid")
        if not _DIGEST_RE.fullmatch(request.content_digest):
            raise ValueError("asset upload digest is invalid")
        if len(request.idempotency_key) > 200 or _SECRET_RE.search(request.idempotency_key):
            raise ValueError("asset upload idempotency key is invalid")
        return request


@dataclass(frozen=True, slots=True)
class PackageImportRequest:
    package_ref: str
    idempotency_key: str

    @classmethod
    def from_mapping(cls, body: Mapping[str, Any]) -> PackageImportRequest:
        if set(body) != {"package_ref", "idempotency_key"}:
            raise ValueError("Package import fields are invalid")
        package_ref = body.get("package_ref")
        idempotency_key = body.get("idempotency_key")
        if (
            not isinstance(package_ref, str)
            or not _ID_RE.fullmatch(package_ref)
            or not isinstance(idempotency_key, str)
            or not idempotency_key.strip()
            or len(idempotency_key) > 200
            or _SECRET_RE.search(idempotency_key)
        ):
            raise ValueError("Package import identity is invalid")
        return cls(package_ref=package_ref, idempotency_key=idempotency_key)


def _error(correlation: Correlation, code: QueryErrorCode, message: str) -> QueryResult:
    return QueryResult(status="error", trace_id=correlation.trace_id, error=QueryError(code=code, message=message))


def _authorized(principal: Principal, space_id: str) -> bool:
    scopes = set(principal.scopes)
    return (
        principal.tenant_id is None
        and bool({"knowledge.admin", "knowledge:admin"} & scopes)
        and bool({f"knowledge.space:{space_id}", f"knowledge:space:{space_id}"} & scopes)
    )


def _read_bound_file(path: Path, *, max_bytes: int) -> bytes:
    if path.is_symlink():
        raise FileNotFoundError("bound source is a symlink")
    descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            raise OSError("bound source is not a regular file")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - total))
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
            if total > max_bytes:
                raise ValueError("bound source exceeds the local upload limit")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _ensure_root(root: Path) -> Path:
    root = root.expanduser().absolute()
    if root.exists() and root.is_symlink():
        raise OSError("staging root must not be a symlink")
    root.mkdir(parents=True, exist_ok=True)
    if root.is_symlink() or not root.is_dir():
        raise OSError("staging root is unavailable")
    return root


def _load_records(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    if path.is_symlink():
        raise OSError("staging record file must not be a symlink")
    records: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        value = json.loads(line)
        if not isinstance(value, dict):
            raise ValueError("staging record is invalid")
        records.append(value)
    return records


def _append_record(path: Path, record: Mapping[str, Any]) -> None:
    if path.exists() and path.is_symlink():
        raise OSError("staging record file must not be a symlink")
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(dict(record), ensure_ascii=False, sort_keys=True) + "\n")
        stream.flush()
        os.fsync(stream.fileno())


class LocalAssetUploadService:
    """Stage one explicitly host-bound local file as a logical Asset."""

    def __init__(self, *, bindings: Mapping[str, Path], staging_root: Path, max_bytes: int = _MAX_UPLOAD_BYTES) -> None:
        if max_bytes <= 0 or max_bytes > _MAX_UPLOAD_BYTES:
            raise ValueError("asset upload limit is invalid")
        self._bindings = {str(key): Path(value).expanduser().absolute() for key, value in bindings.items()}
        self._root = staging_root
        self._max_bytes = max_bytes

    def stage(self, *, principal: Principal, correlation: Correlation, request: AssetUploadRequest) -> QueryResult:
        if not _authorized(principal, request.space_id):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Asset upload requires Admin and Space scope")
        bound = self._bindings.get(request.binding_id)
        if bound is None:
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset upload binding is unavailable")
        try:
            root = _ensure_root(self._root)
            records_path = root / "asset-upload-records.jsonl"
            lock_path = root / ".asset-upload.lock"
            if lock_path.is_symlink():
                raise OSError("staging lock must not be a symlink")
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    records = _load_records(records_path)
                    for record in records:
                        if record.get("idempotency_key") != request.idempotency_key:
                            continue
                        if (
                            record.get("asset_id") != request.asset_id
                            or record.get("space_id") != request.space_id
                            or record.get("content_digest") != request.content_digest
                        ):
                            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "idempotency key was reused with different upload identity")
                        return self._result(correlation, record)
                    content = _read_bound_file(bound, max_bytes=self._max_bytes)
                    digest = "sha256:" + hashlib.sha256(content).hexdigest()
                    if digest != request.content_digest:
                        return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset upload digest does not match the host binding")
                    destination = root / "assets" / request.space_id / f"{request.asset_id}.content"
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    if destination.parent.is_symlink() or destination.is_symlink():
                        raise OSError("asset staging destination must not be a symlink")
                    if destination.exists():
                        existing = _read_bound_file(destination, max_bytes=self._max_bytes)
                        if hashlib.sha256(existing).hexdigest() != digest.removeprefix("sha256:"):
                            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset staging identity is already occupied")
                    else:
                        temporary_name: str | None = None
                        try:
                            with tempfile.NamedTemporaryFile(
                                mode="wb", prefix=".asset-upload-", dir=destination.parent, delete=False
                            ) as temporary:
                                temporary_name = temporary.name
                                temporary.write(content)
                                temporary.flush()
                                os.fsync(temporary.fileno())
                            os.link(temporary_name, destination, follow_symlinks=False)
                        finally:
                            if temporary_name is not None:
                                try:
                                    os.unlink(temporary_name)
                                except FileNotFoundError:
                                    pass
                    record = {
                        "status": "staged",
                        "asset_id": request.asset_id,
                        "space_id": request.space_id,
                        "title": request.title,
                        "filename": request.filename,
                        "mime_type": request.mime_type,
                        "content_digest": request.content_digest,
                        "size_bytes": len(content),
                        "resource_uri": f"knowledge://spaces/{request.space_id}/assets/{request.asset_id}/staged-content",
                        "job_id": "upload_job_" + hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24],
                        "idempotency_key": request.idempotency_key,
                    }
                    _append_record(records_path, record)
                    return self._result(correlation, record)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except ValueError as error:
            return _error(correlation, QueryErrorCode.RESOURCE_LIMIT_EXCEEDED, str(error))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Asset upload staging is unavailable")

    @staticmethod
    def _result(correlation: Correlation, record: Mapping[str, Any]) -> QueryResult:
        data = {key: record[key] for key in ("asset_id", "space_id", "title", "filename", "mime_type", "content_digest", "size_bytes", "resource_uri", "job_id", "status")}
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="Asset 已进入 Platform staging，尚未激活到 Catalog。",
            data={"upload": data},
            evidence=(Evidence(asset_id=str(record["asset_id"]), resource_uri=str(record["resource_uri"]), revision=str(record["content_digest"]), matched_by=("asset_upload", "staged")),),
        )


class LocalPackageImportService:
    """Validate and stage a Package ZIP from an explicit host binding."""

    def __init__(self, *, bindings: Mapping[str, Path], staging_root: Path) -> None:
        self._bindings = {str(key): Path(value).expanduser().absolute() for key, value in bindings.items()}
        self._root = staging_root

    def stage(self, *, principal: Principal, correlation: Correlation, request: PackageImportRequest) -> QueryResult:
        if principal.tenant_id is not None or not ({"knowledge.admin", "knowledge:admin"} & set(principal.scopes)):
            return _error(correlation, QueryErrorCode.PERMISSION_DENIED, "Package import requires Admin scope")
        bound = self._bindings.get(request.package_ref)
        if bound is None or bound.is_symlink() or not bound.is_file():
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Package import binding is unavailable")
        try:
            root = _ensure_root(self._root)
            records_path = root / "package-import-records.jsonl"
            lock_path = root / ".package-import.lock"
            if lock_path.is_symlink():
                raise OSError("staging lock must not be a symlink")
            with lock_path.open("a+", encoding="utf-8") as lock:
                fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
                try:
                    records = _load_records(records_path)
                    for record in records:
                        if record.get("idempotency_key") != request.idempotency_key:
                            continue
                        if record.get("package_ref") != request.package_ref:
                            return _error(correlation, QueryErrorCode.INVALID_REQUEST, "idempotency key was reused with a different Package")
                        return self._result(correlation, record)
                    package_dir = root / "packages" / f"{request.package_ref}-{hashlib.sha256(request.idempotency_key.encode()).hexdigest()[:24]}"
                    validation = import_package_zip(bound, package_dir)
                    manifest = json.loads((validation.package_root / "package-manifest.json").read_text(encoding="utf-8"))
                    if not isinstance(manifest, dict):
                        raise PackageValidationError("Package manifest is invalid")
                    record = {
                        "status": "validated_staged",
                        "package_ref": request.package_ref,
                        "package_id": str(manifest.get("id") or ""),
                        "version": str(manifest.get("version") or ""),
                        "package_revision": validation.package_revision,
                        "file_count": validation.file_count,
                        "asset_count": validation.asset_count,
                        "idempotency_key": request.idempotency_key,
                    }
                    _append_record(records_path, record)
                    return self._result(correlation, record)
                finally:
                    fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        except (PackageValidationError, OSError, ValueError, json.JSONDecodeError):
            return _error(correlation, QueryErrorCode.BINDING_UNAVAILABLE, "Package validation or staging failed")

    @staticmethod
    def _result(correlation: Correlation, record: Mapping[str, Any]) -> QueryResult:
        data = {key: record[key] for key in ("package_ref", "package_id", "version", "package_revision", "file_count", "asset_count", "status")}
        return QueryResult(
            status="ok",
            trace_id=correlation.trace_id,
            answer="Package 已完成校验并进入 staging，尚未切换 active revision。",
            data={"package_import": data},
        )
