"""Durable Platform worker boundary for logical dataset Processing."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from knowledge_contracts import Correlation, Principal, is_valid_knowledge_uri

from .processing import LogicalDatasetProcessingRequest, LogicalDatasetProcessingService

_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


@dataclass(frozen=True, slots=True)
class LogicalDatasetProcessingJobRequest:
    dataset_id: str
    space_id: str
    source_paths: Mapping[str, Path]
    idempotency_key: str
    principal: Principal
    correlation: Correlation

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.dataset_id) or not _ID_RE.fullmatch(self.space_id) or not self.idempotency_key.strip():
            raise ValueError("logical dataset Processing job identity is invalid")
        if not isinstance(self.source_paths, Mapping) or not self.source_paths:
            raise ValueError("logical dataset Processing source paths are required")
        if any(not _ID_RE.fullmatch(str(item)) for item in self.source_paths):
            raise ValueError("logical dataset Processing source ID is invalid")

    def binding_digest(self) -> str:
        material = json.dumps(
            {
                "dataset_id": self.dataset_id,
                "space_id": self.space_id,
                "source_asset_ids": sorted(str(item) for item in self.source_paths),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return "sha256:" + hashlib.sha256(material).hexdigest()


@dataclass(frozen=True, slots=True)
class LogicalDatasetProcessingJobResult:
    job_id: str
    dataset_id: str
    space_id: str
    resource_uri: str
    content_digest: str
    row_count: int

    def __post_init__(self) -> None:
        if not _ID_RE.fullmatch(self.job_id) or not _ID_RE.fullmatch(self.dataset_id) or not _ID_RE.fullmatch(self.space_id):
            raise ValueError("logical dataset Processing result identity is invalid")
        if not is_valid_knowledge_uri(self.resource_uri) or not _DIGEST_RE.fullmatch(self.content_digest) or self.row_count < 0:
            raise ValueError("logical dataset Processing result is invalid")


class LogicalDatasetProcessingJobStore(Protocol):
    def claim(
        self, *, dataset_id: str, space_id: str, idempotency_key: str, binding_digest: str
    ) -> tuple[bool, str, str, LogicalDatasetProcessingJobResult | None]: ...

    def complete(
        self,
        *,
        job_id: str,
        owner: str,
        dataset_id: str,
        space_id: str,
        binding_digest: str,
        resource_uri: str,
        content_digest: str,
        row_count: int,
    ) -> LogicalDatasetProcessingJobResult: ...

    def release(self, *, job_id: str, owner: str) -> None: ...


class LogicalDatasetProcessingError(RuntimeError):
    """Processing stopped before the durable worker job became terminal."""


class LogicalDatasetProcessingWorker:
    """Run the existing CAS Processing service behind a durable fenced claim."""

    def __init__(self, *, service: LogicalDatasetProcessingService, jobs: LogicalDatasetProcessingJobStore) -> None:
        self._service = service
        self._jobs = jobs

    async def process(self, request: LogicalDatasetProcessingJobRequest) -> LogicalDatasetProcessingJobResult:
        binding_digest = request.binding_digest()
        acquired, job_id, owner, existing = self._jobs.claim(
            dataset_id=request.dataset_id,
            space_id=request.space_id,
            idempotency_key=request.idempotency_key,
            binding_digest=binding_digest,
        )
        if not acquired:
            if existing is None:
                raise LogicalDatasetProcessingError("logical dataset Processing job is already running")
            return existing
        try:
            result = await self._service.process(
                principal=request.principal,
                correlation=request.correlation,
                request=LogicalDatasetProcessingRequest(
                    dataset_id=request.dataset_id,
                    space_id=request.space_id,
                    source_paths=request.source_paths,
                ),
            )
            if result.status != "ok" or not isinstance(result.data, Mapping):
                raise LogicalDatasetProcessingError("logical dataset Processing service did not publish")
            dataset = result.data.get("dataset")
            if not isinstance(dataset, Mapping):
                raise LogicalDatasetProcessingError("logical dataset Processing result is invalid")
            resource_uri = str(dataset.get("source_uri") or "")
            content_digest = str(dataset.get("content_digest") or "")
            row_count = dataset.get("row_count")
            if not resource_uri.startswith("knowledge://") or not _DIGEST_RE.fullmatch(content_digest) or type(row_count) is not int:
                raise LogicalDatasetProcessingError("logical dataset Processing publication is invalid")
            return self._jobs.complete(
                job_id=job_id,
                owner=owner,
                dataset_id=request.dataset_id,
                space_id=request.space_id,
                binding_digest=binding_digest,
                resource_uri=resource_uri,
                content_digest=content_digest,
                row_count=row_count,
            )
        except Exception:
            self._jobs.release(job_id=job_id, owner=owner)
            raise
