# Milvus vector index configuration

The local runtime keeps the version `1` index configuration contract. Existing
`knowledge_local_vector` files are unchanged. A Milvus backed index uses the
same embedding, Space and batching fields and adds a provider block:

```json
{
  "version": 1,
  "provider_id": "knowledge_milvus_vector",
  "space_ids": ["space_kb_default"],
  "embedding": {
    "endpoint": "http://127.0.0.1:8080/v1/embeddings",
    "model": "embedding-model",
    "dimension": 1536,
    "api_key_env": "KNOWLEDGE_EMBEDDING_API_KEY"
  },
  "batch_size": 32,
  "max_chars": 4000,
  "milvus": {
    "endpoint": "http://127.0.0.1:19530/",
    "api_key_env": "KNOWLEDGE_MILVUS_API_KEY"
  }
}
```

`milvus.endpoint` must be an explicit `http` or `https` URL with a host and
only the root path. Userinfo, query strings and fragments are rejected. The
credential reference must name an environment variable beginning with
`KNOWLEDGE_MILVUS_`; the value is read by the local host and is never stored
in Catalog metadata or Package manifests.

Catalogs created before the vector-index provider column existed continue to
read active indexes as `knowledge_local_vector`. With the column present,
only `knowledge_local_vector` and `knowledge_milvus_vector` are accepted;
unknown provider values fail closed. The active Collection binding therefore
selects the same provider identity that was published by the index rebuild.


For an explicitly unauthenticated, isolated local Milvus service, `api_key_env`
may be `null`. A named environment variable must exist; there is no ambient
credential discovery. Milvus REST requests do not inherit proxy settings or
follow redirects. Endpoint and dimension contribute to persisted storage
identity, so changing the configured server invalidates an old index binding.

Rebuild uses a fresh `pkv_` collection on each attempt. It writes normalized
vectors, verifies strong-consistency count and actual float32 vector content,
then publishes the local index metadata and remote location in one SQLite
transaction. Source access is checked again after remote work. A failed remote
operation or SQLite commit cannot activate the new collection. Old generations
and unreachable staging collections remain for later retention-aware GC.

Queries use Milvus search for each selected Collection generation, validate IDs
against that generation's checked local chunks, and build citations and scores
from local facts. They recheck current source access and active publication after
remote calls. Missing/tampered remote vectors refuse the query and same-key
replay; a new idempotency key can rebuild the derived index. Local index files and
old request fingerprints remain compatible after the provider-column migration.

This implementation deliberately verifies all vectors of each selected index
before search (maximum 10,000 chunks and 2,000,000 vector components). It is a
bounded correctness baseline, not evidence of production-scale latency. It does
not implement BM25/RRF, reranking, multimodal embeddings, or automatic remote GC.

From `backend`, run the opt-in isolated Docker and application acceptance:

```sh
KNOWLEDGE_TEST_REAL_MILVUS=1 .venv/bin/python -m pytest -q tests/test_knowledge_platform_milvus_runtime.py
```

The test owns a fresh Docker project, Home and ports, tests real storage integrity,
application REST/MCP retrieval, index loss/rebuild and service restart, and then
removes only its own containers. It does not connect to an existing Milvus
endpoint. The runtime uses the [Milvus 2.5 REST API](https://milvus.io/api-reference/restful/v2.5.x/v2/Vector%20(v2)/Query.md).

Pass the configuration using the existing runtime/CLI `--index-config` option.
An explicit rebuild uses the same six-field request as local indexing:

```sh
node packages/knowledge-platform-deploy-cli/src/cli.mjs index \
  --home /absolute/platform-home --space-id space_kb_default \
  --collection-id your_collection --collection-version your_version \
  --capability document_rag_query --provider-id knowledge_milvus_vector \
  --idempotency-key your_unique_rebuild_key --apply --json
```

`wiki_query` uses the same lifecycle with normalized Wiki text Assets. Cold Milvus
process health can precede loaded-collection recovery; failed reads during that
window remain errors and may be retried. No fallback declares the missing remote
index ready. The acceptance bounds this recovery wait at 90 seconds.
