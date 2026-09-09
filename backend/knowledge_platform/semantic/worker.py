"""Durable worker boundary for Semantic Dimension Processing."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class SemanticDimensionBuildInput:
    job_id: str
    space_id: str
    dimension_id: str
    adapter: str
    source_snapshot: tuple[Mapping[str, str], ...]
    publish_after_confirmation: bool = False


@dataclass(frozen=True, slots=True)
class SemanticDimensionBuildArtifact:
    staging_uri: str
    staging_digest: str
    result_summary: Mapping[str, object]

    def __post_init__(self) -> None:
        if not self.staging_uri.startswith("knowledge://") or not _DIGEST_RE.fullmatch(self.staging_digest):
            raise ValueError("semantic dimension staging artifact is invalid")
        if not isinstance(self.result_summary, Mapping):
            raise ValueError("semantic dimension result summary is invalid")


@dataclass(frozen=True, slots=True)
class SemanticDimensionPublication:
    published_uri: str
    published_digest: str

    def __post_init__(self) -> None:
        if not self.published_uri.startswith("knowledge://") or not _DIGEST_RE.fullmatch(self.published_digest):
            raise ValueError("semantic dimension publication is invalid")


@dataclass(frozen=True, slots=True)
class SemanticDimensionBuildClaim:
    acquired: bool
    owner: str = ""
    build_input: SemanticDimensionBuildInput | None = None


class SemanticDimensionBuilder(Protocol):
    def build(self, build_input: SemanticDimensionBuildInput) -> SemanticDimensionBuildArtifact:
        """Build a path-free deterministic staging artifact."""


class SemanticDimensionPublisher(Protocol):
    def publish(
        self, build_input: SemanticDimensionBuildInput, artifact: SemanticDimensionBuildArtifact
    ) -> SemanticDimensionPublication: ...


class SemanticDimensionJobStore(Protocol):
    def claim(self, *, job_id: str, space_id: str) -> SemanticDimensionBuildClaim: ...

    def complete(
        self,
        *,
        job_id: str,
        owner: str,
        build_input: SemanticDimensionBuildInput,
        artifact: SemanticDimensionBuildArtifact,
        publication: SemanticDimensionPublication | None = None,
    ) -> None: ...

    def release(self, *, job_id: str, owner: str) -> None: ...


class SemanticDimensionBuildError(RuntimeError):
    """A dimension build did not reach the human-decision boundary."""


class SemanticDimensionBuildWorker:
    """Claim one queued Platform job and stop at auditable staging."""

    def __init__(
        self,
        *,
        builder: SemanticDimensionBuilder,
        jobs: SemanticDimensionJobStore,
        publisher: SemanticDimensionPublisher | None = None,
    ) -> None:
        self._builder = builder
        self._jobs = jobs
        self._publisher = publisher

    def process(self, *, job_id: str, space_id: str) -> SemanticDimensionBuildArtifact:
        claim = self._jobs.claim(job_id=job_id, space_id=space_id)
        if not claim.acquired or claim.build_input is None:
            raise SemanticDimensionBuildError("semantic dimension job is not claimable")
        try:
            artifact = self._builder.build(claim.build_input)
            publication = None
            if claim.build_input.publish_after_confirmation:
                if self._publisher is None:
                    raise SemanticDimensionBuildError("semantic dimension publisher is unavailable")
                publication = self._publisher.publish(claim.build_input, artifact)
            self._jobs.complete(
                job_id=job_id,
                owner=claim.owner,
                build_input=claim.build_input,
                artifact=artifact,
                publication=publication,
            )
            return artifact
        except Exception:
            self._jobs.release(job_id=job_id, owner=claim.owner)
            raise


class LocalSemanticDimensionBuilder:
    """Deterministic local staging builder; no model, Graph, or source bytes."""

    def build(self, build_input: SemanticDimensionBuildInput) -> SemanticDimensionBuildArtifact:
        if not _ID_RE.fullmatch(build_input.job_id) or not _ID_RE.fullmatch(build_input.space_id) or not _ID_RE.fullmatch(build_input.dimension_id) or not _ID_RE.fullmatch(build_input.adapter):
            raise ValueError("semantic dimension build identity is invalid")
        if not build_input.source_snapshot:
            raise ValueError("semantic dimension source snapshot is empty")
        normalized: list[dict[str, str]] = []
        for item in build_input.source_snapshot:
            if set(item) != {"asset_id", "content_digest"} or not _ID_RE.fullmatch(str(item["asset_id"])) or not _DIGEST_RE.fullmatch(str(item["content_digest"])):
                raise ValueError("semantic dimension source snapshot is invalid")
            normalized.append({"asset_id": str(item["asset_id"]), "content_digest": str(item["content_digest"])})
        encoded = json.dumps(
            {
                "adapter": build_input.adapter,
                "dimension_id": build_input.dimension_id,
                "job_id": build_input.job_id,
                "space_id": build_input.space_id,
                "source_snapshot": normalized,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        return SemanticDimensionBuildArtifact(
            staging_uri=f"knowledge://spaces/{build_input.space_id}/semantic-dimensions/{build_input.dimension_id}/staging",
            staging_digest=digest,
            result_summary={"adapter": build_input.adapter, "source_count": len(normalized), "source_snapshot": normalized},
        )


class LocalSemanticDimensionPublisher:
    """Publish a path-free deterministic semantic result into an isolated root."""

    def __init__(self, *, root: Path) -> None:
        self._root = root.expanduser().absolute()
        self.publish_count = 0

    def _append_manifest(self, record: Mapping[str, object]) -> None:
        self._root.mkdir(parents=True, exist_ok=True)
        manifest = self._root / "publication-manifest.jsonl"
        lock_path = self._root / ".publication-manifest.lock"
        if self._root.is_symlink() or manifest.is_symlink() or lock_path.is_symlink():
            raise OSError("semantic publication paths must not be symlinks")
        with lock_path.open("a+", encoding="utf-8") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                prior = manifest.read_text(encoding="utf-8") if manifest.is_file() else ""
                manifest.write_text(prior + json.dumps(dict(record), sort_keys=True) + "\n", encoding="utf-8")
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    def publish(
        self, build_input: SemanticDimensionBuildInput, artifact: SemanticDimensionBuildArtifact
    ) -> SemanticDimensionPublication:
        encoded = json.dumps(
            {
                "adapter": build_input.adapter,
                "dimension_id": build_input.dimension_id,
                "source_snapshot": [dict(item) for item in build_input.source_snapshot],
                "staging_digest": artifact.staging_digest,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(encoded).hexdigest()
        target = self._root / "published" / build_input.space_id / f"{build_input.dimension_id}-{digest.removeprefix('sha256:')[:16]}.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.is_symlink():
            raise OSError("semantic publication destination must not be a symlink")
        if target.exists():
            if target.read_bytes() != encoded:
                raise ValueError("semantic publication content-addressed destination mismatch")
            return SemanticDimensionPublication(
                published_uri=f"knowledge://spaces/{build_input.space_id}/semantic-dimensions/{build_input.dimension_id}/published",
                published_digest=digest,
            )
        temporary = target.with_name(f".{target.name}.tmp")
        if temporary.exists() or temporary.is_symlink():
            raise FileExistsError("semantic publication temporary output already exists")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                descriptor = -1
                stream.write(encoded)
                stream.flush()
                os.fsync(stream.fileno())
            os.link(temporary, target)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
            if temporary.exists() or temporary.is_symlink():
                temporary.unlink()
        uri = f"knowledge://spaces/{build_input.space_id}/semantic-dimensions/{build_input.dimension_id}/published"
        self._append_manifest(
            {
                "status": "published",
                "published_uri": uri,
                "published_digest": digest,
                "staging_digest": artifact.staging_digest,
                "source_count": len(build_input.source_snapshot),
                "updated_at": datetime.now(UTC).isoformat(),
            }
        )
        self.publish_count += 1
        return SemanticDimensionPublication(published_uri=uri, published_digest=digest)
