from __future__ import annotations

import hashlib
import hmac


def test_local_credential_vault_operator_uses_encrypted_store_and_reopens(tmp_path) -> None:
    from knowledge_platform.catalog import (
        LocalCredentialVaultOperator,
        VaultBinding,
        VaultProviderStateProof,
        run_vault_rebind_rehearsal_with_rollback_probes,
    )
    from provider_registry import LocalCredentialStore
    from runtime_identity.profiles import CredentialVault

    proof_key = b"local-vault-test-proof-key"
    store = LocalCredentialStore(tmp_path, owner_user_id="owner-local")
    store.vault = CredentialVault(hashlib.sha256(b"local-vault-test-key").digest())
    source = store.put("source-app", "synthetic-only-value")
    binding = VaultBinding(
        binding_id="credential_app",
        source_reference=source,
        target_reference=source.replace("source-app", "platform-app"),
    )
    target = binding.target_reference
    rotated = f"{target}-rotated-v2"
    operator = LocalCredentialVaultOperator(
        store,
        proof_key=proof_key,
        tracked_references=(source, target, rotated),
    )

    def verify(proof: VaultProviderStateProof) -> bool:
        expected = hmac.new(proof_key, proof.digest.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(proof.signature, expected)

    result = run_vault_rebind_rehearsal_with_rollback_probes(
        operator,
        (binding,),
        source_revision="local-source-1",
        target_revision="local-target-1",
        active_revision="local-active-1",
        state_proof_verifier=verify,
    )

    assert store.path.is_file()
    assert result.report.retry_idempotent is True
    assert result.report.checks["rollback_compensation"] is True
    serialized = str(result.to_dict())
    assert "synthetic-only-value" not in serialized

    reopened = LocalCredentialStore(tmp_path, owner_user_id="owner-local")
    reopened.vault = CredentialVault(hashlib.sha256(b"local-vault-test-key").digest())
    reopened_operator = LocalCredentialVaultOperator(
        reopened,
        proof_key=proof_key,
        tracked_references=(source, target, rotated),
    )
    assert reopened_operator.state_proof().digest == operator.state_proof().digest
