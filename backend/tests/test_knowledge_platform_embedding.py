from __future__ import annotations

import httpx
import pytest

from knowledge_platform.retrieval.embedding import EmbeddingProviderError, OpenAICompatibleEmbeddingClient


def test_openai_compatible_embedding_client_validates_response_and_preserves_index_order() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "data": [
                    {"index": 1, "embedding": [0.3, 0.4]},
                    {"index": 0, "embedding": [0.1, 0.2]},
                ]
            },
        )

    http_client = httpx.Client(transport=httpx.MockTransport(handler), trust_env=False)
    client = OpenAICompatibleEmbeddingClient(
        endpoint="https://embed.example/v1/embeddings",
        model="text-embedding-v4",
        dimension=2,
        api_key="test-key",
        batch_size=2,
        client=http_client,
    )
    assert client.embed(["first", "second"]) == ((0.1, 0.2), (0.3, 0.4))
    assert requests[0].headers["authorization"] == "Bearer test-key"
    assert "test-key" not in repr(client)
    http_client.close()


@pytest.mark.parametrize(
    "endpoint",
    [
        "https://user:pass@embed.example/v1/embeddings",
        "https://embed.example/v1/embeddings?key=secret",
        " https://embed.example/v1/embeddings",
        "https://embed.example/v1/embeddings\n",
    ],
)
def test_embedding_client_rejects_unsafe_endpoint(endpoint: str) -> None:
    with pytest.raises(ValueError, match="endpoint"):
        OpenAICompatibleEmbeddingClient(endpoint=endpoint, model="m", dimension=2)


def test_embedding_client_fails_closed_on_bad_shape_dimension_or_nan() -> None:
    def bad_response(vector):
        return httpx.MockTransport(lambda _: httpx.Response(200, json={"data": [{"embedding": vector}]}))

    for vector in ([0.1], [float("nan"), 0.2], ["0.1", 0.2]):
        http_client = httpx.Client(transport=bad_response(vector), trust_env=False)
        client = OpenAICompatibleEmbeddingClient(
            endpoint="https://embed.example/v1/embeddings", model="m", dimension=2, client=http_client
        )
        with pytest.raises(EmbeddingProviderError):
            client.embed(["text"])
        http_client.close()

    with pytest.raises(EmbeddingProviderError):
        OpenAICompatibleEmbeddingClient(
            endpoint="https://embed.example/v1/embeddings", model="m", dimension=2, batch_size=1
        ).embed([])
