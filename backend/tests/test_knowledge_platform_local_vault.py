from __future__ import annotations

import hashlib
import hmac
from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import subprocess
import sys

import pytest


def test_local_credential_vault_operator_uses_encrypted_store_and_reopens(tmp_path) -> None:
    from knowledge_platform.catalog import (
        LocalCredentialVaultOperator,
        VaultBinding,
        VaultProviderStateProof,
        run_vault_rebind_rehearsal_with_rollback_probes,
    )
    from knowledge_platform.local.vault import CredentialVault, LocalCredentialStore

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


def test_local_vault_isolated_by_owner_and_home(tmp_path) -> None:
    from knowledge_platform.local.vault import CredentialVault, LocalCredentialStore

    key = hashlib.sha256(b"isolation-key").digest()
    first = LocalCredentialStore(tmp_path / "home-a", owner_user_id="owner-a")
    first.vault = CredentialVault(key)
    reference = first.put("provider", "home-owner-secret")

    # The ciphertext itself carries no plaintext credential material.
    assert b"home-owner-secret" not in first.path.read_bytes()

    other_owner = LocalCredentialStore(tmp_path / "home-a", owner_user_id="owner-b")
    other_owner.vault = CredentialVault(key)
    with pytest.raises(ValueError, match="outside the local owner namespace"):
        other_owner.get(reference)

    other_home = LocalCredentialStore(tmp_path / "home-b", owner_user_id="owner-a")
    other_home.vault = CredentialVault(key)
    assert other_home.get(reference) == ""
    assert other_home.path != first.path


def test_local_vault_default_home_uses_owned_namespace_only(tmp_path, monkeypatch) -> None:
    from knowledge_platform.local.vault import LocalCredentialStore

    knowledge_home = tmp_path / "knowledge-home"
    legacy_home = tmp_path / "legacy-home"
    monkeypatch.setenv("PUDDINGKNOWLEDGE_HOME", str(knowledge_home))
    monkeypatch.setenv("PUDDINGCLAW_HOME", str(legacy_home))

    store = LocalCredentialStore(owner_user_id="owner-default")
    assert not knowledge_home.exists()
    reference = store.put("provider", "owned-home-secret")
    reopened = LocalCredentialStore(owner_user_id="owner-default")
    assert reopened.get(reference) == "owned-home-secret"
    assert str(store.path).startswith(str(knowledge_home))
    assert not legacy_home.exists()


