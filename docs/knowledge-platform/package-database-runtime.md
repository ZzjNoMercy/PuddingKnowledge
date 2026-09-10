# Published database evidence and host binding

A database Package carries portable DDL, documentation, SQL examples and entities.
Collections reference these records through `database_source_ids`; each reference
must resolve within the same Space. Database source IDs and dataset IDs are
separate from Collection IDs and raw file Asset IDs.

Import publishes source facts and Collection relationships transactionally. It
does not copy a running Vanna/Milvus collection, read connector credentials, or
create a live database binding. Re-export preserves only selected evidence.

After import, a local operator can restart the installed service with
`--database-config /absolute/path/database.json` and the existing
`--package-config` and `--state-dir`. The version 2 database configuration pins
an exact published Collection version and database source:

```json
{
  "format": "knowledge-local-database/v2",
  "collection_id": "portable_database",
  "collection_version": "1",
  "source": {
    "dataset_id": "database_sales",
    "host": "127.0.0.1",
    "port": 5432,
    "database": "postgres",
    "username": "knowledge_reader",
    "allowed_tables": ["sales"],
    "password_env": "KNOWLEDGE_DB_PASSWORD"
  },
  "vanna": {
    "package_source_id": "sales_evidence",
    "package_revision": "sha256:<validated-package-revision>",
    "input_digest": "sha256:<canonical-single-source-list-digest>"
  }
}
```

`input_digest` is SHA-256 of UTF-8 JSON `[source]`, with sorted object keys,
compact separators and non-ASCII characters preserved; the source is the
normalized record from `database/index.json`. Credentials remain in the named
host environment variable. This local runtime currently uses `space_kb_default`
and permits loopback PostgreSQL only.

Startup rebuilds a disposable local evidence index from the persisted source.
The gateway recalls evidence lexically and returns exact stored SQL examples;
it is not general model-backed text-to-SQL or an activated vector index.
Schema, generation and execution revalidate the Package source and immutable
Collection facts. Removing or corrupting evidence also rejects previously
issued query plans. The PostgreSQL adapter separately enforces its table
allowlist, source revision, read-only SQL guard and bounded execution.

Run the real temporary PostgreSQL acceptance test from the independent backend:

```sh
PATH=/opt/homebrew/opt/postgresql@16/bin:$PATH .venv/bin/python -m pytest -q tests/test_knowledge_platform_package_database_runtime.py
```

The test creates its own database and reader role, imports evidence, binds the
host, executes REST/MCP queries, restarts, replays import, re-exports and verifies
revocation. It does not use a production database. Missing PostgreSQL binaries
cause a skip, which is not acceptance evidence.
