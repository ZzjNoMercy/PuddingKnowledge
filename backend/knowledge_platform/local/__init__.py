"""Explicit loopback-only local Platform runtime."""

from .vault import CredentialVault, CredentialVaultError, LocalCredentialStore

__all__ = ["CredentialVault", "CredentialVaultError", "LocalCredentialStore"]
