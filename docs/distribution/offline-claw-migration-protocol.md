# Offline Claw migration process protocol v1

An installed Knowledge runtime exposes:

```sh
python -m knowledge_platform.distribution.migrate_from_claw \
  --source-snapshot /absolute/offline \
  --request /absolute/private-request.json --output /absolute/new-delegate-output
```

The request is a private, regular, unlinked JSON file, at most 1 MiB. It has
exactly these fields; duplicate or unknown keys are rejected:

```json
{
  "format": "puddingknowledge-migrate-from-claw-request/v1",
  "installation_id": "installation-1",
  "source_revision": "offline-1",
  "source_schema_revision": "legacy-1",
  "source_catalog": "/absolute/offline/catalog.sqlite3",
  "source_files_root": "/absolute/offline/files",
  "bindings": {"document-id": "docs/body.md"}
}
```

The caller supplies an immutable offline snapshot. Schema revision is a caller
assertion bound into the request digest, not proof of whole-installation
compatibility. Actual document schema and content are verified by the existing
document migration converter. Paths are explicit; credentials are not discovered.

Success stdout is one `puddingknowledge-migrate-from-claw-receipt/v1` JSON object:
`request_digest` binds the exact request bytes, `source_snapshot_identity` binds
the approved absolute snapshot root independently supplied by the orchestrator.
Knowledge rejects Catalog/files outside that root. `artifacts` maps portable relative
paths to SHA-256 digests, and `covered_domains` / `pending_domains` state scope.
The receipt is also stored in `receipt.json`; converted data is in `candidate/`,
which can initialize the persistent local runtime with `--document-migration`.
Harness treats the request as opaque and validates the process receipt and bytes;
it never imports Knowledge source or interprets Catalog records.

Current coverage is document Catalog and blobs. Other Catalog domains, Wiki,
indexes and knowledge credentials remain pending. Every receipt states
`verified_inactive_partial`, `installation_prepared=false`,
`activation_allowed=false`, `writer_fence_verified=false`, and
`credential_rebind_required=true`. This is not the full installation Migration
Manifest, compatibility gate, source snapshot acquisition, or CUTOVER authority.

Retries require identical request bytes and re-run source/target verification.
A process lock serializes output. A crash after candidate publication resumes
before receipt publication; immutable plan/receipt publication never replaces an
existing competing file. Unknown files, changed source, altered artifacts and
changed receipts fail closed. Failure stdout contains a fixed error code and no
request paths or secrets. The explicit installed executable is a trusted local
provider; the receipt is not a cryptographic attestation of arbitrary executables.
