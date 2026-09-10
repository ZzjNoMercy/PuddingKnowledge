# Independent hybrid retrieval and reranking

The Platform owns text retrieval, lexical scoring, rank fusion and rerank HTTP.
The Harness consumes the existing query/MCP contract. No `config.py`, provider
registry, legacy Tool or process-wide model configuration is imported.

The existing version 1 index configuration remains valid. Add this optional
`retrieval` object to either `knowledge_local_vector` or `knowledge_milvus_vector`:

```json
{
  "candidate_limit": 20,
  "vector_weight": 1.0,
  "bm25_weight": 1.0,
  "rrf_k": 60,
  "rerank": {
    "protocol": "dashscope",
    "endpoint": "https://your-workspace-endpoint/api/v1/services/rerank/text-rerank/text-rerank",
    "model": "gte-rerank-v2",
    "api_key_env": "KNOWLEDGE_RERANK_API_KEY"
  }
}
```

Use the operator's complete rerank URL, not the illustrative hostname above.
`rerank: null` enables hybrid retrieval without a rerank service. Omitting
`retrieval` retains vector-only behavior. A zero channel weight disables that
channel; at least one weight must be positive. A named credential environment
variable must exist. `api_key_env: null` explicitly opts into no authentication,
for example for an isolated local protocol fixture. There is no ambient lookup.

Ranking configuration is query-only: changing weights or the reranker does not
replace a stored index, its embedding identity or its idempotency record.
Restart the runtime with the updated `--index-config` file to apply it.

## Ranking and trust boundaries

1. Fix the authorized Space, Collection version and active index generation.
2. Verify the source assets and index chunks; when the vector channel uses
   Milvus, verify its remote vector content before retrieving its candidates.
3. Rank the vector candidates and score BM25 over the same checked local chunks.
   Tokenization case-folds Unicode words and includes Chinese characters and
   adjacent Chinese bigrams. BM25 uses `k1=1.5`, `b=0.75`.
4. Fuse the bounded channel lists using weighted reciprocal rank,
   `weight / (rrf_k + rank)`. RRF scores are normalized by the theoretical maximum
   `sum(weights)/(rrf_k+1)`; they are ranking values, not probabilities.
5. If configured, send the bounded fused text candidates to the reranker. Only
   strict unique indexes and finite scores in `0..1` are accepted. Provider-returned
   documents and resource identifiers are ignored. Citation text always comes
   from the checked local chunk.
6. Recheck current source availability and active publications after all model
   calls before returning evidence. A configured reranker failure returns an
   error; it does not silently report the unreranked list as successful reranking.

Direct document/Wiki routes consult active indexes before fallback retrieval.
Fallback cannot reintroduce Assets already owned by an active index, so a full
legacy result list cannot bypass the configured ranking or its validation.

Candidate limits are `1..50`; the effective pool is at least the requested query
limit (also at most 50). The hybrid corpus is bounded to 10,000 chunks and 32 MiB
of UTF-8 text. Rerank input is bounded to 50 documents, 12,000 characters per
chunk, and 1 MiB of document UTF-8; responses are bounded to 1 MiB. Exceeding a
bound refuses the operation. Model clients do not inherit HTTP proxy environment
or follow redirects.

The native DashScope payload follows its
[rerank API](https://www.alibabacloud.com/help/en/model-studio/text-rerank-api).
`qwen3-vl-rerank` uses text objects for query/documents; the text model path uses
strings. This implementation does not upload images or video and does not claim
support for a different provider's OpenAI-compatible rerank protocol.

## Acceptance and remaining work

From `backend`, the deterministic provider HTTP/application test is:

```sh
.venv/bin/python -m pytest -q tests/test_knowledge_platform_hybrid_runtime.py -k local
```

Include an isolated, real Docker Milvus deployment (cached infrastructure images
required) with:

```sh
KNOWLEDGE_TEST_REAL_MILVUS=1 .venv/bin/python -m pytest -q tests/test_knowledge_platform_hybrid_runtime.py
```

The tests prove lexical rank improvement, rerank ordering through REST/MCP/direct
routes, index replay after ranking-config changes, disabled-vector operation,
invalid provider result rejection, and source revocation during reranking. The
model services are deterministic local HTTP fixtures; no live paid DashScope
request or ranking-quality benchmark is claimed. Milvus is a real isolated
service, and only that test project's containers are removed.

BM25 currently runs over checked text in the Platform process. This does not
claim a Milvus sparse index, production latency, multimodal parity, or per-stage
trace spans. Remote old-generation/abandoned-index GC, full product acceptance,
and production upgrade/release validation remain separate unfinished work.
Production activation remains disabled.
