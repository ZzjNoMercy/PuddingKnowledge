"""Offline credential continuity rebind from a legacy PuddingClaw Home.

This command decrypts the legacy PuddingClaw credential envelopes backing the
Knowledge-owned configuration slots and re-encrypts the secret values into the
local Knowledge credential vault, one receipt entry per slot.  The legacy Home
is strictly read-only: every source file in scope is sha256 fingerprinted
before and after the run, and any drift aborts with
``credential_continuity_verified=false``.  Plaintext secret values exist only
in process memory; they never reach the receipt, the standard output or any
file other than the target vault's own encrypted payload.  Every failure mode
is fail-closed: an unresolved key authority, an unreadable envelope, a target
conflict or a source mutation refuses the affected slots and verifies nothing.

Provenance for the captured legacy formats (a read-only re-implementation; the
PuddingClaw repository is never imported, matching the captured-resolver
precedent in ``distribution/document_routes.py``):

- Credential home layout: PuddingClaw ``backend/runtime_identity/paths.py``
  lines 172-182 and 252-255 (``credentials_root`` is
  ``<home>/users/<owner>/credentials``, the authority manifest is
  ``<home>/.vault-keys/<owner>.provider.json``, the file key is
  ``<home>/.vault-keys/<owner>.key`` and each per-profile directory
  ``<provider>/<profile>/`` holds ``vault.enc`` plus non-secret
  ``profile.json``).
- Master key authority: ``backend/runtime_identity/profiles.py`` lines
  137-439 (manifest ``{schema_version: 1, owner_user_id, provider, key_id,
  created_at}`` with provider in ``{keychain, file, environment}``; ``key_id``
  is ``"sha256:" + sha256(key).hexdigest()[:32]`` at lines 361-363; the
  environment provider decodes ``PUDDINGCLAW_MASTER_KEY`` as 64-char hex or
  base64url at lines 341-359; the file provider reads 32 raw bytes at lines
  368-383; the macOS Keychain lookup is lines 385-412).  A manifest whose
  provider cannot be resolved locks the run; the keychain provider is only
  consulted behind an explicit ``--allow-keychain``.
- Envelope format: ``backend/runtime_identity/profiles.py`` lines 442-481 and
  542-544 (AES-256-GCM with a 12-byte nonce; JSON envelope ``{version,
  algorithm: "AES-256-GCM", key_id, nonce, ciphertext}`` with base64 fields;
  open accepts version 1 or 2 and version 2 pins ``key_id``; AAD is
  ``puddingclaw:v1:<owner>:<provider>:<profile_id>``).
- Provider registry payload: ``backend/provider_registry.py`` lines 124-129
  and 185-274 (``provider-registry.enc`` opens under AAD provider
  ``provider-registry``/profile ``default`` to plaintext JSON
  ``{"version": 1, "credentials": {<ref>: {"value": ..., "updated_at": ...}}}``).
- Legacy ref-key conventions: ``backend/provider_registry.py`` line 1152
  (``<provider>-credential-<name>``), ``backend/knowledge/parsers/registry.py``
  line 172 (``parser-<parser_id>``), ``backend/config.py`` lines 685, 1370 and
  1755 (``database-config``), ``backend/knowledge/connectors/feishu.py`` lines
  141, 162, 486, 611 and 728 (``feishu-app-*``, ``feishu-oauth-verifier-*``
  and ``feishu-user-grant-*-v<n>``).
- Slot authority: ``docs/knowledge-platform/config-credential-ownership.yaml``
  (``agent-knowledge-platform-config-credential-ownership/v1``, spec v0.7, 17
  entries; an identical copy ships in this repository).  The mapping is
  embedded in ``_SLOTS`` below; the YAML is the authority and is deliberately
  never read at runtime.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
from pathlib import Path
import re
import stat
import subprocess

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .catalog_snapshot import _path
from ..local.vault import LocalCredentialStore

FORMAT = "puddingknowledge-credential-rebind/v1"
BUNDLE_FORMAT = "puddingknowledge-credential-bundle/v1"
_MASTER_KEY_ENV = "PUDDINGCLAW_MASTER_KEY"
_KEYCHAIN_SERVICE = "PuddingClaw Credential Vault"
_PROVIDERS = ("keychain", "file", "environment")
_REGISTRY_AAD = ("provider-registry", "default")
_MAX_ENVELOPE = 8 * 1024 * 1024
_MAX_RECEIPT = 8 * 1024 * 1024
_MAX_SOURCE_FILE = 64 * 1024 * 1024
_MAX_MANIFEST = 64 * 1024
_MAX_VALUE = 1024 * 1024
_HEX_64 = re.compile(r"[0-9a-fA-F]{64}")
_PROVIDER_CREDENTIAL_REF = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9_-]{0,62}-credential-[A-Za-z0-9][A-Za-z0-9_-]{0,62}"
)
_SAFE_OWNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
_SAFE_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_EMPTY_DIGEST = "sha256:" + hashlib.sha256(b"").hexdigest()

# Slot mapping table.  Provenance: docs/knowledge-platform/config-credential-ownership.yaml
# (spec v0.7) — every entry with target_owner=puddingknowledge and non-empty
# secret_fields, plus the split-ownership providers.*.credentials entry.
#
# "exact" and "prefix" and "class" slots are value slots: the selector matches
# ref-keys inside the decrypted provider-registry.enc payload and the matched
# secret values are rebound.  "reference" slots carry only credential
# references (provider_credential_ref / credential_ref / password_ref fields);
# the secret bytes they point at are owned and rebound by the value slots in
# covered_by, so reference slots never materialize a vault entry of their own
# and are reported as not-applicable.
#
# (name, secret_fields, kind, selector, covered_by)
_SLOTS = (
    ("database.password", ("password", "password_ref"), "exact", ("database-config",), ()),
    (
        "knowledge.feishu.app_credentials",
        ("app_secret", "access_token", "refresh_token", "verifier"),
        "prefix",
        ("feishu-app-", "feishu-user-grant-", "feishu-oauth-verifier-"),
        (),
    ),
    ("knowledge.parsers.items.llama_parse_cloud", ("api_key",), "exact", ("parser-llama_parse_cloud",), ()),
    ("knowledge.parsers.items.mineru_cloud_light", ("api_key",), "exact", ("parser-mineru_cloud_light",), ()),
    ("knowledge.parsers.items.mineru_cloud_precise", ("api_key",), "exact", ("parser-mineru_cloud_precise",), ()),
    ("providers.*.credentials", ("api_key", "token"), "class", ("provider-credential",), ()),
    ("knowledge.llm_wiki.gbrain.*", ("provider_credential_ref",), "reference", (), ("providers.*.credentials",)),
    (
        "knowledge.mineru.*",
        ("api_key", "credential_ref"),
        "reference",
        (),
        (
            "knowledge.parsers.items.mineru_cloud_precise",
            "knowledge.parsers.items.mineru_cloud_light",
            "providers.*.credentials",
        ),
    ),
    ("knowledge.multimodal_index.*", ("provider_credential_ref",), "reference", (), ("providers.*.credentials",)),
    ("rag.*", ("provider_credential_ref",), "reference", (), ("providers.*.credentials",)),
    (
        "vanna.*",
        ("llm.api_key", "embedding.api_key", "database.password_ref"),
        "reference",
        (),
        ("providers.*.credentials", "database.password"),
    ),
)
_SLOT_NAMES = tuple(name for name, _fields, _kind, _selector, _covered in _SLOTS)

# Non-secret runtime metadata that may legitimately sit inside credentials_root
# (profiles.py:572-574 and :892-906).  Anything else is reported, never read
# for content, and never migrated.
_METADATA_NAMES = {"credential-profiles.json", "project-bindings.json", ".registry.lock"}
_PROFILE_METADATA_NAMES = {"profile.json", ".profile.lock"}


class CredentialRebindError(ValueError):
    """The legacy source, key authority or target vault refuses the rebind."""


def _encode(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode()


def _digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _key_id(key):
    return "sha256:" + hashlib.sha256(key).hexdigest()[:32]


def _sync_directory(directory):
    descriptor = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _write_receipt(path, data):
    part = _path(str(path) + ".part")
    descriptor = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(part, path)
    _sync_directory(path.parent)


def _read_source(path, limit, *, expected=None):
    """Read one source file without following links and pin its digest."""

    path = _path(path)
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_size > limit:
            raise CredentialRebindError("legacy credential source is not a bounded private file")
        data = bytearray()
        while len(data) <= limit:
            chunk = os.read(descriptor, min(65536, limit + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        if len(data) > limit:
            raise CredentialRebindError("legacy credential source exceeds its budget")
    finally:
        os.close(descriptor)
    payload = bytes(data)
    if expected is not None and _digest(payload) != expected:
        raise CredentialRebindError("legacy credential source changed during the rebind")
    return payload


def _fingerprint_source(home, owner):
    """Digest every source file in scope; the mapping is compared after the run."""

    scope = {}
    manifest = home / ".vault-keys" / f"{owner}.provider.json"
    scope[manifest.relative_to(home).as_posix()] = _digest(_read_source(manifest, _MAX_MANIFEST))
    file_key = home / ".vault-keys" / f"{owner}.key"
    if file_key.exists():
        scope[file_key.relative_to(home).as_posix()] = _digest(_read_source(file_key, 128))
    credentials_root = home / "users" / owner / "credentials"
    if credentials_root.exists():
        if credentials_root.is_symlink() or not credentials_root.is_dir():
            raise CredentialRebindError("legacy credentials root is not a real directory")
        for root, directories, files in os.walk(credentials_root, followlinks=False):
            root_path = Path(root)
            for name in sorted(directories):
                if (root_path / name).is_symlink():
                    raise CredentialRebindError("legacy credentials root must not contain links")
            for name in sorted(files):
                path = root_path / name
                if path.is_symlink():
                    raise CredentialRebindError("legacy credentials root must not contain links")
                relative = path.relative_to(home).as_posix()
                scope[relative] = _digest(_read_source(path, _MAX_SOURCE_FILE))
    skill_registry = home / "users" / owner / "skill-secrets" / "registry.enc"
    if skill_registry.exists():
        scope[skill_registry.relative_to(home).as_posix()] = _digest(_read_source(skill_registry, _MAX_SOURCE_FILE))
    return scope


def _keychain_key(owner):
    result = subprocess.run(
        ["security", "find-generic-password", "-s", _KEYCHAIN_SERVICE, "-a", owner, "-w"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    if result.returncode != 0 or not result.stdout.strip():
        raise CredentialRebindError("legacy Keychain credential authority is unavailable")
    try:
        value = base64.urlsafe_b64decode(result.stdout.strip().encode("ascii"))
    except (ValueError, UnicodeError) as error:
        raise CredentialRebindError("legacy Keychain credential authority is invalid") from error
    if len(value) != 32:
        raise CredentialRebindError("legacy Keychain credential authority is invalid")
    return value


def _resolve_authority(home, owner, fingerprint, *, allow_keychain, environ, keychain_reader):
    raw = _read_source(
        home / ".vault-keys" / f"{owner}.provider.json",
        _MAX_MANIFEST,
        expected=fingerprint[f".vault-keys/{owner}.provider.json"],
    )
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialRebindError("legacy credential authority manifest is not readable") from error
    provider = str(manifest.get("provider") or "") if isinstance(manifest, dict) else ""
    expected_key_id = str(manifest.get("key_id") or "") if isinstance(manifest, dict) else ""
    if (
        not isinstance(manifest, dict)
        or manifest.get("schema_version") != 1
        or provider not in _PROVIDERS
        or manifest.get("owner_user_id") != owner
        or not expected_key_id
    ):
        raise CredentialRebindError("legacy credential authority manifest is invalid")
    if provider == "file":
        file_key = f".vault-keys/{owner}.key"
        if file_key not in fingerprint:
            raise CredentialRebindError("legacy file credential authority has no key file")
        key = _read_source(home / ".vault-keys" / f"{owner}.key", 128, expected=fingerprint[file_key])
        if len(key) != 32:
            raise CredentialRebindError("legacy file credential authority key is invalid")
    elif provider == "environment":
        encoded = str(environ.get(_MASTER_KEY_ENV, "")).strip()
        if not encoded:
            raise CredentialRebindError(f"legacy environment credential authority requires {_MASTER_KEY_ENV}")
        try:
            key = (
                bytes.fromhex(encoded)
                if _HEX_64.fullmatch(encoded)
                else base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            )
        except (ValueError, UnicodeError) as error:
            raise CredentialRebindError("legacy environment credential authority key is invalid") from error
        if len(key) != 32:
            raise CredentialRebindError("legacy environment credential authority key is invalid")
    else:
        if not allow_keychain:
            raise CredentialRebindError(
                "legacy credential authority is pinned to the macOS Keychain; rerun with --allow-keychain"
            )
        key = keychain_reader(owner)
    if _key_id(key) != expected_key_id:
        raise CredentialRebindError("legacy credential authority key does not match its manifest")
    return provider, expected_key_id, key


def _open_envelope(key, envelope, *, owner, provider, profile_id):
    try:
        value = json.loads(envelope.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialRebindError("legacy credential envelope is not readable") from error
    if (
        not isinstance(value, dict)
        or value.get("version") not in (1, 2)
        or value.get("algorithm") != "AES-256-GCM"
    ):
        raise CredentialRebindError("legacy credential envelope is unsupported")
    if value.get("version") == 2 and value.get("key_id") != _key_id(key):
        raise CredentialRebindError("legacy credential envelope belongs to another key authority")
    aad = f"puddingclaw:v1:{owner}:{provider}:{profile_id}".encode()
    try:
        nonce = base64.b64decode(value["nonce"], validate=True)
        ciphertext = base64.b64decode(value["ciphertext"], validate=True)
    except (KeyError, TypeError, ValueError) as error:
        raise CredentialRebindError("legacy credential envelope is malformed") from error
    try:
        return AESGCM(key).decrypt(nonce, ciphertext, aad)
    except (InvalidTag, ValueError) as error:
        raise CredentialRebindError("legacy credential envelope cannot be decrypted") from error


def _open_registry(home, owner, fingerprint, key):
    relative = f"users/{owner}/credentials/provider-registry.enc"
    if relative not in fingerprint:
        return None, {}
    envelope = _read_source(
        home / "users" / owner / "credentials" / "provider-registry.enc",
        _MAX_ENVELOPE,
        expected=fingerprint[relative],
    )
    raw = _open_envelope(key, envelope, owner=owner, provider=_REGISTRY_AAD[0], profile_id=_REGISTRY_AAD[1])
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise CredentialRebindError("legacy provider registry payload is not readable") from error
    if not isinstance(payload, dict) or payload.get("version") != 1 or not isinstance(payload.get("credentials"), dict):
        raise CredentialRebindError("legacy provider registry payload is invalid")
    return _digest(envelope), payload["credentials"]


def _target_key(name, secret_fields, kind):
    """Deterministic vault key: drop ``*`` segments, then append the primary
    secret field unless the slot already names it.  Credential-set bundle
    slots (prefix/class) keep their bare stem."""

    stem = ".".join(segment for segment in name.split(".") if segment != "*")
    if kind in ("prefix", "class"):
        key = stem
    else:
        field = secret_fields[0]
        key = stem if stem == field or stem.endswith("." + field) else stem + "." + field
    if not _SAFE_KEY.fullmatch(key):
        raise CredentialRebindError("slot mapping produced an unsafe vault key")
    return key


def _matched_refs(kind, selector, credentials):
    if kind == "exact":
        return [ref for ref in selector if ref in credentials]
    if kind == "prefix":
        return sorted(ref for ref in credentials if any(ref.startswith(prefix) for prefix in selector))
    return sorted(ref for ref in credentials if _PROVIDER_CREDENTIAL_REF.fullmatch(ref))


def _slot_value(refs, credentials):
    """Materialize the value for one slot; malformed selected entries refuse."""

    values = {}
    for ref in refs:
        item = credentials[ref]
        if not isinstance(item, dict) or not isinstance(item.get("value"), str):
            raise CredentialRebindError("legacy provider registry entry is malformed")
        values[ref] = item["value"]
    values = {ref: value for ref, value in values.items() if value}
    if not values:
        return None, []
    ordered = sorted(values)
    if len(ordered) == 1:
        return values[ordered[0]], ordered
    bundle = {"format": BUNDLE_FORMAT, "entries": {ref: values[ref] for ref in ordered}}
    return json.dumps(bundle, ensure_ascii=False, sort_keys=True, separators=(",", ":")), ordered


def _classify_artifacts(owner, fingerprint):
    """Bucket every fingerprinted artifact; content is never inspected here."""

    prefix = f"users/{owner}/credentials/"
    skipped, not_applicable, metadata, unrecognized = [], [], [], []
    for relative in sorted(fingerprint):
        digest = fingerprint[relative]
        if relative == f"users/{owner}/skill-secrets/registry.enc":
            not_applicable.append(
                {"artifact": relative, "status": "not-applicable", "reason": "harness_owned_skill_secrets",
                 "source_ref_digest": digest}
            )
        elif not relative.startswith(prefix):
            metadata.append(relative)
        else:
            inner = relative[len(prefix):]
            parts = inner.split("/")
            if inner == "provider-registry.enc" or inner in _METADATA_NAMES:
                metadata.append(relative)
            elif len(parts) == 2 and parts[0] == "authorization-flows" and parts[1].endswith(".enc"):
                skipped.append(
                    {"artifact": relative, "status": "skipped", "reason": "transient_in_flight_oauth_flow",
                     "source_ref_digest": digest}
                )
            elif len(parts) == 3 and parts[2] == "vault.enc":
                not_applicable.append(
                    {"artifact": relative, "status": "not-applicable",
                     "reason": "harness_owned_provider_profile_state", "source_ref_digest": digest}
                )
            elif len(parts) == 3 and parts[2] in _PROFILE_METADATA_NAMES:
                metadata.append(relative)
            else:
                unrecognized.append({"artifact": relative, "status": "unrecognized", "source_ref_digest": digest})
    return skipped, not_applicable, metadata, unrecognized


def _retained_refs(matched, credentials):
    """Refs no slot selector claims stay in the legacy vault, with a reason."""

    retained = []
    for ref in sorted(set(credentials) - set(matched)):
        if ref.startswith("database-source-"):
            reason = "catalog_wave_owned"
        elif ref.startswith("web-search-") or ref.startswith("mcp:") or ref == "puddingclaw-langsmith":
            reason = "harness_owned"
        else:
            reason = "unrecognized_ref"
        retained.append({"ref": ref, "reason": reason})
    return retained


def _select_slots(slots):
    if slots is None:
        return list(_SLOTS)
    selected = []
    for name in slots:
        if name not in _SLOT_NAMES:
            raise CredentialRebindError(f"unknown credential slot: {name}")
        if name not in selected:
            selected.append(name)
    return [slot for slot in _SLOTS if slot[0] in selected]


def rebind_credentials(
    source_home,
    owner,
    target_root,
    receipt,
    *,
    allow_keychain=False,
    slots=None,
    environ=None,
    keychain_reader=None,
    _before_final_fingerprint=None,
):
    """Rebind every selected slot from the legacy Home into the target vault.

    The receipt is written atomically on every admitted run, including failed
    ones; it is deterministic, so an exact retry produces an identical file.
    """

    home = _path(source_home)
    if not home.is_dir():
        raise CredentialRebindError("legacy source Home is not a directory")
    receipt_path = _path(receipt)
    if not receipt_path.parent.is_dir():
        raise CredentialRebindError("receipt directory does not exist")
    owner = str(owner or "")
    if not _SAFE_OWNER.fullmatch(owner):
        raise CredentialRebindError("owner_user_id must be a safe local identifier")
    try:
        store = LocalCredentialStore(target_root, owner_user_id=owner)
    except ValueError as error:
        raise CredentialRebindError(str(error)) from error
    selected = _select_slots(slots)
    fingerprint = _fingerprint_source(home, owner)
    skipped, not_applicable_artifacts, _metadata, unrecognized = _classify_artifacts(owner, fingerprint)
    environ = os.environ if environ is None else environ
    entries = []
    retained = []
    error = ""
    authority_provider = None
    authority_key_id = None
    registry_digest = _EMPTY_DIGEST
    try:
        authority_provider, authority_key_id, key = _resolve_authority(
            home,
            owner,
            fingerprint,
            allow_keychain=allow_keychain,
            environ=environ,
            keychain_reader=keychain_reader or _keychain_key,
        )
        opened = _open_registry(home, owner, fingerprint, key)
        if opened[0] is not None:
            registry_digest = opened[0]
        credentials = opened[1]
        for name, secret_fields, kind, selector, covered_by in selected:
            target_ref = f"credential://users/{owner}/credentials/{_target_key(name, secret_fields, kind)}"
            vault_ref = f"vault://users/{owner}/credentials/{_target_key(name, secret_fields, kind)}"
            base = {"slot": name, "target_ref": target_ref, "secret_fields": list(secret_fields)}
            if kind == "reference":
                entries.append(
                    {**base, "source_ref_digest": registry_digest, "status": "not-applicable",
                     "materialized": False, "covered_by": list(covered_by)}
                )
                continue
            refs = _matched_refs(kind, selector, credentials)
            value, present = _slot_value(refs, credentials)
            if value is None:
                entries.append(
                    {**base, "source_ref_digest": registry_digest, "status": "absent",
                     "materialized": False, "source_refs": []}
                )
                continue
            entry = {
                **base,
                "source_ref_digest": registry_digest,
                "source_refs": present,
                "bundle": len(present) > 1,
                "vault_ref": vault_ref,
            }
            if len(value.encode("utf-8")) > _MAX_VALUE:
                entries.append({**entry, "status": "failed", "materialized": False, "reason": "value_exceeds_vault_budget"})
                continue
            existing = store.get(vault_ref)
            if existing and not hmac.compare_digest(existing.encode("utf-8"), value.encode("utf-8")):
                entries.append({**entry, "status": "failed", "materialized": False, "reason": "target_conflict"})
                continue
            if not existing:
                store.put(_target_key(name, secret_fields, kind), value)
                if not hmac.compare_digest(store.get(vault_ref).encode("utf-8"), value.encode("utf-8")):
                    entries.append({**entry, "status": "failed", "materialized": False, "reason": "target_readback_mismatch"})
                    continue
            entries.append({**entry, "status": "rebound", "materialized": True})
        all_matched = []
        for _name, _fields, kind, selector, _covered in _SLOTS:
            if kind != "reference":
                all_matched.extend(_matched_refs(kind, selector, credentials))
        retained = _retained_refs(all_matched, credentials)
    except CredentialRebindError as exc:
        error = exc.args[0] if exc.args and isinstance(exc.args[0], str) else "credential rebind rejected"
        entries = []
        for name, secret_fields, kind, _selector, covered_by in selected:
            target_ref = f"credential://users/{owner}/credentials/{_target_key(name, secret_fields, kind)}"
            base = {"slot": name, "target_ref": target_ref, "secret_fields": list(secret_fields)}
            if kind == "reference":
                entries.append(
                    {**base, "source_ref_digest": _EMPTY_DIGEST, "status": "not-applicable",
                     "materialized": False, "covered_by": list(covered_by)}
                )
            else:
                entries.append({**base, "source_ref_digest": _EMPTY_DIGEST, "status": "failed", "materialized": False})
    if _before_final_fingerprint is not None:
        _before_final_fingerprint()
    source_unchanged = _fingerprint_source(home, owner) == fingerprint
    failed = any(entry["status"] == "failed" for entry in entries)
    verified = not error and not failed and source_unchanged
    result = {
        "format": FORMAT,
        "state": "completed" if verified else "failed",
        "credential_continuity_verified": verified,
        "owner_user_id": owner,
        "source": {
            "home": str(home),
            "authority_provider": authority_provider,
            "authority_key_id": authority_key_id,
            "fingerprint": fingerprint,
            "fingerprint_unchanged": source_unchanged,
        },
        "target": {"root": str(store.root)},
        "slots_selected": [slot[0] for slot in selected],
        "rebinds": entries,
        "skipped": skipped,
        "not_applicable_artifacts": not_applicable_artifacts,
        "unrecognized_source_artifacts": unrecognized,
        "retained_source_refs": retained,
        "counts": {
            "rebound": sum(1 for entry in entries if entry["status"] == "rebound"),
            "absent": sum(1 for entry in entries if entry["status"] == "absent"),
            "failed": sum(1 for entry in entries if entry["status"] == "failed"),
            "not_applicable": sum(1 for entry in entries if entry["status"] == "not-applicable"),
            "skipped": len(skipped),
            "retained": len(retained),
        },
    }
    if error:
        result["error"] = error
    if len(_encode(result)) > _MAX_RECEIPT:
        raise CredentialRebindError("credential rebind receipt exceeds its budget")
    _write_receipt(receipt_path, _encode(result) + b"\n")
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-home", required=True)
    parser.add_argument("--owner", required=True)
    parser.add_argument("--target-root", required=True)
    parser.add_argument("--receipt", required=True)
    parser.add_argument("--allow-keychain", action="store_true")
    parser.add_argument("--slot", action="append", dest="slots", default=None)
    args = parser.parse_args(argv)
    try:
        result = rebind_credentials(
            args.source_home,
            args.owner,
            args.target_root,
            args.receipt,
            allow_keychain=args.allow_keychain,
            slots=args.slots,
        )
    except Exception:
        print(
            json.dumps(
                {"format": FORMAT, "state": "failed", "credential_continuity_verified": False,
                 "error_code": "credential_rebind_rejected"},
                sort_keys=True,
            )
        )
        return 1
    summary = {
        "format": FORMAT,
        "state": result["state"],
        "credential_continuity_verified": result["credential_continuity_verified"],
        "counts": result["counts"],
        "receipt": str(_path(args.receipt)),
    }
    print(json.dumps(summary, sort_keys=True))
    return 0 if result["credential_continuity_verified"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
