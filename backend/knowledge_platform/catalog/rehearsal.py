"""Pure verification primitives for the Phase 0B Catalog rehearsal.

The functions here do not copy rows, open a database, or switch a revision.
They turn a sanitized source/target snapshot into evidence that a later
migration runner can consume. Keeping verification pure makes failure
injection and idempotency checks possible before production wiring exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any


class RehearsalVerificationError(ValueError):
    """Raised when a target snapshot is unsafe to activate."""


REQUIRED_CHECKS = frozenset({"secret_redaction", "file_reachability", "lease_state", "foreign_keys"})


def _digest(value: Any) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class TableSnapshot:
    table: str
    row_count: int
    primary_key_digest: str
    normalized_content_digest: str
    file_reference_digest: str
    lease_state_digest: str
    redacted_secret_fields: tuple[str, ...] = ()


def build_table_snapshot(
    table: str,
    rows: Iterable[Mapping[str, Any]],
    *,
    primary_key_fields: Sequence[str],
    secret_fields: Sequence[str] = (),
    file_reference_fields: Sequence[str] = (),
    lease_fields: Sequence[str] = (),
) -> TableSnapshot:
    """Build a deterministic, secret-free snapshot from sanitized row data."""

    if not table.strip():
        raise ValueError("table must not be empty")
    if not primary_key_fields:
        raise ValueError("primary_key_fields must not be empty")
    source_rows = [dict(row) for row in rows]
    missing_keys = [
        field_name for field_name in primary_key_fields if any(field_name not in row for row in source_rows)
    ]
    if missing_keys:
        raise RehearsalVerificationError(f"{table}: primary-key fields missing: {missing_keys}")

    redacted = set(secret_fields)
    normalized_rows = []
    for row in source_rows:
        normalized = {key: "<redacted>" if key in redacted else value for key, value in row.items()}
        normalized_rows.append(normalized)
    normalized_rows.sort(key=lambda row: tuple(str(row.get(key, "")) for key in primary_key_fields))
    primary_keys = [tuple(row.get(key) for key in primary_key_fields) for row in normalized_rows]
    file_refs = sorted(
        str(row[field_name])
        for row in normalized_rows
        for field_name in file_reference_fields
        if row.get(field_name) not in (None, "")
    )
    lease_state = [{field_name: row.get(field_name) for field_name in lease_fields} for row in normalized_rows]
    return TableSnapshot(
        table=table,
        row_count=len(normalized_rows),
        primary_key_digest=_digest(primary_keys),
        normalized_content_digest=_digest(normalized_rows),
        file_reference_digest=_digest(file_refs),
        lease_state_digest=_digest(lease_state),
        redacted_secret_fields=tuple(sorted(redacted)),
    )


@dataclass(frozen=True, slots=True)
class RehearsalReport:
    source_revision: str
    target_revision: str
    active_revision_before: str
    active_revision_after: str
    source_tables: tuple[TableSnapshot, ...]
    target_tables: tuple[TableSnapshot, ...]
    target_out_of_scope_tables: tuple[TableSnapshot, ...] = ()
    injected_failure_checkpoints: tuple[str, ...] = ()
    retry_idempotent: bool = False
    checks: Mapping[str, bool] = field(default_factory=dict)
    check_scopes: Mapping[str, str] = field(default_factory=dict)

    @property
    def active_revision_changed(self) -> bool:
        return self.active_revision_before != self.active_revision_after

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-safe machine-readable report."""

        return {
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "active_revision_before": self.active_revision_before,
            "active_revision_after": self.active_revision_after,
            "active_revision_changed": self.active_revision_changed,
            "source_tables": [asdict(snapshot) for snapshot in self.source_tables],
            "target_tables": [asdict(snapshot) for snapshot in self.target_tables],
            "target_out_of_scope_tables": [asdict(snapshot) for snapshot in self.target_out_of_scope_tables],
            "injected_failure_checkpoints": list(self.injected_failure_checkpoints),
            "retry_idempotent": self.retry_idempotent,
            "checks": dict(self.checks),
            "check_scopes": dict(self.check_scopes),
        }

    def verify_safe(self) -> None:
        """Fail closed unless source/target evidence is complete and equal."""

        if self.active_revision_changed:
            raise RehearsalVerificationError("Phase 0 rehearsal must not change active_revision")
        if self.source_revision.strip() == self.target_revision.strip():
            raise RehearsalVerificationError("target revision must be distinct from source revision")
        if not self.retry_idempotent:
            raise RehearsalVerificationError("retry idempotency is not proven")
        missing_checks = sorted(REQUIRED_CHECKS - set(self.checks))
        if missing_checks:
            raise RehearsalVerificationError(f"required rehearsal checks are missing: {missing_checks}")
        if len(self.source_tables) != len(self.target_tables):
            raise RehearsalVerificationError("source and target table counts differ")
        source_by_name = {snapshot.table: snapshot for snapshot in self.source_tables}
        target_by_name = {snapshot.table: snapshot for snapshot in self.target_tables}
        if len(source_by_name) != len(self.source_tables) or len(target_by_name) != len(self.target_tables):
            raise RehearsalVerificationError("duplicate table names are not allowed")
        if set(source_by_name) != set(target_by_name):
            raise RehearsalVerificationError("source and target table names differ")
        out_of_scope_names = [snapshot.table for snapshot in self.target_out_of_scope_tables]
        if len(set(out_of_scope_names)) != len(out_of_scope_names):
            raise RehearsalVerificationError("duplicate out-of-scope table names are not allowed")
        for table, source in source_by_name.items():
            target = target_by_name[table]
            for field_name in (
                "row_count",
                "primary_key_digest",
                "normalized_content_digest",
                "file_reference_digest",
                "lease_state_digest",
                "redacted_secret_fields",
            ):
                if getattr(source, field_name) != getattr(target, field_name):
                    raise RehearsalVerificationError(f"{table}: {field_name} differs")
        if not all(self.checks.values()):
            raise RehearsalVerificationError("one or more explicit rehearsal checks failed")
