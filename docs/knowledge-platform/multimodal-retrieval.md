# Independent image and text indexing

Knowledge owns the multimodal model client, authorized source reads and index
publication. Harness continues to use the existing query/MCP contracts.

To opt in, set the index configuration's `embedding` object to:

```json
{
  "protocol": "dashscope_multimodal",
  "endpoint": "https://your-provider/api/v1/services/embeddings/multimodal-embedding/multimodal-embedding",
  "model": "qwen3-vl-embedding",
  "dimension": 1024,
  "api_key_env": "KNOWLEDGE_EMBEDDING_API_KEY"
}
```

Supply the operator's complete endpoint. The example hostname is illustrative.
The credential comes only from the explicitly named environment variable.
Omitting `protocol`, or setting it to `openai`, preserves the previous text-only
configuration and index identity. Switching to the multimodal protocol requires
a rebuild with a new idempotency key. Both local and Milvus vector providers work.

Text chunks and images use the same configured model and vector dimension.
Images of kind `image`, `original_file` or `derived_media` enter the index when
their MIME is an image type. PNG, JPEG, GIF and WebP MIME/magic pairs are accepted;
unsupported formats refuse the rebuild. The client receives only bytes already
read through the authorized BlobReader, never arbitrary paths or remote URLs.
Images are sent as data URIs. Each image produces one vector. Text queries use
the same vector space. The DashScope payload follows its
[multimodal embedding API](https://www.alibabacloud.com/help/en/model-studio/multimodal-embedding-api-reference).
`qwen3-vl-embedding` uses `enable_fusion=false`. Fusion-only `qwen2.5-vl` models
are rejected because a fused vector does not satisfy independent item indexing.

Image titles and descriptions form bounded metadata context for optional BM25
and text reranking. They are not OCR or a transcription of image contents.
Image evidence has an empty quote and retains the checked Catalog Asset URI.
Source bytes, model identity, image metadata and active generation are verified;
source revocation during embedding or querying refuses publication/results.
Failed rebuilds leave the previous generation pointer intact. Revoked source
content cannot be served through that previous generation either.

Source input remains bounded to 32 MiB per rebuild, 8 MiB per image, 10,000
chunks and two million vector components. Image HTTP batches are at most ten
images and 16 MiB of original bytes; text batches are at most twenty items.
The client checks aggregate input limits before Base64 encoding. Model responses
are bounded to 8 MiB and must have exact item counts, contiguous unique indexes,
finite vectors and the configured dimension. No proxy environment or redirect
is inherited by the owned HTTP client. Magic checks identify encoding, not a
full image decoder or decompression validator.

## Reproducible acceptance

From `backend`:

```sh
uv sync --locked --all-extras
PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_knowledge_platform_multimodal_embedding.py tests/test_knowledge_platform_multimodal_index.py tests/test_knowledge_platform_multimodal_runtime.py
```

For a real isolated Milvus deployment, with cached Docker images available:

```sh
KNOWLEDGE_TEST_REAL_MILVUS=1 PYTHONPATH=. .venv/bin/python -m pytest -q tests/test_knowledge_platform_multimodal_runtime.py
```

Run Docker acceptance suites sequentially. They preserve preexisting containers
and remove only their own projects. The model server is a deterministic local
HTTP fixture. These checks prove Package image roundtrip, image ranking through
REST/MCP/direct routes, restart without image re-embedding, malformed output
rejection and source revocation during model calls. They do not measure model
quality and do not call a paid provider.

Separate text/image embedding spaces, image-query input, video, image-aware
reranking, OCR parity, per-stage trace, complete Console flows and production
release acceptance remain unfinished. Production activation remains disabled.
