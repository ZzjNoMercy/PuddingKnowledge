"""Read-only local provider handle observations for deployment preparation."""

from __future__ import annotations

import hashlib
import re
import socket
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from .deployment import DeploymentArtifact, DeploymentManifest

_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})
_REQUIRED_VECTOR_FIELDS = frozenset({"id", "doc_id", "text", "embedding"})


class LocalProviderObservationError(ValueError):
    """A local provider handle cannot be safely observed."""


@dataclass(frozen=True, slots=True)
class LocalProviderHandle:
    kind: str
    status: str
    resource_id: str
    locator_digest: str

    def __post_init__(self) -> None:
        if self.kind not in {"catalog", "blob", "vector_index", "wiki_root"}:
            raise ValueError("local provider kind is invalid")
        if self.status not in {"observed", "blocked"}:
            raise ValueError("local provider status is invalid")
        if not _ID_RE.fullmatch(self.resource_id):
            raise ValueError("local provider resource id is invalid")
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", self.locator_digest):
            raise ValueError("local provider locator digest is invalid")

    @property
    def ready(self) -> bool:
        return self.status == "observed"

    def to_dict(self) -> dict[str, object]:
        return {
            "kind": self.kind,
            "status": self.status,
            "resource_id": self.resource_id,
            "locator_digest": self.locator_digest,
        }


def _digest_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _safe_file_digest(path: Path) -> str:
    path = path.expanduser().absolute()
    if path.is_symlink() or not path.is_file():
        raise LocalProviderObservationError("local provider file is not a regular file")
    return _digest_bytes(path.read_bytes())


def _safe_tree_digest(root: Path) -> str:
    root = root.expanduser().absolute()
    if root.is_symlink() or not root.is_dir():
        raise LocalProviderObservationError("local provider root is not a regular directory")
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            raise LocalProviderObservationError("local provider root contains a symlink")
        if not path.is_file():
            continue
        relative = path.relative_to(root).as_posix().encode("utf-8")
        content = path.read_bytes()
        digest.update(len(relative).to_bytes(4, "big"))
        digest.update(relative)
        digest.update(len(content).to_bytes(8, "big"))
        digest.update(content)
    return _digest_bytes(digest.digest())


def _validate_vector_config(vector_uri: str, text_collection: str, image_collection: str) -> tuple[str, int]:
    if not isinstance(vector_uri, str) or not vector_uri.strip():
        raise LocalProviderObservationError("vector URI is invalid")
    parsed = urlparse(vector_uri)
    if parsed.scheme not in {"http", "https", "grpc", "grpcs"} or parsed.hostname not in _LOOPBACK_HOSTS:
        raise LocalProviderObservationError("vector probe is limited to loopback")
    if not isinstance(text_collection, str) or not _ID_RE.fullmatch(text_collection):
        raise LocalProviderObservationError("vector text collection is invalid")
    if not isinstance(image_collection, str) or not _ID_RE.fullmatch(image_collection):
        raise LocalProviderObservationError("vector image collection is invalid")
    if text_collection == image_collection:
        raise LocalProviderObservationError("vector collections must differ")
    return parsed.hostname, parsed.port or 19530


def _probe_vector_collections(
    *,
    vector_uri: str,
    text_collection: str,
    image_collection: str,
    probe: Callable[[str, str, str], bool] | None,
) -> bool:
    host, port = _validate_vector_config(vector_uri, text_collection, image_collection)
    if probe is not None:
        return bool(probe(vector_uri, text_collection, image_collection))
    try:
        with socket.create_connection((host, port), timeout=1):
            pass
    except OSError:
        return False
    try:
        from pymilvus import MilvusClient

        client = MilvusClient(uri=vector_uri, timeout=1)
        collections = set(client.list_collections())
        required_collections = {text_collection, image_collection}
        if not required_collections.issubset(collections):
            return False
        for collection_name in required_collections:
            description = client.describe_collection(collection_name=collection_name)
            fields = description.get("fields") if isinstance(description, dict) else None
            if not isinstance(fields, list):
                return False
            field_names = {
                field.get("name")
                for field in fields
                if isinstance(field, dict) and isinstance(field.get("name"), str)
            }
            if not _REQUIRED_VECTOR_FIELDS.issubset(field_names):
                return False
        return True
    except Exception:
        return False


def observe_local_provider_handles(
    *,
    catalog_path: Path,
    wiki_root: Path,
    vector_uri: str,
    text_collection: str,
    image_collection: str,
    vector_probe: Callable[[str, str, str], bool] | None = None,
) -> tuple[LocalProviderHandle, ...]:
    """Observe local inputs without returning physical paths or credentials."""

    handles: list[LocalProviderHandle] = []
    try:
        catalog_digest = _safe_file_digest(catalog_path)
        handles.append(LocalProviderHandle("catalog", "observed", "local-catalog", catalog_digest))
    except LocalProviderObservationError:
        handles.append(LocalProviderHandle("catalog", "blocked", "local-catalog", _digest_bytes(b"blocked-catalog")))
    try:
        wiki_digest = _safe_tree_digest(wiki_root)
        handles.append(LocalProviderHandle("blob", "observed", "local-blob-tree", wiki_digest))
        handles.append(LocalProviderHandle("wiki_root", "observed", "local-wiki-root", wiki_digest))
    except LocalProviderObservationError:
        blocked = _digest_bytes(b"blocked-wiki-root")
        handles.append(LocalProviderHandle("blob", "blocked", "local-blob-tree", blocked))
        handles.append(LocalProviderHandle("wiki_root", "blocked", "local-wiki-root", blocked))
    vector_digest = _digest_bytes(f"{vector_uri}|{text_collection}|{image_collection}".encode())
    try:
        vector_ready = _probe_vector_collections(
            vector_uri=vector_uri,
            text_collection=text_collection,
            image_collection=image_collection,
            probe=vector_probe,
        )
    except LocalProviderObservationError:
        vector_ready = False
    handles.append(
        LocalProviderHandle(
            "vector_index",
            "observed" if vector_ready else "blocked",
            "local-vector-index",
            vector_digest,
        )
    )
    return tuple(sorted(handles, key=lambda handle: handle.kind))


def deployment_manifest_from_provider_handles(
    *,
    deployment_revision: str,
    handles: tuple[LocalProviderHandle, ...],
) -> DeploymentManifest:
    """Build a deployment bundle only from fully observed provider handles."""

    if len(handles) != 4 or {handle.kind for handle in handles} != {
        "catalog",
        "blob",
        "vector_index",
        "wiki_root",
    }:
        raise LocalProviderObservationError("deployment provider handles are incomplete")
    if not all(handle.ready for handle in handles):
        raise LocalProviderObservationError("deployment provider handles are not all observed")
    return DeploymentManifest(
        deployment_revision=deployment_revision,
        artifacts=tuple(
            DeploymentArtifact(
                kind=handle.kind,
                revision=deployment_revision,
                resource_id=handle.resource_id,
                locator_digest=handle.locator_digest,
            )
            for handle in handles
        ),
    )


__all__ = [
    "LocalProviderHandle",
    "LocalProviderObservationError",
    "deployment_manifest_from_provider_handles",
    "observe_local_provider_handles",
]
