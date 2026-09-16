# Credential continuity rebind v1

An installed Knowledge runtime exposes:

```sh
python -m knowledge_platform.distribution.credential_rebind \
  --source-home /absolute/offline/puddingclaw-home \
  --owner local \
  --target-root /absolute/puddingknowledge-vault-root \
  --receipt /absolute/private/credential-rebind.json \
  [--allow-keychain] [--slot <name> ...]
```

The command decrypts the legacy PuddingClaw credential envelopes backing the
Knowledge-owned configuration slots and re-encrypts the secret values into the
local Knowledge credential vault (`knowledge_platform.local.vault`), one
receipt entry per slot. It is an offline step of the repository-split
migration: the legacy Home is strictly read-only, plaintext secret values exist
only in process memory, and every failure mode is fail-closed. The exit code
is 0 only when `credential_continuity_verified=true`; the receipt is written
atomically (part file, fsync, rename, directory fsync) on every admitted run,
including failed ones. Input validation failures — bad paths, an unknown
`--slot`, a missing authority manifest — reject before any receipt.

## Legacy source formats

The module re-implements a minimal read-only reader; the PuddingClaw
repository is never imported. Provenance is pinned in the module docstring:

- Layout (PuddingClaw `backend/runtime_identity/paths.py:172-182`, `:252-255`):
  `credentials_root` is `<home>/users/<owner>/credentials`, the authority
  manifest is `<home>/.vault-keys/<owner>.provider.json`, the file key is
  `<home>/.vault-keys/<owner>.key`, and each per-profile directory
  `<provider>/<profile>/` holds `vault.enc` plus non-secret `profile.json`.
- Key authority (`backend/runtime_identity/profiles.py:137-439`): the manifest
  pins `{schema_version: 1, owner_user_id, provider, key_id, created_at}` with
  provider in `{keychain, file, environment}` and
  `key_id = "sha256:" + sha256(key).hexdigest()[:32]`. The file provider reads
  32 raw bytes from the key file. The environment provider decodes
  `PUDDINGCLAW_MASTER_KEY` as 64-char hex or base64url. The keychain provider
  shells out to `security find-generic-password -s "PuddingClaw Credential
  Vault" -a <owner> -w` and is only consulted behind an explicit
  `--allow-keychain`; without the flag a keychain-pinned manifest fails
  closed. A resolved key whose id does not match the manifest fails closed.
- Envelope (`backend/runtime_identity/profiles.py:442-481`, `:542-544`):
  AES-256-GCM, 12-byte nonce, JSON envelope `{version, algorithm:
  "AES-256-GCM", key_id, nonce, ciphertext}` (base64 fields); open accepts
  version 1 or 2 and version 2 pins `key_id`; AAD is
  `puddingclaw:v1:<owner>:<provider>:<profile_id>`.
- Registry payload (`backend/provider_registry.py:124-129`, `:185-274`):
  `provider-registry.enc` opens under AAD provider `provider-registry`,
  profile `default`, to plaintext JSON
  `{"version": 1, "credentials": {<ref>: {"value": ..., "updated_at": ...}}}`.
  Every slot this tool migrates resolves to ref-keys in this payload.

## Slot mapping

The slot authority is
`docs/knowledge-platform/config-credential-ownership.yaml` (spec v0.7, 17
entries). The mapping below is embedded in the module (`_SLOTS`); the YAML is
never read at runtime. Value slots match ref-keys inside the decrypted
provider registry. Reference slots carry only credential references; the
secret bytes they point at are rebound by the value slots in `covered_by`, so
they never materialize a vault entry and are reported `not-applicable`.

| slot | secret fields | source selector | target vault key |
| --- | --- | --- | --- |
| `database.password` | password, password_ref | exact `database-config` | `database.password` |
| `knowledge.feishu.app_credentials` | app_secret, access_token, refresh_token, verifier | prefixes `feishu-app-`, `feishu-user-grant-`, `feishu-oauth-verifier-` | `knowledge.feishu.app_credentials` |
| `knowledge.parsers.items.llama_parse_cloud` | api_key | exact `parser-llama_parse_cloud` | `knowledge.parsers.items.llama_parse_cloud.api_key` |
| `knowledge.parsers.items.mineru_cloud_light` | api_key | exact `parser-mineru_cloud_light` | `knowledge.parsers.items.mineru_cloud_light.api_key` |
| `knowledge.parsers.items.mineru_cloud_precise` | api_key | exact `parser-mineru_cloud_precise` | `knowledge.parsers.items.mineru_cloud_precise.api_key` |
| `providers.*.credentials` | api_key, token | class `<provider>-credential-<name>` | `providers.credentials` |
| `knowledge.llm_wiki.gbrain.*` | provider_credential_ref | reference → `providers.*.credentials` | not materialized |
| `knowledge.mineru.*` | api_key, credential_ref | reference → parser items + `providers.*.credentials` | not materialized |
| `knowledge.multimodal_index.*` | provider_credential_ref | reference → `providers.*.credentials` | not materialized |
| `rag.*` | provider_credential_ref | reference → `providers.*.credentials` | not materialized |
| `vanna.*` | llm.api_key, embedding.api_key, database.password_ref | reference → `providers.*.credentials` + `database.password` | not materialized |

