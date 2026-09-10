import json

import httpx
import pytest

from knowledge_platform.retrieval.rerank import DashScopeReranker, RerankProviderError


def _client(handler):
    return httpx.Client(transport=httpx.MockTransport(handler), trust_env=False, follow_redirects=False)


def test_text_payload_and_stable_sorted_result():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": [
            {"index": 1, "relevance_score": 0.4, "document": {"text": "untrusted"}},
            {"index": 0, "relevance_score": 0.9, "document": {"text": "untrusted"}},
        ]}})

    result = DashScopeReranker("https://dashscope.example/rerank", "text-rerank", "key", client=_client(handler)).rerank(
        "what", ["first", "second"], 2
    )
    assert result == [(0, 0.9), (1, 0.4)]
    assert seen == {
        "model": "text-rerank",
        "input": {"query": "what", "documents": ["first", "second"]},
        "parameters": {"top_n": 2, "return_documents": False},
    }


def test_qwen_vl_payload_wraps_all_text():
    seen = {}

    def handler(request):
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"output": {"results": [{"index": 0, "relevance_score": 1}]}})

    result = DashScopeReranker("https://dashscope.example/rerank", "qwen3-vl-rerank", client=_client(handler)).rerank(
        "what", ["first"], 1
    )
    assert result == [(0, 1.0)]
    assert seen["input"] == {"query": {"text": "what"}, "documents": [{"text": "first"}]}


@pytest.mark.parametrize("endpoint", [
    "dashscope.example/rerank", "ftp://dashscope.example/rerank", "https://u:p@dashscope.example/rerank",
    "https://dashscope.example/rerank?x=1", "https://dashscope.example/rerank#x", "https://dashscope.example/\nrerank",
])
def test_endpoint_must_be_explicit_and_safe(endpoint):
    with pytest.raises(ValueError):
        DashScopeReranker(endpoint, "text-rerank")


def test_validation_rejects_oversized_and_invalid_inputs():
    client = DashScopeReranker("https://dashscope.example/rerank", "text-rerank")
    with pytest.raises(RerankProviderError):
        client.rerank("x" * 513, ["doc"], 1)
    with pytest.raises(RerankProviderError):
        client.rerank("x", [], 1)
    with pytest.raises(RerankProviderError):
        client.rerank("x", ["doc"], True)
    with pytest.raises(RerankProviderError):
        client.rerank("x", ["x" * 12001], 1)


@pytest.mark.parametrize("results", [
    [{"index": 0, "relevance_score": 0.2}],
    [{"index": True, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.1}],
    [{"index": 0, "relevance_score": 0.2}, {"index": 0, "relevance_score": 0.1}],
    [{"index": 2, "relevance_score": 0.2}, {"index": 1, "relevance_score": 0.1}],
    [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 0.1}],
])
def test_malformed_provider_results_fail_closed(results):
    def handler(request):
        return httpx.Response(200, json={"output": {"results": results}})

    client = _client(handler)
    with pytest.raises(RerankProviderError):
        DashScopeReranker("https://dashscope.example/rerank", "text-rerank", client=client).rerank(
            "what", ["first", "second"], 2
        )


def test_http_error_redirect_and_response_size_are_errors():
    def redirect(request):
        return httpx.Response(307, headers={"Location": "https://other.example/rerank"})

    with pytest.raises(RerankProviderError):
        DashScopeReranker("https://dashscope.example/rerank", "text-rerank", client=_client(redirect)).rerank(
            "what", ["first"], 1
        )

    def too_large(request):
        return httpx.Response(200, content=b"{" + b"x" * (1024 * 1024) + b"}")

    with pytest.raises(RerankProviderError):
        DashScopeReranker("https://dashscope.example/rerank", "text-rerank", client=_client(too_large)).rerank(
            "what", ["first"], 1
        )
