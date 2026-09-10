# Local vector index lifecycle

The independent runtime can rebuild a persistent vector index from a selected
Collection's normalized text Assets. Raw attachments remain Package members;
they require a normalized document before indexing. The runtime reads bytes
through the current authorized BlobReader and verifies their Catalog digests.

Start the installed service with `--state-dir` and an explicit `--index-config`:

```json
{
  "version": 1,
  "provider_id": "knowledge_local_vector",
  "space_ids": ["space_kb_default"],
  "embedding": {
    "endpoint": "http://127.0.0.1:9001/embeddings",
    "model": "your-explicit-model",
    "dimension": 768,
    "api_key_env": "KNOWLEDGE_EMBEDDING_API_KEY"
  },
  "batch_size": 32,
  "max_chars": 1200
}
```

The named environment variable must exist; `null` explicitly selects a provider
without an API key. No ambient model or credential is discovered. The embedding
endpoint receives source chunks and query text. Configuration alone performs no
embedding requests.

Run against the Home's verified owned service instance:

```sh
knowledge-platform index --home /absolute/knowledge-home \
  --space-id space_kb_default --collection-id docs --collection-version 1 \
  --capability document_rag_query --provider-id knowledge_local_vector \
  --idempotency-key index-docs-1 --apply --json
```

The same six fields are accepted by `POST /v1/indexes:rebuild`. Admin and an
explicit configured Space scope are required. `wiki_query` is also supported.
Omitting `--apply` returns a plan. Request data cannot specify paths, credentials,
embedding endpoint or model.

Publication stores vectors, source/model fingerprints and the active generation
in one SQLite transaction. A failed or cancelled rebuild preserves the old
published generation; a killed runtime can restart and retry the request.
Replaying a completed request verifies its index and sources without embedding
again. A changed source/model or an inactive generation requires a new request
key. Active derived bindings are projected into query routing without taking
over the file or Package publisher's ingestion bindings.

REST/MCP `knowledge_query` routes the exact Collection to this index. Search uses
normalized cosine similarity and revalidates current source readability and
digests, model identity, chunk count and chunk digest before returning evidence.
Revoking source publication therefore revokes indexed evidence even when Catalog
Asset metadata remains. File replacement may create a new Collection version;
rebuild that exact version. Orphaned old versions do not enter current queries.

The implementation bounds source bytes (32 MiB total, 8 MiB per read), chunk count
(10,000), vector components (2,000,000), chunk length and embedding batch size.
`active=true` means available to this local query runtime; production activation
remains `activation_allowed=false`. This is a local SQLite vector store, not a
Milvus deployment. Reranking, index GC, remote distributed workers and production
soak/activation remain separate work.

Real acceptance tests use an owned HTTP embedding fixture and independent server
processes, including a rebuild interrupted with SIGKILL, restart/retry, Package
replay, source update and publication revocation:

```sh
.venv/bin/python -m pytest -q tests/test_knowledge_platform_local_vector_index.py tests/test_knowledge_platform_local_vector_runtime.py
```