def test_local_vault_rejects_symlinked_home_and_payload(tmp_path) -> None:
    from knowledge_platform.local.vault import CredentialVault, LocalCredentialStore

    real_home = tmp_path / "real-home"
    linked_home = tmp_path / "linked-home"
    linked_home.symlink_to(real_home, target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        LocalCredentialStore(linked_home, owner_user_id="owner")

    store = LocalCredentialStore(real_home, owner_user_id="owner")
    store.vault = CredentialVault(hashlib.sha256(b"symlink-key").digest())
    store.put("provider", "secret")
    outside = tmp_path / "outside-vault"
    outside.write_bytes(store.path.read_bytes())
    store.path.unlink()
    store.path.symlink_to(outside)
    with pytest.raises(ValueError, match="must not contain a symlink"):
        store.get(store.reference_prefix + "provider")


def test_local_vault_serializes_concurrent_read_modify_write(tmp_path) -> None:
    from knowledge_platform.local.vault import CredentialVault, LocalCredentialStore

    key = hashlib.sha256(b"concurrency-key").digest()
    stores = [LocalCredentialStore(tmp_path, owner_user_id="owner") for _ in range(8)]
    for store in stores:
        store.vault = CredentialVault(key)
    with ThreadPoolExecutor(max_workers=8) as executor:
        references = list(executor.map(lambda pair: pair[0].put(pair[1], pair[1]), zip(stores, (f"item-{i}" for i in range(8)))))

    reopened = LocalCredentialStore(tmp_path, owner_user_id="owner")
    reopened.vault = CredentialVault(key)
    assert {reopened.get(reference) for reference in references} == {f"item-{i}" for i in range(8)}


def test_local_vault_same_instance_serializes_first_key_creation(tmp_path) -> None:
    from knowledge_platform.local.vault import LocalCredentialStore

    store = LocalCredentialStore(tmp_path, owner_user_id="owner")
    with ThreadPoolExecutor(max_workers=8) as executor:
        vaults = list(executor.map(lambda _: store.vault, range(8)))

    assert store._key_path.is_file()
    assert len({vault._key for vault in vaults}) == 1


def test_local_vault_bad_lock_does_not_corrupt_another_instance_lock(tmp_path) -> None:
    from knowledge_platform.local.vault import CredentialVaultError, LocalCredentialStore

    first = LocalCredentialStore(tmp_path, owner_user_id="owner")
    second = LocalCredentialStore(tmp_path, owner_user_id="owner")
    lock_path = first._lock_path
    with first._exclusive_lock():
        lock_path.unlink()
        os.mkfifo(lock_path)
        with pytest.raises(CredentialVaultError, match="lock must be a regular file"):
            with second._exclusive_lock():
                pass
        lock_path.unlink()
        lock_path.touch()


def test_local_vault_nonregular_lock_fails_without_blocking_subprocess(tmp_path) -> None:
    from knowledge_platform.local.vault import CredentialVaultError, LocalCredentialStore

    store = LocalCredentialStore(tmp_path, owner_user_id="owner")
    store.path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(store._lock_path)
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from knowledge_platform.local.vault import CredentialVaultError, LocalCredentialStore
store = LocalCredentialStore(Path(sys.argv[2]), owner_user_id="owner")
try:
    store.put("provider", "value")
except CredentialVaultError as error:
    assert "lock must be a regular file" in str(error)
else:
    raise AssertionError("non-regular lock was accepted")
"""
    completed = subprocess.run(
        [sys.executable, "-c", script, str(Path(__file__).resolve().parents[1]), str(tmp_path)],
        capture_output=True,
        text=True,
        timeout=2,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_local_vault_nested_lock_exception_restores_depth(tmp_path) -> None:
    from knowledge_platform.local.vault import LocalCredentialStore

    store = LocalCredentialStore(tmp_path, owner_user_id="owner")
    with pytest.raises(RuntimeError, match="nested failure"):
        with store._exclusive_lock():
            with store._exclusive_lock():
                raise RuntimeError("nested failure")

    assert getattr(store._lock_state, "depth", 0) == 0
    store.put("provider", "value")
    assert store.get("provider") == "value"


@pytest.mark.parametrize(("special_file", "operation"), (("key", "get"), ("payload", "put")))
def test_local_vault_key_and_payload_fifo_fail_without_blocking_subprocess(
    tmp_path, special_file: str, operation: str
) -> None:
    script = """
import os
import sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from knowledge_platform.local.vault import CredentialVaultError, LocalCredentialStore
store = LocalCredentialStore(Path(sys.argv[2]), owner_user_id="owner")
if sys.argv[3] == "key":
    store.path.parent.mkdir(parents=True, exist_ok=True)
    os.mkfifo(store._key_path)
    store.path.write_bytes(b"placeholder")
else:
    store.vault
    os.mkfifo(store.path)
try:
    if sys.argv[4] == "get":
        store.get("provider")
    else:
        store.put("provider", "value")
except CredentialVaultError:
    pass
else:
    raise AssertionError("special vault file was accepted")
"""
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(Path(__file__).resolve().parents[1]),
            str(tmp_path),
            special_file,
            operation,
        ],
        capture_output=True,
        text=True,
        timeout=2,
        env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


def test_local_vault_rejects_relative_home_and_non_regular_lock(tmp_path, monkeypatch) -> None:
    from knowledge_platform.local import vault as vault_module
    from knowledge_platform.local.vault import CredentialVaultError, LocalCredentialStore

    with pytest.raises(ValueError, match="absolute path"):
        LocalCredentialStore("relative-knowledge-home", owner_user_id="owner")
    monkeypatch.setenv("PUDDINGKNOWLEDGE_HOME", "relative-env-home")
    with pytest.raises(ValueError, match="absolute path"):
        LocalCredentialStore(owner_user_id="owner")

    monkeypatch.delenv("PUDDINGKNOWLEDGE_HOME")
    store = LocalCredentialStore(tmp_path, owner_user_id="owner")
    lock_path = store.path.parent / ".vault.lock"
    store.path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.touch()
    lock_path.unlink()
    # A FIFO is openable with O_RDWR, so this exercises the post-open fstat
    # check instead of merely testing an OS-level open failure.
    import os

    os.mkfifo(lock_path)
    with pytest.raises(CredentialVaultError, match="lock must be a regular file"):
        store.put("provider", "value")

    original_fcntl = vault_module.fcntl
    vault_module.fcntl = None
    try:
        lock_path.unlink()
        with pytest.raises(CredentialVaultError, match="file-locking platform"):
            store.put("provider", "value")
    finally:
        vault_module.fcntl = original_fcntl
