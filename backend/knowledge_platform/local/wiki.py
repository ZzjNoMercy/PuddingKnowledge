"""Persistent local Wiki compilation services.

The local runtime owns the source binding and the publication receipt.  A
published page is stored as UTF-8 bytes in the same SQLite transaction as the
terminal compilation record; readers therefore never have to reconcile a
filesystem page with a Catalog row after a crash.
"""

from __future__ import annotations

import contextvars
import hashlib
import json
import os
import re
import sqlite3
import stat
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from knowledge_contracts import is_valid_knowledge_uri
from knowledge_platform.wiki.compiler import (
    WikiCompilationRequest,
    WikiCompilationResult,
    WikiCompilationWorker,
)
from knowledge_platform.wiki.local import BoundedWikiContextService, LocalWikiDraftValidator
from knowledge_platform.wiki.ports import (
    RawSnapshot,
    ValidatedWikiDraft,
    WikiCompilationClaim,
)

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_MAX_SOURCE_BYTES = 8 * 1024 * 1024
_MAX_MARKDOWN_BYTES = 8 * 1024 * 1024
_TABLE = "knowledge_local_wiki_compilations"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _digest(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _key_digest(value: str) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > 512:
        raise ValueError("Wiki idempotency key is invalid")
    return _digest(value.encode("utf-8"))


def _fingerprint(
    *, snapshot_id: str, source_revision: str, source_uri: str, content_digest: str
) -> str:
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
    return _digest(encoded)


def _safe_database(path: Path) -> Path:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise FileNotFoundError("Wiki Catalog database is unavailable")
    return path


def _safe_state_root(path: Path) -> Path:
    path = path.expanduser().absolute()
    for parent in (path, *path.parents):
        if parent.exists() and parent.is_symlink():
            raise OSError("Wiki state path contains a symlink")
    if path.exists() and path.is_symlink():
        raise OSError("Wiki state root must not be a symlink")
    path.mkdir(parents=True, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise OSError("Wiki state root is not a directory")
    return path


def _normalize_asset(value: Any) -> tuple[Path, str | None, str | None]:
    configured_revision: str | None = None
    configured_digest: str | None = None
    if isinstance(value, str):
        raw_path = value
    elif isinstance(value, Mapping):
        if set(value) - {"path", "source_revision", "content_digest"}:
            raise ValueError("Wiki asset binding fields are invalid")
        raw_path = value.get("path")
        configured_revision = value.get("source_revision")
        configured_digest = value.get("content_digest")
        if configured_revision is not None and (
            not isinstance(configured_revision, str) or not configured_revision.strip()
        ):
            raise ValueError("Wiki source_revision is invalid")
        if configured_digest is not None and (
            not isinstance(configured_digest, str) or not _DIGEST_RE.fullmatch(configured_digest)
        ):
            raise ValueError("Wiki content_digest is invalid")
    else:
        raise ValueError("Wiki assets must map ids to absolute paths")
    if not isinstance(raw_path, str) or not raw_path.startswith("/"):
        raise ValueError("Wiki asset path must be absolute")
    path = Path(raw_path).expanduser()
    for parent in path.parents:
        if parent.is_symlink():
            raise ValueError("Wiki asset path contains a symlink")
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise ValueError("Wiki asset path must identify a regular file")
    return path, configured_revision, configured_digest


def load_wiki_config(path: Path) -> dict[str, Any]:
    """Load and validate the small, host-bound Wiki service configuration."""

    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise ValueError("Wiki configuration is unavailable")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ValueError("Wiki configuration is invalid") from error
    if (not isinstance(value, dict) or set(value) != {"version", "space_id", "assets", "model"}
            or type(value.get("version")) is not int or value.get("version") != 1):
        raise ValueError("Wiki configuration version must be 1")
    if value.get("space_id") != "space_kb_default":
        raise ValueError("Wiki configuration Space must be space_kb_default")
    assets = value.get("assets")
    if not isinstance(assets, dict) or not assets:
        raise ValueError("Wiki configuration assets are required")
    normalized_assets: dict[str, Any] = {}
    for asset_id, asset in assets.items():
        if not isinstance(asset_id, str) or not _ID_RE.fullmatch(asset_id):
            raise ValueError("Wiki asset id is invalid")
        path_value, revision, digest = _normalize_asset(asset)
        # Keep the normalized path out of diagnostic/repr objects only as far
        # as the runtime needs; it is an explicit local binding, never a URI.
        normalized_assets[asset_id] = {
            "path": str(path_value),
            **({"source_revision": revision} if revision is not None else {}),
            **({"content_digest": digest} if digest is not None else {}),
        }
    model = value.get("model")
    if not isinstance(model, dict):
        raise ValueError("Wiki model configuration is required")
    for field in ("endpoint", "model"):
        if not isinstance(model.get(field), str) or not model[field].strip():
            raise ValueError(f"Wiki model {field} is required")
    if "api_key_env" in model and (
        not isinstance(model["api_key_env"], str) or not model["api_key_env"].strip()
    ):
        raise ValueError("Wiki model api_key_env is invalid")
    # The secret itself must only be resolved by the model adapter from the
    # named environment variable; it is never accepted in this file.
    if any(key.lower() in {"api_key", "token", "secret", "password"} for key in model):
        raise ValueError("Wiki model configuration must use api_key_env")
    return {"version": 1, "space_id": "space_kb_default", "assets": normalized_assets, "model": dict(model)}


class _ConfiguredRawSnapshotRepository:
    def __init__(self, *, assets: Mapping[str, Any], space_id: str, catalog_path: Path) -> None:
        self._assets = dict(assets)
        self._space_id = space_id
        self._catalog_path = _safe_database(catalog_path)

    def _catalog_source(self, snapshot_id: str) -> tuple[str, str, str, str]:
        connection = sqlite3.connect(self._catalog_path)
        try:
            has_assets = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_assets'"
            ).fetchone()
            if has_assets is None:
                raise ValueError("Wiki source Catalog is unavailable")
            row = connection.execute(
                "SELECT space_id, source_uri, revision, content_digest FROM knowledge_assets WHERE id = ?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                raise ValueError("Wiki source Asset is not in the Catalog")
            return tuple(str(item or "") for item in row)  # type: ignore[return-value]
        finally:
            connection.close()

    @staticmethod
    def _read_bounded(path: Path) -> bytes:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise ValueError("Wiki source Asset must be a regular file")
            if metadata.st_size > _MAX_SOURCE_BYTES:
                raise ValueError("Wiki source Asset exceeds size limit")
            result = bytearray()
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, _MAX_SOURCE_BYTES + 1 - len(result)))
                if not chunk:
                    break
                result.extend(chunk)
                if len(result) > _MAX_SOURCE_BYTES:
                    raise ValueError("Wiki source Asset exceeds size limit")
            return bytes(result)
        finally:
            os.close(descriptor)

    async def get(self, *, snapshot_id: str, source_revision: str) -> RawSnapshot:
        configured = self._assets.get(snapshot_id)
        if configured is None:
            raise ValueError("Wiki source Asset is not configured")
        path, expected_revision, expected_digest = _normalize_asset(configured)
        if path.is_symlink() or not path.is_file():
            raise ValueError("Wiki source Asset is unavailable")
        data = self._read_bounded(path)
        digest = _digest(data)
        if expected_digest is not None and digest != expected_digest:
            raise ValueError("Wiki source Asset digest changed")
        revision = expected_revision or digest
        if source_revision != revision:
            raise ValueError("Wiki source revision does not match configured Asset")
        catalog_space, catalog_uri, catalog_revision, catalog_digest = self._catalog_source(snapshot_id)
        source_uri = f"knowledge://spaces/{self._space_id}/assets/{snapshot_id}"
        if (
            catalog_space != self._space_id
            or catalog_uri != source_uri
            or catalog_revision != revision
            or catalog_digest != digest
        ):
            raise ValueError("Wiki source Catalog binding does not match the source snapshot")
        try:
            content = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ValueError("Wiki source Asset must be UTF-8") from error
        return RawSnapshot(snapshot_id, revision, source_uri, content, digest)


