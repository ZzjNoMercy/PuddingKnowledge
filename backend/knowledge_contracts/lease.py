"""Framework-neutral queue lease contracts.

The contract carries only stable job identity and lease state.  Database
syntax, worker framework, and host clock selection belong to an adapter.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from uuid import uuid4


class LeaseStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class LeaseError(RuntimeError):
    """Base error for lease state violations."""


class LeaseLostError(LeaseError):
    """The caller no longer owns a live lease."""


@dataclass(frozen=True, slots=True)
class LeaseRecord:
    job_id: str
    status: LeaseStatus
    owner: str | None
    expires_at: str | None
    attempt: int
    fencing_token: str | None = None

    def __post_init__(self) -> None:
        if not self.job_id.strip():
            raise ValueError("LeaseRecord.job_id must not be empty")
        if self.attempt < 0:
            raise ValueError("LeaseRecord.attempt must not be negative")
        if self.status is LeaseStatus.RUNNING and (not self.owner or not self.expires_at or not self.fencing_token):
            raise ValueError("running LeaseRecord requires owner, expires_at, and fencing_token")


@dataclass(frozen=True, slots=True)
class LeaseCompletion:
    job_id: str
    owner: str
    status: LeaseStatus
    fencing_token: str | None = None

    def __post_init__(self) -> None:
        if self.status not in {LeaseStatus.SUCCEEDED, LeaseStatus.FAILED, LeaseStatus.CANCELLED}:
            raise ValueError("LeaseCompletion status must be terminal")
        if not self.job_id.strip() or not self.owner.strip() or not self.fencing_token:
            raise ValueError("LeaseCompletion identity and fencing_token must not be empty")


def new_fencing_token() -> str:
    """Return an unguessable token that is never reused after reclaim."""

    return uuid4().hex
