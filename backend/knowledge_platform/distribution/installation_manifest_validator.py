"""Structural validator for the installation migration manifest contract.

The canonical schema is
``docs/knowledge-platform/installation-migration-manifest.schema.json``
(format ``agent-knowledge-platform-installation-migration/v1``), owned by this
repository.  This validator is deliberately structural only: it accepts every
state of the specification 11.20 machine and never advances, reopens or
downgrades a manifest.  It stays behaviorally identical to the committed
Harness-side consumer so a manifest accepted here is accepted there.  Unlike
``installation_migration`` (an in-memory shadow contract), this module performs
no state-machine invariant checks; producers and the writer authority assign
command bind the states they require.
"""

from __future__ import annotations

import re
from typing import Any

MANIFEST_FORMAT = "agent-knowledge-platform-installation-migration/v1"
_DOMAINS = ("session_harness", "knowledge_catalog", "connector_jobs")
_WRITERS = ("puddingclaw", "puddingharness", "puddingknowledge")
_STATES = ("DISCOVERED", "PREPARED", "CUTOVER", "ROLLED_BACK", "FINALIZED")
_STRATEGIES = ("reverse_delta", "snapshot_restore", "no_write_until_finalized")
_REQUIRED = {
    "format", "source", "targets", "object_summaries", "id_resource_mappings",
    "credential_rebinds", "active_writers", "checkpoint", "rollback_strategy",
    "state", "rollback_window_open",
}
_DIGEST_FIELDS = {
    "snapshot_digest", "rollback_evidence_digest", "recovery_evidence_digest",
    "post_cutover_delta_digest", "rollback_reconciliation_digest",
}
_TEXT_FIELDS = {"staging_namespace", "active_installation_revision", "completed_at"}
_OPTIONAL = _DIGEST_FIELDS | _TEXT_FIELDS | {
    "post_cutover_delta_count", "rollback_delta_reconciled", "failure_checkpoint",
    "recovery_count", "started_at",
}
_HEX = "0123456789abcdef"


def _sha(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 71
        and value.startswith("sha256:")
        and all(character in _HEX for character in value[7:])
    )


def _text(value: Any, maximum: int = 160) -> bool:
    return isinstance(value, str) and 1 <= len(value) <= maximum


def _resource_uri(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"(knowledge|harness)://[^\s]+", value) is not None


def _credential_uri(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"credential://[^\s]+", value) is not None


def _rebind_shape(item: Any) -> bool:
    return (
        isinstance(item, dict)
        and set(item) == {"slot", "source_ref_digest", "target_ref", "status"}
        and _text(item["slot"])
        and _sha(item["source_ref_digest"])
        and _credential_uri(item["target_ref"])
        and item["status"] in ("pending", "rebound", "failed")
    )


def validate_manifest(value: Any) -> None:
    """Schema-faithful structural validation of the versioned manifest contract."""
    if not isinstance(value, dict) or not _REQUIRED <= set(value) or not set(value) <= _REQUIRED | _OPTIONAL:
        raise ValueError("Installation manifest keys are invalid")
    if value["format"] != MANIFEST_FORMAT:
        raise ValueError("Installation manifest format is unsupported")
    source = value["source"]
    if (
        not isinstance(source, dict)
        or set(source) != {"installation_id", "schema_revision", "catalog_revision"}
        or not all(_text(source[key]) for key in source)
    ):
        raise ValueError("Installation manifest source is invalid")
    targets = value["targets"]
    if not isinstance(targets, dict) or not targets or not all(_text(item) for item in targets.values()):
        raise ValueError("Installation manifest targets are invalid")
    summaries = value["object_summaries"]
    if not isinstance(summaries, list):
        raise ValueError("Installation manifest object summaries are invalid")
    domains = []
    for item in summaries:
        if (
            not isinstance(item, dict)
            or set(item) != {"domain", "object_count", "source_digest"}
            or item["domain"] not in _DOMAINS
            or type(item["object_count"]) is not int
            or item["object_count"] < 0
            or not _sha(item["source_digest"])
        ):
            raise ValueError("Installation manifest object summary is invalid")
        domains.append(item["domain"])
    if len(set(domains)) != len(domains):
        raise ValueError("Installation manifest repeats an object summary domain")
    mappings = value["id_resource_mappings"]
    if not isinstance(mappings, list) or any(
        not isinstance(item, dict)
        or set(item) != {"source_id", "resource_uri"}
        or not _text(item["source_id"])
        or not _resource_uri(item["resource_uri"])
        for item in mappings
    ):
        raise ValueError("Installation manifest resource mapping is invalid")
    rebinds = value["credential_rebinds"]
    if not isinstance(rebinds, list) or any(not _rebind_shape(item) for item in rebinds):
        raise ValueError("Installation manifest credential rebind is invalid")
    slots = [item["slot"] for item in rebinds]
    if len(set(slots)) != len(slots):
        raise ValueError("Installation manifest repeats a credential slot")
    writers = value["active_writers"]
    if (
        not isinstance(writers, dict)
        or set(writers) != set(_DOMAINS)
        or any(writers[domain] not in _WRITERS for domain in _DOMAINS)
    ):
        raise ValueError("Installation manifest active writers are invalid")
    checkpoint = value["checkpoint"]
    if not isinstance(checkpoint, dict) or not all(_text(item) for item in checkpoint.values()):
        raise ValueError("Installation manifest checkpoint is invalid")
    if value["rollback_strategy"] not in _STRATEGIES or value["state"] not in _STATES:
        raise ValueError("Installation manifest rollback strategy or state is invalid")
    if type(value["rollback_window_open"]) is not bool:
        raise ValueError("Installation manifest rollback window flag is invalid")
    for key in _DIGEST_FIELDS & set(value):
        if value[key] is not None and not _sha(value[key]):
            raise ValueError("Installation manifest digest field is invalid")
    for key in _TEXT_FIELDS & set(value):
        if value[key] is not None and not _text(value[key]):
            raise ValueError("Installation manifest text field is invalid")
    if "started_at" in value and not _text(value["started_at"]):
        raise ValueError("Installation manifest start time is invalid")
    if "failure_checkpoint" in value and value["failure_checkpoint"] is not None and not _text(value["failure_checkpoint"], 64):
        raise ValueError("Installation manifest failure checkpoint is invalid")
    for key in {"post_cutover_delta_count", "recovery_count"} & set(value):
        if type(value[key]) is not int or value[key] < 0:
            raise ValueError("Installation manifest counter is invalid")
    if "rollback_delta_reconciled" in value and type(value["rollback_delta_reconciled"]) is not bool:
        raise ValueError("Installation manifest rollback reconciliation flag is invalid")


__all__ = ["MANIFEST_FORMAT", "validate_manifest"]