class _PersistentWikiStore:
    """Durable claim/publication store with a crash-releasable per-key lock."""

    def __init__(self, *, database_path: Path, state_root: Path, space_id: str) -> None:
        self.database_path = _safe_database(database_path)
        self.state_root = _safe_state_root(state_root)
        self.space_id = space_id
        self._locks: dict[str, Any] = {}
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.database_path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with self._connect() as connection:
            connection.execute(
                f"""CREATE TABLE IF NOT EXISTS {_TABLE} (
                    key_digest TEXT PRIMARY KEY,
                    space_id TEXT NOT NULL,
                    fingerprint TEXT NOT NULL,
                    snapshot_id TEXT NOT NULL,
                    source_revision TEXT NOT NULL,
                    source_uri TEXT NOT NULL,
                    content_digest TEXT NOT NULL,
                    status TEXT NOT NULL,
                    resource_uri TEXT NOT NULL DEFAULT '',
                    markdown BLOB NOT NULL DEFAULT X'',
                    receipt_id TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
            )"""
            )

    @staticmethod
    def _catalog_source_matches(connection: sqlite3.Connection, snapshot: RawSnapshot) -> None:
        row = connection.execute(
            "SELECT space_id, source_uri, revision, content_digest FROM knowledge_assets WHERE id = ?",
            (snapshot.snapshot_id,),
        ).fetchone()
        if row is None or tuple(str(item or "") for item in row) != (
            snapshot.source_uri.removeprefix("knowledge://spaces/").split("/", 2)[0],
            snapshot.source_uri,
            snapshot.source_revision,
            snapshot.content_digest,
        ):
            raise ValueError("Wiki source Catalog changed during compilation")

    def _lock_path(self, key_digest: str) -> Path:
        return self.state_root / ("wiki-" + key_digest.removeprefix("sha256:") + ".lock")

    def _acquire(self, key_digest: str) -> bool:
        try:
            import fcntl
        except ImportError as error:  # pragma: no cover - POSIX local runtime contract
            raise RuntimeError("Wiki compilation requires fcntl locking") from error
        lock_path = self._lock_path(key_digest)
        if lock_path.exists() and lock_path.is_symlink():
            raise OSError("Wiki compilation lock must not be a symlink")
        descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        stream = os.fdopen(descriptor, "a+")
        try:
            if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                raise OSError("Wiki compilation lock must be a regular file")
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            stream.close()
            return False
        except BaseException:
            stream.close()
            raise
        self._locks[key_digest] = stream
        return True

    def _release_lock(self, key_digest: str) -> None:
        stream = self._locks.pop(key_digest, None)
        if stream is not None:
            import fcntl

            try:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)
            finally:
                stream.close()

    async def claim(self, *, request: WikiCompilationRequest) -> WikiCompilationClaim:
        key_digest = _key_digest(request.idempotency_key)
        fingerprint = _fingerprint(
            snapshot_id=request.snapshot_id,
            source_revision=request.source_revision,
            source_uri=request.source_uri,
            content_digest=request.content_digest,
        )
        if not self._acquire(key_digest):
            return WikiCompilationClaim(False)
        try:
            with self._connect() as connection:
                row = connection.execute(f"SELECT * FROM {_TABLE} WHERE key_digest = ?", (key_digest,)).fetchone()
                if row is not None:
                    if row["space_id"] != self.space_id or row["fingerprint"] != fingerprint:
                        raise ValueError("Wiki idempotency key is bound to another request")
                    if row["status"] == "succeeded":
                        uri = str(row["resource_uri"] or "")
                        if not is_valid_knowledge_uri(uri):
                            raise ValueError("Stored Wiki resource URI is invalid")
                        self._release_lock(key_digest)
                        return WikiCompilationClaim(False, uri)
                    if row["status"] != "running":
                        raise ValueError("Stored Wiki compilation state is invalid")
                    connection.execute(
                        f"UPDATE {_TABLE} SET updated_at = ?, status = 'running' WHERE key_digest = ?",
                        (_now(), key_digest),
                    )
                else:
                    connection.execute(
                        f"""INSERT INTO {_TABLE}
                        (key_digest, space_id, fingerprint, snapshot_id, source_revision,
                         source_uri, content_digest, status, created_at, updated_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, 'running', ?, ?)""",
                        (
                            key_digest,
                            self.space_id,
                            fingerprint,
                            request.snapshot_id,
                            request.source_revision,
                            request.source_uri,
                            request.content_digest,
                            _now(),
                            _now(),
                        ),
                    )
            return WikiCompilationClaim(True)
        except Exception:
            self._release_lock(key_digest)
            raise

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        key_digest = _key_digest(idempotency_key)
        with self._connect() as connection:
            row = connection.execute(f"SELECT status, resource_uri FROM {_TABLE} WHERE key_digest = ?", (key_digest,)).fetchone()
            if row is None or row["status"] != "succeeded" or row["resource_uri"] != resource_uri:
                raise ValueError("Wiki publication was not atomically committed")
        self._release_lock(key_digest)

    async def release(self, *, idempotency_key: str) -> None:
        key_digest = _key_digest(idempotency_key)
        try:
            # Keep the fingerprinted running row.  The flock is the live
            # ownership record; retaining the row makes a failed key retryable
            # with the same request while rejecting reuse for another request.
            with self._connect() as connection:
                connection.execute(
                    f"UPDATE {_TABLE} SET updated_at = ? WHERE key_digest = ? AND status = 'running'",
                    (_now(), key_digest),
                )
        finally:
            self._release_lock(key_digest)

    async def publish(
        self,
        draft: ValidatedWikiDraft,
        *,
        snapshot: RawSnapshot,
        idempotency_key: str,
    ) -> str:
        key_digest = _key_digest(idempotency_key)
        markdown = draft.draft.markdown.encode("utf-8")
        if len(markdown) > _MAX_MARKDOWN_BYTES:
            raise ValueError("Wiki draft exceeds size limit")
        fingerprint = _fingerprint(
            snapshot_id=snapshot.snapshot_id,
            source_revision=snapshot.source_revision,
            source_uri=snapshot.source_uri,
            content_digest=snapshot.content_digest,
        )
        output_id = "wiki_" + fingerprint.removeprefix("sha256:")[:48]
        resource_uri = f"knowledge://spaces/{self.space_id}/assets/{output_id}"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._catalog_source_matches(connection, snapshot)
            row = connection.execute(f"SELECT * FROM {_TABLE} WHERE key_digest = ?", (key_digest,)).fetchone()
            expected = fingerprint
            if row is None or row["status"] != "running" or row["fingerprint"] != expected:
                raise ValueError("Wiki publication claim is not owned")
            published_digest = _digest(markdown)
            existing = connection.execute(
                "SELECT space_id, kind, source_type, source_uri, revision, content_digest FROM knowledge_assets WHERE id = ?",
                (output_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """INSERT INTO knowledge_assets
                    (id, space_id, kind, title, description, mime_type, source_type,
                     source_uri, revision, content_digest, permissions_json, metadata_json,
                     created_at, updated_at)
                    VALUES (?, ?, 'wiki_page', ?, '', 'text/markdown', 'local_wiki_compilation',
                            ?, ?, ?, '{}', ?, ?, ?)""",
                    (
                        output_id,
                        self.space_id,
                        draft.draft.title,
                        resource_uri,
                        snapshot.source_revision,
                        published_digest,
                        json.dumps({"published": True, "source_snapshot_id": snapshot.snapshot_id, "receipt_id": draft.receipt_id}, sort_keys=True),
                        _now(),
                        _now(),
                    ),
                )
            elif tuple(str(item or "") for item in existing) != (
                self.space_id,
                "wiki_page",
                "local_wiki_compilation",
                resource_uri,
                snapshot.source_revision,
                published_digest,
            ):
                raise ValueError("Wiki publication Asset is already owned by another page")
            connection.execute(
                f"""UPDATE {_TABLE}
                   SET status = 'succeeded', resource_uri = ?, markdown = ?,
                       receipt_id = ?, updated_at = ?
                 WHERE key_digest = ? AND status = 'running'""",
                (resource_uri, sqlite3.Binary(markdown), draft.receipt_id, _now(), key_digest),
            )
        return resource_uri

    def bindings(self) -> dict[str, bytes]:
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT resource_uri, markdown FROM {_TABLE} WHERE space_id = ? AND status = 'succeeded' ORDER BY resource_uri",
                (self.space_id,),
            ).fetchall()
        return {str(row["resource_uri"]): bytes(row["markdown"]) for row in rows}

    def read(self, resource_uri: str) -> bytes:
        if not is_valid_knowledge_uri(resource_uri):
            raise ValueError("Wiki resource URI is invalid")
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT markdown FROM {_TABLE} WHERE resource_uri = ? AND space_id = ? AND status = 'succeeded'",
                (resource_uri, self.space_id),
            ).fetchone()
        if row is None:
            raise LookupError("Published Wiki resource is unavailable")
        return bytes(row["markdown"])


