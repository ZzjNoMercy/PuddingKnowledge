from __future__ import annotations

import hashlib
import json

import pytest


class _OpaqueVault:
    def __init__(self, references: dict[str, str]) -> None:
        self.references = dict(references)
        self.fail_rotation = False
        self.fail_rebind_after_write = False

    def state_proof(self):
        from knowledge_platform.catalog import VaultProviderStateProof

        payload = json.dumps(sorted(self.references), separators=(",", ":")).encode()
        digest = "sha256:" + hashlib.sha256(payload).hexdigest()
        return VaultProviderStateProof(digest=digest, signature="proof:" + digest)

    def is_readable(self, reference: str) -> bool:
        return reference in self.references

    def rebind(self, source_reference: str, target_reference: str) -> None:
        self.references[target_reference] = self.references[source_reference]
        if self.fail_rebind_after_write:
            raise RuntimeError("provider rebind failed after write")

    def rotate(self, target_reference: str) -> str:
        if self.fail_rotation:
            raise RuntimeError("provider rotation failed")
        rotated = target_reference + "/v2"
        self.references[rotated] = self.references[target_reference]
        return rotated

    def rollback(self, source_reference: str, target_reference: str, rotated_reference: str | None) -> None:
        self.references.pop(rotated_reference, None)
        self.references.pop(target_reference, None)


def _bindings():
    from knowledge_platform.catalog import VaultBinding

    return (
        VaultBinding(
            binding_id="credential_app_1",
            source_reference="vault://users/user_1/credentials/feishu-app-app_1",
            target_reference="vault://platform/install_1/credential_app_1",
        ),
        VaultBinding(
            binding_id="grant_grant_1",
            source_reference="vault://users/user_1/credentials/feishu-user-grant-grant_1-v1",
            target_reference="vault://platform/install_1/grant_grant_1",
        ),
    )


def _verify_state_proof(proof) -> bool:
    return proof.signature == "proof:" + proof.digest


def test_vault_rebind_rehearsal_is_non_secret_and_compensates_failures() -> None:
    from knowledge_platform.catalog import run_vault_rebind_rehearsal_with_rollback_probes

    bindings = _bindings()
    vault = _OpaqueVault({binding.source_reference: "opaque" for binding in bindings})
    baseline = vault.state_proof().digest
    result = run_vault_rebind_rehearsal_with_rollback_probes(
        vault,
        bindings,
        source_revision="legacy-vault-1",
        target_revision="platform-vault-1",
        active_revision="legacy-active-1",
        state_proof_verifier=_verify_state_proof,
    )
    assert all(result.report.checks.values())
    assert result.report.retry_idempotent is True
    assert len(result.report.injected_failure_checkpoints) == 3
    assert vault.state_proof().digest != baseline
    serialized = json.dumps(result.to_dict(), ensure_ascii=False, sort_keys=True)
    assert all(binding.source_reference not in serialized for binding in bindings)
    assert all(item.source_reference_digest.startswith("sha256:") for item in result.bindings)
    assert all(item.old_reference_retained for item in result.bindings)


def test_vault_rebind_rehearsal_restores_provider_state_when_rotation_fails() -> None:
    from knowledge_platform.catalog import run_vault_rebind_rehearsal_with_rollback_probes

    bindings = _bindings()
    vault = _OpaqueVault({binding.source_reference: "opaque" for binding in bindings})
    baseline = vault.state_proof().digest
    vault.fail_rotation = True
    with pytest.raises(RuntimeError, match="provider rotation failed"):
        run_vault_rebind_rehearsal_with_rollback_probes(
            vault,
            bindings,
            source_revision="legacy-vault-1",
            target_revision="platform-vault-1",
            active_revision="legacy-active-1",
            failure_checkpoints=("after_rebind",),
            state_proof_verifier=_verify_state_proof,
        )
    assert vault.state_proof().digest == baseline


def test_vault_rebind_rehearsal_compensates_partial_rebind_failure() -> None:
    from knowledge_platform.catalog import run_vault_rebind_rehearsal_with_rollback_probes

    bindings = _bindings()
    vault = _OpaqueVault({binding.source_reference: "opaque" for binding in bindings})
    baseline = vault.state_proof().digest
    vault.fail_rebind_after_write = True
    with pytest.raises(RuntimeError, match="provider rebind failed after write"):
        run_vault_rebind_rehearsal_with_rollback_probes(
            vault,
            bindings,
            source_revision="legacy-vault-1",
            target_revision="platform-vault-1",
            active_revision="legacy-active-1",
            failure_checkpoints=("after_rebind",),
            state_proof_verifier=_verify_state_proof,
        )
    assert vault.state_proof().digest == baseline


def test_vault_rebind_rehearsal_rejects_plaintext_and_duplicate_bindings() -> None:
    from knowledge_platform.catalog import (
        VaultBinding,
        VaultRebindVerificationError,
        run_vault_rebind_rehearsal_with_rollback_probes,
    )

    vault = _OpaqueVault({})
    unsafe = VaultBinding(
        binding_id="credential_1",
        source_reference="vault://users/user_1/credentials/raw-secret",
        target_reference="vault://platform/install_1/credential_1",
    )
    with pytest.raises(VaultRebindVerificationError):
        run_vault_rebind_rehearsal_with_rollback_probes(
            vault,
            (unsafe, unsafe),
            source_revision="legacy-vault-1",
            target_revision="platform-vault-1",
            active_revision="legacy-active-1",
            state_proof_verifier=_verify_state_proof,
        )


def test_vault_rebind_rehearsal_requires_independent_state_proof_verification() -> None:
    from knowledge_platform.catalog import VaultRebindVerificationError, run_vault_rebind_rehearsal_with_rollback_probes

    bindings = _bindings()
    vault = _OpaqueVault({binding.source_reference: "opaque" for binding in bindings})
    with pytest.raises(VaultRebindVerificationError, match="independently verified"):
        run_vault_rebind_rehearsal_with_rollback_probes(
            vault,
            bindings,
            source_revision="legacy-vault-1",
            target_revision="platform-vault-1",
            active_revision="legacy-active-1",
            state_proof_verifier=lambda proof: False,
        )
