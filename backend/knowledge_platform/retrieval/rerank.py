"""Request-scoped DashScope text reranking with fail-closed validation."""

from __future__ import annotations

import json
import math
from typing import Any
from urllib.parse import urlsplit

import httpx


_MAX_RESPONSE_BYTES = 1024 * 1024
_MAX_QUERY_CHARS = 512
_MAX_DOCUMENTS = 50
_MAX_DOCUMENT_CHARS = 12_000
_MAX_DOCUMENT_BYTES = 1024 * 1024


class RerankProviderError(RuntimeError):
    """The rerank provider rejected or returned an unverifiable response."""


def _fail(message: str, cause: BaseException | None = None) -> RerankProviderError:
    error = RerankProviderError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


class DashScopeReranker:
    """Call one explicit DashScope rerank endpoint.

    No process-wide configuration, proxy environment, or redirect following is
    used. The endpoint and credential are supplied by the owning runtime.
    """

    def __init__(
        self,
        endpoint: str,
        model: str,
        api_key: str = "",
        timeout: float = 30,
        client: Any | None = None,
    ) -> None:
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or not parsed.path
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or endpoint != endpoint.strip()
            or len(endpoint)>2048
            or " " in endpoint
            or any(ord(char) < 32 or ord(char) == 127 for char in endpoint)
        ):
            raise ValueError("rerank endpoint is invalid")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("rerank endpoint port is invalid") from error
        if not isinstance(model, str) or not model.strip() or model!=model.strip() or len(model) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in model):
            raise ValueError("rerank model is invalid")
        if not isinstance(api_key, str) or len(api_key) > 4096 or api_key != api_key.strip() or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise ValueError("rerank api_key is invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120:
            raise ValueError("rerank timeout is invalid")
        self.endpoint = endpoint
        self.model = model
        self._api_key = api_key
        self._timeout = float(timeout)
        self._client = client

    def rerank(self, query: str, documents: list[str], top_n: int) -> list[tuple[int, float]]:
        self._validate_input(query, documents, top_n)
        if self.model == "qwen3-vl-rerank":
            query_payload: str | dict[str, str] = {"text": query}
            document_payload: list[str | dict[str, str]] = [{"text": document} for document in documents]
        else:
            query_payload = query
            document_payload = documents
        body = {
            "model": self.model,
            "input": {"query": query_payload, "documents": document_payload},
            "parameters": {"top_n": top_n, "return_documents": False},
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            response_bytes = bytearray()
            owned_client = self._client is None
            http_client = self._client or httpx.Client(
                timeout=self._timeout, trust_env=False, follow_redirects=False
            )
            try:
                with http_client.stream("POST", self.endpoint, headers=headers, json=body) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        response_bytes.extend(chunk)
                        if len(response_bytes) > _MAX_RESPONSE_BYTES:
                            raise _fail("rerank response exceeds size limit")
            finally:
                if owned_client:
                    http_client.close()
            try:
                payload = json.loads(bytes(response_bytes))
            except (TypeError, ValueError) as error:
                raise _fail("rerank response is not valid JSON", error)
            return self._parse_results(payload, top_n, len(documents))
        except RerankProviderError:
            raise
        except Exception as error:
            raise _fail("rerank provider request failed", error)

    @staticmethod
    def _validate_input(query: str, documents: list[str], top_n: int) -> None:
        if not isinstance(query, str) or not query.strip() or len(query) > _MAX_QUERY_CHARS:
            raise _fail("rerank query is invalid")
        if not isinstance(documents, list) or not 1 <= len(documents) <= _MAX_DOCUMENTS:
            raise _fail("rerank documents are invalid")
        total_bytes = 0
        for document in documents:
            if not isinstance(document, str) or len(document) > _MAX_DOCUMENT_CHARS:
                raise _fail("rerank document is invalid")
            total_bytes += len(document.encode("utf-8"))
        if total_bytes > _MAX_DOCUMENT_BYTES:
            raise _fail("rerank documents exceed size limit")
        if isinstance(top_n, bool) or not isinstance(top_n, int) or not 1 <= top_n <= len(documents):
            raise _fail("rerank top_n is invalid")

    @staticmethod
    def _parse_results(payload: Any, top_n: int, document_count: int) -> list[tuple[int, float]]:
        output = payload.get("output") if isinstance(payload, dict) else None
        results = output.get("results") if isinstance(output, dict) else None
        if not isinstance(results, list) or len(results) != top_n:
            raise _fail("rerank response result count is invalid")
        parsed: list[tuple[int, float]] = []
        seen: set[int] = set()
        for item in results:
            if not isinstance(item, dict):
                raise _fail("rerank response result is invalid")
            index = item.get("index")
            score = item.get("relevance_score")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < document_count or index in seen:
                raise _fail("rerank response index is invalid")
            if isinstance(score, bool) or not isinstance(score, (int, float)) or not math.isfinite(float(score)) or not 0 <= float(score) <= 1:
                raise _fail("rerank response score is invalid")
            seen.add(index)
            parsed.append((index, float(score)))
        return sorted(parsed, key=lambda item: (-item[1], item[0]))