@dataclass(frozen=True, slots=True)
class WikiServices:
    wiki_compilation: WikiCompilationWorker
    published_bindings: Callable[[], dict[str, bytes]]
    read_published: Callable[[str], bytes]


class _BoundWikiCompilationWorker(WikiCompilationWorker):
    """Carry the full request to the request-aware durable claim port."""

    def __init__(self, *, jobs: "_WorkerJobAdapter", **kwargs: Any) -> None:
        super().__init__(jobs=jobs, **kwargs)
        self._bound_jobs = jobs

    async def compile(self, request: WikiCompilationRequest) -> WikiCompilationResult:
        token = self._bound_jobs.request.set(request)
        ownership_token = self._bound_jobs.owned.set(False)
        try:
            return await super().compile(request)
        except BaseException:
            # The generic worker intentionally catches ordinary Exception;
            # cancellation is a BaseException on supported Python versions.
            # Release here so a cancelled request cannot strand its flock.
            await self._bound_jobs.release(idempotency_key=request.idempotency_key)
            raise
        finally:
            self._bound_jobs.owned.reset(ownership_token)
            self._bound_jobs.request.reset(token)


def build_wiki_services(
    config: Mapping[str, Any], catalog_path: Path, state_root: Path
) -> WikiServices:
    """Build the durable Wiki worker and dynamic publication reader."""

    if not isinstance(config, Mapping) or config.get("version") != 1:
        raise ValueError("Wiki configuration version must be 1")
    if config.get("space_id") != "space_kb_default":
        raise ValueError("Wiki configuration Space must be space_kb_default")
    assets = config.get("assets")
    model_config = config.get("model")
    if not isinstance(assets, Mapping) or not assets or not isinstance(model_config, Mapping):
        raise ValueError("Wiki configuration is incomplete")
    # Import at construction time so a missing model adapter fails clearly and
    # does not make ordinary local query startup import an optional SDK.
    from knowledge_platform.local.wiki_model import HttpWikiModelGateway

    store = _PersistentWikiStore(database_path=catalog_path, state_root=state_root, space_id="space_kb_default")
    jobs = _WorkerJobAdapter(store)
    worker = _BoundWikiCompilationWorker(
        snapshots=_ConfiguredRawSnapshotRepository(
            assets=assets, space_id="space_kb_default", catalog_path=catalog_path
        ),
        context=BoundedWikiContextService(),
        model=HttpWikiModelGateway(dict(model_config)),
        validator=LocalWikiDraftValidator(),
        publisher=store,
        jobs=jobs,
    )
    return WikiServices(worker, store.bindings, store.read)


