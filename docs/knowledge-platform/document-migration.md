# Offline legacy document migration

```sh
python -m knowledge_platform.distribution.document_migration \
  --source-catalog /absolute/offline-catalog.sqlite3 \
  --source-files-root /absolute/offline-files \
  --bindings /absolute/private-document-bindings.json \
  --output /absolute/new-candidate \
  --installation-id install-1 --source-revision legacy-1
```

The private bindings JSON maps each legacy document ID to one relative file path
inside the explicit offline files root. Every document must have exactly one
binding. Paths from the legacy Catalog are never opened or inferred. The binding
must match the document's recorded SHA-256; absent or mismatched digests reject.
Symbolic/hard-linked blob files and escaping paths reject. Budgets are 32 MiB per
body, 256 MiB total, 5000 bindings and 64 MiB source Catalog. Source SQLite WAL,
SHM and journal sidecars reject: use a verified offline snapshot first.

The actual core ownership converter creates Platform Space, Asset and Collection
rows. Content is copied to content-addressed `blobs/` files. `manifest.json` binds
the source Catalog digest, revision, explicit bindings digest, generated Catalog,
blobs and Asset-to-blob mappings. The source Catalog and bodies are rechecked
before publication. Output remains private and inactive; no writer is switched,
no credential is copied, and no source is deleted. The intermediate whole source
Catalog copy is private and removed; it is not included in the output.

Repeat with the same inputs and complete output to verify idempotency. Replays
verify body digests against source facts and rerun the immutable core converter
against a read-only target connection; rewriting the output manifest digest
cannot legitimize altered target rows. An incomplete publication without a valid
manifest rejects. This version does not resume a process killed during the final
file publication: retain the incomplete candidate for diagnosis and choose a new
output. Existing directories are never overwritten or automatically removed.

The output supplies bindings for `LocalFilesystemBlobReader` and
`LocalDocumentRetrievalProvider`. Tests disconnect the original files and Catalog
and verify target-only lexical retrieval. This does not automatically register
the candidate in Console or a persistent workspace, nor provide full parser,
embedding, vector-index or multimodal migration. Binary files are byte-preserved;
text retrieval behavior remains that of the selected runtime provider.

This is the document ownership slice only, not `migrate-from-claw` for an entire
installation. Other Catalog slices, Wiki, connector jobs, credentials, index
rebuilding, installation snapshots, persistent writer fences, rollback and target
activation remain separate work. `complete_installation_migration=false` and
`activation_allowed=false` are explicit in the report and manifest.

The Catalog and blobs may contain private user data. The private manifest contains
Asset IDs, relative content-addressed paths and digests, but no host paths or raw
content; CLI output contains only bounded status and counts.