Target key derivation is deterministic: drop every `*` path segment from the
slot name; credential-set bundle slots (prefix/class) keep that stem, single
value slots append the primary secret field unless the stem already ends with
it. A slot matched to exactly one ref stores that raw value; a slot matched to
several refs stores one canonical JSON bundle `{"format":
"puddingknowledge-credential-bundle/v1", "entries": {<ref>: <value>}}` with
sorted keys. Matched refs whose stored value is empty count as absent.

The `providers.*.credentials` class sweeps every `<provider>-credential-<name>`
entry into the Platform vault bundle. Offline, the credential home cannot
attribute providers to workloads (those bindings live in the legacy config
database), so the split is realized as: the Harness retains ownership and
rotation of LLM provider credentials in its own untouched vault, while the
Platform receives the shared provider credential material its workloads
(gbrain, multimodal index, rag, vanna, cloud parsers) reference. Refs no slot
claims are listed under `retained_source_refs` with a reason
(`harness_owned`, `catalog_wave_owned` for per-connection `database-source-*`
passwords rebound with the Catalog wave, or `unrecognized_ref`).

## Receipt contract

The receipt is one canonical `puddingknowledge-credential-rebind/v1` JSON
object, deterministic for identical inputs: no timestamps, sorted keys, sorted
lists. Each `rebinds` entry carries the four installation-manifest contract
keys — `slot`, `source_ref_digest` (`sha256:` of the exact source envelope
bytes searched), `target_ref` (`credential://users/<owner>/credentials/<key>`)
and `status` — plus `secret_fields`, and for rebound entries the materialized
`vault_ref` (`vault://...` form), `source_refs`, `bundle` and
`materialized=true`. Statuses:

- `rebound`: decrypted from the source, written to the target vault, read back
  and compared in memory. A target key already holding the same value is an
  idempotent `rebound` without a write.
- `absent`: the slot's selector matched no secret in the source. The four
  contract keys stay meaningful (`source_ref_digest` digests the envelope that
  was searched, or the empty-bytes digest when no registry exists). Absent
  slots never fail the run; verification reflects present slots only.
- `failed`: a present slot was refused — target conflict (the key holds a
  different value and is never overwritten), a vault budget overflow, a
  read-back mismatch, or a run-level authority/envelope failure. Any `failed`
  entry forces `credential_continuity_verified=false`.
- `not-applicable`: reference slots (with `covered_by`) and never a failure.

`skipped` enumerates `authorization-flows/*.enc` with their digests: transient
in-flight OAuth flows are never migrated. `not_applicable_artifacts`
enumerates per-profile `vault.enc` state archives and
`skill-secrets/registry.enc` (both Harness-owned) with digests.
`unrecognized_source_artifacts` reports anything else under the credentials
root; non-secret metadata (authority files, profile registries, locks) is
fingerprinted but not listed. `credential_continuity_verified=true` requires
every present slot rebound, no failed entry, no run-level error and a
byte-identical source.

## Fail-closed semantics and plaintext hygiene

Every source file in scope — the authority manifest, the file key, the full
credentials root and the skill-secrets registry — is sha256 fingerprinted
before the run and again after; drift aborts unverified, and the manifest, key
and registry bytes are re-pinned to their first fingerprint when read. A
corrupted or foreign-key envelope fails before any target write, so nothing is
materialized. Retrying the exact command on a completed target is a no-op
success producing a byte-identical receipt. Plaintext secrets never reach the
receipt, stdout, stderr or any file other than the target vault's own
encrypted payload; error messages are fixed strings with no values.

Test coverage lives in `backend/tests/test_credential_rebind.py`:

```sh
cd backend && TMPDIR=/private/tmp/pk-tmpdir \
  uv run --no-sync python -m pytest -q tests/test_credential_rebind.py
```

The synthetic legacy Home seals envelopes with the same AES-256-GCM/AAD
algorithm; the keychain path is exercised only through an injected reader, so
tests never touch the real macOS Keychain. This tool does not acquire writer
authority, advance the installation Migration Manifest, or grant
PREPARED/CUTOVER; the Harness installation manifest consumes the rebound
entries as evidence.