class _WorkerJobAdapter:
    """Adapt request-aware durable claims to the generic worker port."""

    def __init__(self, store: _PersistentWikiStore) -> None:
        self._store = store
        self.request: contextvars.ContextVar[WikiCompilationRequest | None] = contextvars.ContextVar(
            "wiki_compilation_request", default=None
        )
        self.owned: contextvars.ContextVar[bool] = contextvars.ContextVar(
            "wiki_compilation_claim_owned", default=False
        )

    async def claim(self, *, idempotency_key: str) -> WikiCompilationClaim:
        # The generic port cannot carry the request fingerprint.  The worker
        # installs it immediately before this adapter is called.
        request = self.request.get()
        if request is None:
            raise RuntimeError("Wiki job claim request is not bound")
        self.owned.set(False)
        claim = await self._store.claim(request=request)
        self.owned.set(claim.acquired)
        return claim

    async def complete(self, *, idempotency_key: str, resource_uri: str) -> None:
        if not self.owned.get():
            return
        await self._store.complete(idempotency_key=idempotency_key, resource_uri=resource_uri)
        self.owned.set(False)

    async def release(self, *, idempotency_key: str) -> None:
        if not self.owned.get():
            return
        try:
            await self._store.release(idempotency_key=idempotency_key)
        finally:
            self.owned.set(False)
