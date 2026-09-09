"""Fail-closed Phase 10 release-candidate validation matrix.

The current repository can prove only local shadows.  This module makes that
distinction explicit and prevents a same-checkout observation from becoming a
PuddingKnowledge release or PuddingHarness RC claim.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from typing import Any

_FORMAT = "agent-knowledge-platform-phase10-rc-validation/v1"
_STATUS = "PHASE10_RC_PREFLIGHT_NOT_RELEASEABLE"
_CHECK_NAMES = (
    "platform_independent_build",
    "platform_independent_test",
    "platform_sbom",
    "harness_independent_build",
    "harness_independent_test",
    "harness_sbom",
    "harness_no_knowledge_dependency",
    "external_mcp_e2e",
    "installation_upgrade",
    "failure_recovery",
    "stateful_rollback",
)
_CHECK_STATUSES = {"shadow_verified", "blocked", "not_run"}
_SHA256 = re.compile(r"^sha256:[0-9a-f]{64}$")
_SAFE_REVISION = re.compile(r"^[A-Za-z0-9._:/+@-]{1,256}$")


class RcValidationError(ValueError):
    """Raised when the RC validation matrix is incomplete or overclaims."""


def _safe_revision(value: Any, *, label: str) -> str:
    text = str(value or "").strip()
    if (
        not _SAFE_REVISION.fullmatch(text)
        or ".." in text
        or "\\" in text
        or any(ord(character) < 32 or ord(character) == 127 for character in text)
        or any(marker.lower() in text.lower() for marker in ("/Users/", "/private/", "file://", "password=", "secret="))
    ):
        raise RcValidationError(f"RC {label} is unsafe")
    return text


@dataclass(frozen=True, slots=True)
class RcCheck:
    name: str
    status: str
    evidence: str

    def __post_init__(self) -> None:
        if self.name not in _CHECK_NAMES:
            raise RcValidationError("unknown RC validation check")
        if self.status not in _CHECK_STATUSES:
            raise RcValidationError("unsupported RC validation status")
        if not isinstance(self.evidence, str) or not self.evidence.strip() or any(
            marker in self.evidence for marker in ("/Users/", "/private/", "file://", "password=", "secret=")
        ):
            raise RcValidationError("RC evidence must be a portable explanation")

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "status": self.status, "evidence": self.evidence}


@dataclass(frozen=True, slots=True)
class RcValidationManifest:
    source_revision: str
    source_tag: str | None
    source_tag_signed: bool
    source_worktree_clean: bool
    extraction_replay_consistent: bool
    extraction_manifest_digest: str
    installation_shadow_digest: str
    checks: tuple[RcCheck, ...]
    release_rollback_window_open: bool = True
    status: str = _STATUS

    def __post_init__(self) -> None:
        if self.status != _STATUS:
            raise RcValidationError("RC matrix must remain non-releaseable")
        if not isinstance(self.source_revision, str):
            raise RcValidationError("RC source revision is required")
        _safe_revision(self.source_revision, label="source revision")
        if self.extraction_replay_consistent is not True:
            raise RcValidationError("RC matrix requires a replay-consistent extraction manifest")
        if self.source_tag is not None and not isinstance(self.source_tag, str):
            raise RcValidationError("RC source tag cannot be empty")
        if self.source_tag is not None:
            _safe_revision(self.source_tag, label="source tag")
        for label, digest in (
            ("extraction manifest", self.extraction_manifest_digest),
            ("installation shadow", self.installation_shadow_digest),
        ):
            if not isinstance(digest, str) or not _SHA256.fullmatch(digest):
                raise RcValidationError(f"{label} digest is invalid")
        if tuple(check.name for check in self.checks) != _CHECK_NAMES:
            raise RcValidationError("RC checks must cover the complete ordered matrix")
        if any(check.status != "blocked" for check in self.checks[:8]):
            raise RcValidationError("independent RC checks must remain blocked in preflight")
        if not self.release_rollback_window_open:
            raise RcValidationError("release rollback window cannot close in Phase 10 preflight")

    @property
    def releaseable(self) -> bool:
        return False

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": _FORMAT,
            "status": self.status,
            "releaseable": self.releaseable,
            "source": {
                "revision": self.source_revision,
                "tag": self.source_tag,
                "tag_signed": self.source_tag_signed,
                "worktree_clean": self.source_worktree_clean,
            },
            "evidence": {
                "extraction_replay_consistent": self.extraction_replay_consistent,
                "extraction_manifest_digest": self.extraction_manifest_digest,
                "installation_shadow_digest": self.installation_shadow_digest,
            },
            "checks": [check.to_dict() for check in self.checks],
            "release_rollback_window_open": self.release_rollback_window_open,
        }


def stable_digest(value: Any) -> str:
    """Return a digest for a path-free JSON-compatible evidence projection."""

    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def build_rc_validation_manifest(
    *,
    extraction_manifest: dict[str, Any],
    installation_shadow: dict[str, Any],
    dependency_scan: dict[str, Any] | None = None,
) -> RcValidationManifest:
    """Build the honest RC matrix from the two local Phase 10 shadows."""

    if extraction_manifest.get("status") != "PHASE10_EXTRACTION_PREFLIGHT_NOT_EXECUTABLE":
        raise RcValidationError("unexpected extraction preflight status")
    if extraction_manifest.get("replay_consistent") is not True:
        raise RcValidationError("extraction preflight replay is not proven")
    dependency_evidence = "mixed-file cleanup and dependency scan remain unexecuted"
    if dependency_scan is not None:
        if dependency_scan.get("status") != "PHASE10_HARNESS_DEPENDENCY_SCAN_BLOCKED":
            raise RcValidationError("unexpected Harness dependency scan status")
        finding_count = dependency_scan.get("finding_count")
        if not isinstance(finding_count, int) or finding_count < 0:
            raise RcValidationError("Harness dependency scan finding count is invalid")
        dependency_evidence = f"same-checkout scan found {finding_count} Knowledge-coupling findings; mixed cleanup remains pending"
    if installation_shadow.get("status") != "PHASE10_INSTALLATION_MIGRATION_SHADOW_PASS_NOT_ACTIVATABLE":
        raise RcValidationError("unexpected installation shadow status")
    rollback_verified = (
        installation_shadow.get("replay_idempotent") is True
        and installation_shadow.get("post_cutover_delta_verified") is True
        and installation_shadow.get("rollback_reconciliation_verified") is True
        and installation_shadow.get("stateful_rollback_replay_verified") is True
        and installation_shadow.get("credential_rebind_recovery_verified") is True
        and installation_shadow.get("activation_allowed") is False
        and installation_shadow.get("execution_allowed") is False
        and installation_shadow.get("physical_copy_performed") is False
        and installation_shadow.get("secret_bytes_read") is False
        and installation_shadow.get("source_home_changed") is False
        and installation_shadow.get("canonical_catalog_changed") is False
        and installation_shadow.get("final_manifest", {}).get("state") == "ROLLED_BACK"
    )
    upgrade_verified = (
        installation_shadow.get("state_sequence") == ["DISCOVERED", "PREPARED", "CUTOVER", "ROLLED_BACK"]
        and installation_shadow.get("post_cutover_delta_verified") is True
        and installation_shadow.get("rollback_reconciliation_verified") is True
        and installation_shadow.get("credential_rebind_recovery_verified") is True
        and installation_shadow.get("activation_allowed") is False
        and installation_shadow.get("execution_allowed") is False
        and installation_shadow.get("physical_copy_performed") is False
        and installation_shadow.get("secret_bytes_read") is False
    )
    failure_recovery_verified = (
        installation_shadow.get("failure_recovery_verified") is True
        and installation_shadow.get("partial_target_import_replay_verified") is True
    )
    checks = (
        RcCheck("platform_independent_build", "blocked", "no extracted Platform repository exists in this checkout"),
        RcCheck("platform_independent_test", "blocked", "no extracted Platform repository exists in this checkout"),
        RcCheck("platform_sbom", "blocked", "no release artifact or SBOM was generated"),
        RcCheck("harness_independent_build", "blocked", "no extracted Harness repository exists in this checkout"),
        RcCheck("harness_independent_test", "blocked", "no independent Harness test root exists in this checkout"),
        RcCheck("harness_sbom", "blocked", "no pure Harness release artifact or SBOM was generated"),
        RcCheck("harness_no_knowledge_dependency", "blocked", dependency_evidence),
        RcCheck("external_mcp_e2e", "blocked", "no extracted RC endpoint is available for external MCP E2E"),
        RcCheck(
            "installation_upgrade",
            "shadow_verified" if upgrade_verified else "blocked",
            "local state-machine shadow reached CUTOVER, recorded a non-zero post-cutover delta, and reconciled it"
            if upgrade_verified
            else "installation shadow did not complete its state sequence",
        ),
        RcCheck(
            "failure_recovery",
            "shadow_verified" if failure_recovery_verified else "blocked",
            "a registered PREPARED checkpoint failed, resumed, and was verified before CUTOVER"
            if failure_recovery_verified
            else "injected installer failure and resumable recovery are not yet implemented",
        ),
        RcCheck(
            "stateful_rollback",
            "shadow_verified" if rollback_verified else "blocked",
            "post-cutover delta was reconciled, object-set reverse replay was lossless, source writers were restored, target revision cleared, and replay was idempotent"
            if rollback_verified
            else "rollback invariants were not proven by the installation shadow",
        ),
    )
    return RcValidationManifest(
        source_revision=str(extraction_manifest.get("source_revision") or "unresolved"),
        source_tag=extraction_manifest.get("source_tag"),
        source_tag_signed=extraction_manifest.get("source_tag_signed") is True,
        source_worktree_clean=extraction_manifest.get("source_worktree_clean") is True,
        extraction_replay_consistent=True,
        extraction_manifest_digest=stable_digest(extraction_manifest),
        installation_shadow_digest=stable_digest(installation_shadow),
        checks=checks,
    )


__all__ = [
    "RcCheck",
    "RcValidationError",
    "RcValidationManifest",
    "build_rc_validation_manifest",
    "stable_digest",
]
