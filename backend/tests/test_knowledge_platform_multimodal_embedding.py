import base64
import json

import httpx
import pytest

from knowledge_platform.retrieval.multimodal_embedding import (
    DashScopeMultimodalEmbeddingClient,
    MultimodalEmbeddingProviderError,
)


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)


def _png(size=16):
    return b"\x89PNG\r\n\x1a\n" + b"x" * (size - 8)


def test_text_batches_at_provider_limit_and_reorders_response():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        count = len(body["input"]["contents"])
        return httpx.Response(200, json={"output": {"embeddings": [
            {"index": index, "embedding": [float(index), 1.0]}
            for index in reversed(range(count))
        ]}})

    values = DashScopeMultimodalEmbeddingClient(
        "https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2,
        api_key="secret", batch_size=256, client=_client(handler)
    ).embed([f"text-{i}" for i in range(25)])
    assert len(calls) == 2
    assert [len(call["input"]["contents"]) for call in calls] == [20, 5]
    assert calls[0]["parameters"] == {"dimension": 2, "enable_fusion": False}
    assert values[0] == [0.0, 1.0] and values[-1] == [4.0, 1.0]


def test_images_are_sniffed_and_encoded_with_ten_item_batches():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        count = len(body["input"]["contents"])
        return httpx.Response(200, json={"output": {"embeddings": [
            {"index": i, "embedding": [0.1, 0.2]} for i in range(count)
        ]}})

    images = [(_png(), "image/png") for _ in range(11)]
    result = DashScopeMultimodalEmbeddingClient(
        "https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2,
        client=_client(handler), batch_size=256
    ).embed_images(images)
    assert len(result) == 11 and len(calls) == 2
    assert [len(call["input"]["contents"]) for call in calls] == [10, 1]
    encoded = calls[0]["input"]["contents"][0]["image"]
    assert encoded.startswith("data:image/png;base64,")
    assert base64.b64decode(encoded.split(",", 1)[1]) == _png()


@pytest.mark.parametrize("mime,raw", [
    ("image/png", b"not-png"), ("image/jpeg", _png()), ("image/bmp", _png()),
])
def test_images_require_allowed_matching_magic(mime, raw):
    client = DashScopeMultimodalEmbeddingClient("https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2)
    with pytest.raises(MultimodalEmbeddingProviderError):
        client.embed_images([(raw, mime)])


def test_image_byte_budget_splits_http_batches_and_total_limit():
    calls = []

    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        return httpx.Response(200, json={"output": {"embeddings": [
            {"index": i, "embedding": [0.1, 0.2]} for i in range(len(body["input"]["contents"]))
        ]}})

    raw = _png(8 * 1024 * 1024)
    result = DashScopeMultimodalEmbeddingClient(
        "https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2,
        client=_client(handler), batch_size=256
    ).embed_images([(raw, "image/png"), (raw, "image/png"), (b"\x89PNG\r\n\x1a\n", "image/png")])
    assert len(result) == 3 and [len(c["input"]["contents"]) for c in calls] == [2, 1]
    with pytest.raises(MultimodalEmbeddingProviderError):
        DashScopeMultimodalEmbeddingClient("https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2).embed_images(
            [(raw, "image/png")] * 5
        )


@pytest.mark.parametrize("results", [
    [{"index": 0, "embedding": [0.1]}],
    [{"index": True, "embedding": [0.1, 0.2]}, {"index": 1, "embedding": [0.1, 0.2]}],
    [{"index": 0, "embedding": [float("nan"), 0.2]}, {"index": 1, "embedding": [0.1, 0.2]}],
    [{"index": 0, "embedding": [0.1, 0.2]}, {"index": 0, "embedding": [0.1, 0.2]}],
])
def test_response_shape_is_strict(results):
    def handler(request):
        return httpx.Response(200, json={"output": {"embeddings": results}})

    client = DashScopeMultimodalEmbeddingClient(
        "https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2, client=_client(handler)
    )
    with pytest.raises(MultimodalEmbeddingProviderError):
        client.embed(["a", "b"])


def test_response_http_failure_and_oversize_are_normalized():
    def handler(request):
        return httpx.Response(200, content=b"{" + b"x" * (8 * 1024 * 1024) + b"}")

    client = DashScopeMultimodalEmbeddingClient(
        "https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2, client=_client(handler)
    )
    with pytest.raises(MultimodalEmbeddingProviderError):
        client.embed(["a"])


def test_text_boundaries_are_checked_before_payload_construction():
    client = DashScopeMultimodalEmbeddingClient("https://dashscope.example/api/v1/embedding", "qwen3-vl-embedding", 2)
    with pytest.raises(MultimodalEmbeddingProviderError):
        client.embed(["x" * 12_001])
    with pytest.raises(MultimodalEmbeddingProviderError):
        client.embed(["x" * 12_000] * 2_800)


def test_fusion_only_model_is_rejected():
    with pytest.raises(ValueError):
        DashScopeMultimodalEmbeddingClient("https://dashscope.example/api/v1/embedding", "qwen2.5-vl", 2)
