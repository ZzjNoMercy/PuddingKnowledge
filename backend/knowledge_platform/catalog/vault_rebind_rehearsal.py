"""Non-secret Vault rebind/rotation rehearsal for the Platform Catalog.

The operator owns secret bytes and performs server-side operations. This
module receives and emits references only, and requires a provider state
digest so compensation can be verified without reading a secret.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from .connector_rehearsal import _credential_reference
from .rehearsal import RehearsalReport, RehearsalVerificationError, build_table_snapshot

VAULT_FAILURE_CHECKPOINTS = frozenset({"after_rebind", "after_rotation", "before_finalize"})
DEFAULT_VAULT_FAILURE_PROBES = ("after_rebind", "after_rotation", "before_finalize")
_SAFE_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")


class VaultRebindVerificationError(RehearsalVerificationError):
    """Raised when a provider cannot prove a safe server-side rebind."""


class VaultRebindInjectedFailure(RuntimeError):
    """Controlled failure used to prove provider compensation."""


class VaultReferenceOperator(Protocol):
    """Secret-provider operations; implementations must never return secret bytes."""

    def state_proof(self) -> VaultProviderStateProof:
        """Return an authoritative, signed proof without secret material."""

    def is_readable(self, reference: str) -> bool:
        """Check reference readability without returning its value."""

    def rebind(self, source_reference: str, target_reference: str) -> None:
        """Create/attach the target reference by a provider-side operation."""

    def rotate(self, target_reference: str) -> str:
        """Rotate the target credential and return only its new reference."""

    def rollback(self, source_reference: str, target_reference: str, rotated_reference: str | None) -> None:
        """Compensate one attempted binding, even after partial provider failure."""


class LocalCredentialVaultOperator:
    """Use the existing local encrypted Credential Vault as a provider.

    This adapter is intentionally scoped to one ``LocalCredentialStore``
    namespace. It performs server-side-style operations inside the encrypted
    store and only exposes reference readability plus a signed logical-state
    proof. Secret bytes never leave this class. It is suitable for local
    development shadowing, not for claiming that an external production Vault
    has been re-bound.
    """

    def __init__(
        self,
        store: Any,
        *,
        proof_key: bytes,
        tracked_references: Sequence[str] = (),
    ) -> None:
        if not proof_key:
            raise ValueError("proof_key must not be empty")
        self.store = store
        self._proof_key = bytes(proof_key)
        self._tracked_references: set[str] = set()
        for reference in tracked_references:
            self._track(reference)

    @property
    def backend_name(self) -> str:
        return "LocalCredentialStore/CredentialVault"

    def _track(self, reference: str) -> str:
        normalized = _validate_reference(reference, label="local Vault reference")
        self._tracked_references.add(normalized)
        return normalized

    def _key_for(self, reference: str) -> str:
        normalized = self._track(reference)
        owner = str(getattr(self.store, "owner_user_id", "") or "")
        prefix = f"vault://users/{owner}/credentials/"
        if not owner or not normalized.startswith(prefix):
            raise VaultRebindVerificationError("local Vault reference is outside the store namespace")
        key = normalized.removeprefix(prefix)
        if not _SAFE_IDENTIFIER.fullmatch(key):
            raise VaultRebindVerificationError("local Vault reference key is unsafe")
        return key

    def _read(self, reference: str) -> str:
        self._key_for(reference)
        try:
            value = self.store.get(reference)
        except Exception as error:  # noqa: BLE001 - provider errors are redacted at this boundary
            raise VaultRebindVerificationError("local Credential Vault is unreadable") from error
        return str(value or "")

    def state_proof(self) -> VaultProviderStateProof:
        logical_state: list[tuple[str, str | None]] = []
        for reference in sorted(self._tracked_references):
            value = self._read(reference)
            value_digest = None if not value else "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()
            logical_state.append((reference, value_digest))
        payload = json.dumps(logical_state, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        signature = hmac.new(self._proof_key, digest.encode("ascii"), hashlib.sha256).hexdigest()
        return VaultProviderStateProof(digest=digest, signature=signature)

    def is_readable(self, reference: str) -> bool:
        return bool(self._read(reference))

    def rebind(self, source_reference: str, target_reference: str) -> None:
        value = self._read(source_reference)
        if not value:
            raise VaultRebindVerificationError("local source Vault reference is empty")
        target_key = self._key_for(target_reference)
        written_reference = self.store.put(target_key, value)
        if written_reference != target_reference:
            raise VaultRebindVerificationError("local Vault provider returned an unexpected target reference")
        self._track(target_reference)

    def rotate(self, target_reference: str) -> str:
        value = self._read(target_reference)
        if not value:
            raise VaultRebindVerificationError("local target Vault reference is empty")
        target_key = self._key_for(target_reference)
        rotated_key = f"{target_key}-rotated-v2"
        rotated_reference = self.store.put(rotated_key, value)
        self._track(rotated_reference)
        return rotated_reference

    def rollback(self, source_reference: str, target_reference: str, rotated_reference: str | None) -> None:
        self._key_for(source_reference)
        target_key = self._key_for(target_reference)
        if rotated_reference:
            self._key_for(rotated_reference)
            self.store.delete(rotated_reference)
        self.store.delete(target_key)


@dataclass(frozen=True, slots=True)
class VaultBinding:
    """One non-secret source-to-target binding in a migration manifest."""

    binding_id: str
    source_reference: str
    target_reference: str


@dataclass(frozen=True, slots=True)
class VaultProviderStateProof:
    """Provider snapshot digest plus an externally verifiable signature."""

    digest: str
    signature: str


@dataclass(frozen=True, slots=True)
class VaultBindingEvidence:
    binding_id: str
    source_reference_digest: str
    target_reference: str
    rotated_reference: str
    old_reference_retained: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "binding_id": self.binding_id,
            "source_reference_digest": self.source_reference_digest,
            "target_reference": self.target_reference,
            "rotated_reference": self.rotated_reference,
            "old_reference_retained": self.old_reference_retained,
        }


def _validate_identifier(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not _SAFE_IDENTIFIER.fullmatch(text):
        raise VaultRebindVerificationError(f"unsafe Vault binding {label}")
    return text


def _validate_reference(value: Any, *, label: str) -> str:
    text = str(value or "")
    if not text or _credential_reference(text) != text:
        raise VaultRebindVerificationError(f"invalid or unsafe Vault {label}")
    return text


def _reference_digest(value: str) -> str:
    from .rehearsal_runner import _source_ref_digest

    return _source_ref_digest(value)


def _validated_state_proof(
    operator: VaultReferenceOperator,
    verifier: Callable[[VaultProviderStateProof], bool],
) -> str:
    proof = operator.state_proof()
    if not isinstance(proof, VaultProviderStateProof):
        raise VaultRebindVerificationError("Vault provider returned an invalid state proof")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", proof.digest):
        raise VaultRebindVerificationError("Vault provider must return a non-secret sha256 state digest")
    if not proof.signature or len(proof.signature) > 512 or not verifier(proof):
        raise VaultRebindVerificationError("Vault provider state proof is not independently verified")
    return proof.digest


def _manifest_rows(evidence: Sequence[VaultBindingEvidence]) -> list[dict[str, Any]]:
    return [item.to_dict() for item in evidence]


def _execute_rebind(
    operator: VaultReferenceOperator,
    bindings: Sequence[VaultBinding],
    *,
    failure_checkpoint: str | None = None,
) -> tuple[VaultBindingEvidence, ...]:
    prepared: list[tuple[VaultBinding, str | None]] = []
    try:
        for binding in bindings:
            if not operator.is_readable(binding.source_reference):
                raise VaultRebindVerificationError(f"source Vault reference is not readable: {binding.binding_id}")
            # Register compensation before calling an external provider. A
            # provider may create the target and then report an exception.
            prepared.append((binding, None))
            operator.rebind(binding.source_reference, binding.target_reference)
            if not operator.is_readable(binding.target_reference):
                raise VaultRebindVerificationError(f"target Vault reference is not readable: {binding.binding_id}")
        if failure_checkpoint == "after_rebind":
            raise VaultRebindInjectedFailure("injected Vault rehearsal failure at after_rebind")
        evidence: list[VaultBindingEvidence] = []
        for index, (binding, _) in enumerate(prepared):
            rotated_reference = _validate_reference(
                operator.rotate(binding.target_reference), label=f"rotated reference {binding.binding_id}"
            )
            if rotated_reference in {binding.source_reference, binding.target_reference}:
                raise VaultRebindVerificationError(f"rotation did not produce a new reference: {binding.binding_id}")
            prepared[index] = (binding, rotated_reference)
            if not operator.is_readable(rotated_reference):
                raise VaultRebindVerificationError(f"rotated Vault reference is not readable: {binding.binding_id}")
            old_retained = operator.is_readable(binding.source_reference)
            if not old_retained:
                raise VaultRebindVerificationError(f"old Vault reference was revoked early: {binding.binding_id}")
            evidence.append(
                VaultBindingEvidence(
                    binding_id=binding.binding_id,
                    source_reference_digest=_reference_digest(binding.source_reference),
                    target_reference=binding.target_reference,
                    rotated_reference=rotated_reference,
                    old_reference_retained=old_retained,
                )
            )
            if failure_checkpoint == "after_rotation" and index == len(prepared) - 1:
                raise VaultRebindInjectedFailure("injected Vault rehearsal failure at after_rotation")
        if failure_checkpoint == "before_finalize":
            raise VaultRebindInjectedFailure("injected Vault rehearsal failure at before_finalize")
        return tuple(evidence)
    except Exception:
        rollback_errors: list[str] = []
        for binding, rotated_reference in reversed(prepared):
            try:
                operator.rollback(binding.source_reference, binding.target_reference, rotated_reference)
            except Exception as error:  # pragma: no cover - exercised by provider integration, not fake
                rollback_errors.append(f"{binding.binding_id}: {error}")
        if rollback_errors:
            raise VaultRebindVerificationError(
                "Vault compensation failed; manual provider recovery required: " + "; ".join(rollback_errors)
            ) from None
        raise


@dataclass(frozen=True, slots=True)
class VaultRebindRehearsalResult:
    source_revision: str
    target_revision: str
    active_revision: str
    injected_failure_checkpoints: tuple[str, ...]
    bindings: tuple[VaultBindingEvidence, ...]
    report: RehearsalReport

    def to_dict(self) -> dict[str, Any]:
        return {
            "format": "agent-knowledge-platform-vault-rebind-rehearsal/v1",
            "source_revision": self.source_revision,
            "target_revision": self.target_revision,
            "active_revision": self.active_revision,
            "injected_failure_checkpoints": list(self.injected_failure_checkpoints),
            "bindings": [binding.to_dict() for binding in self.bindings],
            "report": self.report.to_dict(),
        }

    def write_json(self, path: str | Path) -> None:
        Path(path).write_text(
            json.dumps(self.to_dict(), ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )


def run_vault_rebind_rehearsal_with_rollback_probes(
    operator: VaultReferenceOperator,
    bindings: Sequence[VaultBinding],
    *,
    source_revision: str,
    target_revision: str,
    active_revision: str,
    failure_checkpoints: Sequence[str] = DEFAULT_VAULT_FAILURE_PROBES,
    state_proof_verifier: Callable[[VaultProviderStateProof], bool] | None = None,
) -> VaultRebindRehearsalResult:
    """Run server-side rebind + rotation and prove compensating rollback.

    ``operator.state_digest`` is the only provider state observation used for
    rollback proof. The returned manifest contains no source reference value.
    """

    for value, label in (
        (source_revision, "source revision"),
        (target_revision, "target revision"),
        (active_revision, "active revision"),
    ):
        _validate_identifier(value, label=label)
    if source_revision == target_revision:
        raise VaultRebindVerificationError("source and target revisions must differ")
    if state_proof_verifier is None:
        raise VaultRebindVerificationError("state_proof_verifier is required for Vault rollback evidence")
    normalized_bindings = tuple(bindings)
    if not normalized_bindings:
        raise VaultRebindVerificationError("at least one Vault binding is required")
    binding_ids: set[str] = set()
    for binding in normalized_bindings:
        binding_id = _validate_identifier(binding.binding_id, label="binding id")
        if binding_id in binding_ids:
            raise VaultRebindVerificationError(f"duplicate Vault binding id: {binding_id}")
        binding_ids.add(binding_id)
        _validate_reference(binding.source_reference, label=f"source reference {binding_id}")
        target_reference = _validate_reference(binding.target_reference, label=f"target reference {binding_id}")
        if target_reference == binding.source_reference:
            raise VaultRebindVerificationError(f"target reference must be distinct: {binding_id}")
    checkpoints = tuple(failure_checkpoints)
    unknown = sorted(set(checkpoints) - VAULT_FAILURE_CHECKPOINTS)
    if not checkpoints:
        raise VaultRebindVerificationError("failure_checkpoints must not be empty")
    if unknown:
        raise VaultRebindVerificationError(f"unsupported Vault failure checkpoints: {unknown}")

    baseline = _validated_state_proof(operator, state_proof_verifier)
    for checkpoint in checkpoints:
        try:
            _execute_rebind(operator, normalized_bindings, failure_checkpoint=checkpoint)
        except VaultRebindInjectedFailure:
            pass
        if _validated_state_proof(operator, state_proof_verifier) != baseline:
            raise VaultRebindVerificationError(f"provider state changed after rollback probe {checkpoint}")

    evidence = _execute_rebind(operator, normalized_bindings)
    retry_evidence = _execute_rebind(operator, normalized_bindings)
    _validated_state_proof(operator, state_proof_verifier)
    retry_idempotent = evidence == retry_evidence
    rows = _manifest_rows(evidence)
    serialized_rows = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    snapshot = build_table_snapshot("vault_bindings", rows, primary_key_fields=("binding_id",))
    report = RehearsalReport(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision_before=active_revision,
        active_revision_after=active_revision,
        source_tables=(snapshot,),
        target_tables=(build_table_snapshot("vault_bindings", rows, primary_key_fields=("binding_id",)),),
        injected_failure_checkpoints=checkpoints,
        retry_idempotent=retry_idempotent,
        checks={
            "secret_redaction": all(binding.source_reference not in serialized_rows for binding in normalized_bindings),
            "file_reachability": True,
            "lease_state": True,
            "foreign_keys": True,
            "vault_rebind": all(binding.target_reference for binding in evidence),
            "vault_rotation": all(binding.rotated_reference for binding in evidence),
            "old_reference_retained": all(binding.old_reference_retained for binding in evidence),
            "rollback_compensation": True,
        },
        check_scopes={
            "secret_redaction": "only source-reference digests and provider-generated references enter the manifest",
            "file_reachability": "not applicable to Vault references",
            "lease_state": "not applicable to Vault reference operations",
            "foreign_keys": "not applicable to Vault reference operations",
            "vault_rebind": "provider-side rebind verified by target readability",
            "vault_rotation": "provider-side rotation verified by rotated-reference readability",
            "old_reference_retained": "old reference remains readable during the rollback window",
            "rollback_compensation": "provider state digest returns to its pre-probe value",
        },
    )
    report.verify_safe()
    return VaultRebindRehearsalResult(
        source_revision=source_revision,
        target_revision=target_revision,
        active_revision=active_revision,
        injected_failure_checkpoints=checkpoints,
        bindings=evidence,
        report=report,
    )
