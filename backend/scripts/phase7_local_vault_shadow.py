"""Run the Vault rebind/rotation contract against the local encrypted Vault.

This is a development shadow only. It creates synthetic opaque values in an
isolated temporary local Credential Vault, so it never reads or mutates the
developer's configured credentials. The report deliberately does not claim
production Vault or installation-copy verification.
"""

from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import tempfile
from pathlib import Path

from knowledge_platform.catalog import (
    LocalCredentialVaultOperator,
    VaultBinding,
    VaultProviderStateProof,
    run_vault_rebind_rehearsal_with_rollback_probes,
)
from knowledge_platform.local.vault import CredentialVault, LocalCredentialStore


def _verify_factory(proof_key: bytes):
    def verify(proof: VaultProviderStateProof) -> bool:
        expected = hmac.new(proof_key, proof.digest.encode("ascii"), hashlib.sha256).hexdigest()
        return hmac.compare_digest(proof.signature, expected)

    return verify


def run(output: Path) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="puddingknowledge-local-vault-shadow-") as temp_root:
        # tempfile may expose the macOS /var alias; pass the canonical,
        # explicitly-created shadow home through the vault symlink boundary.
        root = Path(temp_root).resolve()
        store = LocalCredentialStore(root, owner_user_id="local-vault-shadow")
        isolated_vault_key = hashlib.sha256(b"phase7-local-vault-shadow-key").digest()
        store.vault = CredentialVault(isolated_vault_key)
        source_references = (
            store.put("source-app", "synthetic-opaque-app-value"),
            store.put("source-grant", "synthetic-opaque-grant-value"),
        )
        bindings = tuple(
            VaultBinding(
                binding_id=binding_id,
                source_reference=source_reference,
                target_reference=source_reference.replace("source-", "platform-", 1),
            )
            for binding_id, source_reference in (
                ("credential_app", source_references[0]),
                ("credential_grant", source_references[1]),
            )
        )
        target_references = tuple(binding.target_reference for binding in bindings)
        rotated_references = tuple(f"{reference}-rotated-v2" for reference in target_references)
        proof_key = b"phase7-local-vault-shadow-proof-key"
        operator = LocalCredentialVaultOperator(
            store,
            proof_key=proof_key,
            tracked_references=(*source_references, *target_references, *rotated_references),
        )
        result = run_vault_rebind_rehearsal_with_rollback_probes(
            operator,
            bindings,
            source_revision="local-vault-source-1",
            target_revision="local-vault-platform-1",
            active_revision="local-vault-active-1",
            state_proof_verifier=_verify_factory(proof_key),
        )
        reopened_store = LocalCredentialStore(root, owner_user_id="local-vault-shadow")
        reopened_store.vault = CredentialVault(isolated_vault_key)
        reopened_operator = LocalCredentialVaultOperator(
            reopened_store,
            proof_key=proof_key,
            tracked_references=(*source_references, *target_references, *rotated_references),
        )
        final_proof = operator.state_proof()
        reopened_proof = reopened_operator.state_proof()
        if final_proof.digest != reopened_proof.digest:
            raise RuntimeError("local Credential Vault state did not survive reopen")
        payload = result.to_dict()
        payload.update(
            {
                "status": "LOCAL_VAULT_SHADOW_PASS_NOT_PRODUCTION_VERIFICATION",
                "provider_backend": operator.backend_name,
                "isolated_store": True,
                "encrypted_store_created": store.path.is_file(),
                "key_authority": "explicit isolated shadow key; macOS Keychain not invoked",
                "restart_reopen_verified": True,
                "production_vault_adapter_verified": False,
                "production_installation_copy_verified": False,
                "activation_allowed": False,
                "scope_boundary": "synthetic opaque values in a temporary local store; no configured credentials read or changed",
            }
        )

    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return payload


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("artifacts/phase0b-local-catalog/phase7-local-vault-shadow/phase7-local-vault-shadow-report.json"),
    )
    args = parser.parse_args()
    payload = run(args.output)
    print(
        json.dumps(
            {
                "status": payload["status"],
                "provider_backend": payload["provider_backend"],
                "bindings": len(payload["bindings"]),
                "restart_reopen_verified": payload["restart_reopen_verified"],
                "activation_allowed": payload["activation_allowed"],
                "report": str(args.output),
            },
            ensure_ascii=False,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
