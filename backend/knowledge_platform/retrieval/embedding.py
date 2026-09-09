"""Explicit OpenAI-compatible text embedding provider for Platform rebuilds."""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import Any
from urllib.parse import urlsplit

import httpx


class EmbeddingProviderError(RuntimeError):
    """The configured embedding provider cannot produce a validated batch."""


class OpenAICompatibleEmbeddingClient:
    """Request-scoped embedding client with no ambient config or credential lookup."""

    def __init__(
        self,
        *,
        endpoint: str,
        model: str,
        dimension: int,
        api_key: str = "",
        batch_size: int = 32,
        timeout: float = 120.0,
        client: Any | None = None,
    ) -> None:
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (
            parsed is None
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or endpoint != endpoint.strip()
            or any(ord(character) < 32 for character in endpoint)
        ):
            raise ValueError("embedding endpoint is invalid")
        if not isinstance(model, str) or not model.strip() or len(model) > 160 or any(ord(c) < 32 for c in model):
            raise ValueError("embedding model is invalid")
        if type(dimension) is not int or not 1 <= dimension <= 16_384:
            raise ValueError("embedding dimension is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 256:
            raise ValueError("embedding batch_size is invalid")
        if not isinstance(api_key, str) or api_key != api_key.strip() or any(ord(c) < 32 for c in api_key) or len(api_key) > 4096:
            raise ValueError("embedding api_key is invalid")
        if not isinstance(timeout, (int, float)) or not math.isfinite(float(timeout)) or float(timeout) <= 0:
            raise ValueError("embedding timeout is invalid")
        self._endpoint = endpoint
        self._model = model
        self._dimension = dimension
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = float(timeout)
        self._client = client

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)) or not texts:
            raise EmbeddingProviderError("embedding input batch is invalid")
        if len(texts) > self._batch_size or any(not isinstance(text, str) or not text.strip() or len(text) > 16_384 for text in texts):
            raise EmbeddingProviderError("embedding input batch is invalid")
        payload: dict[str, object] = {"input": list(texts), "model": self._model}
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = f"Bearer {self._api_key}"
        owned_client = self._client is None
        http_client = self._client or httpx.Client(timeout=self._timeout, trust_env=False)
        try:
            response = http_client.post(self._endpoint, headers=headers, json=payload)
            response.raise_for_status()
            body = response.json()
        except Exception as error:
            raise EmbeddingProviderError("embedding provider request failed") from error
        finally:
            if owned_client:
                http_client.close()
        if not isinstance(body, dict) or not isinstance(body.get("data"), list) or len(body["data"]) != len(texts):
            raise EmbeddingProviderError("embedding provider returned an invalid count")
        items = body["data"]
        indexed = [item.get("index") for item in items if isinstance(item, dict)]
        if len(indexed) != len(items) or any(index is not None and type(index) is not int for index in indexed):
            raise EmbeddingProviderError("embedding provider returned invalid indexes")
        if any(index is None for index in indexed) and any(index is not None for index in indexed):
            raise EmbeddingProviderError("embedding provider returned mixed indexes")
        if all(index is not None for index in indexed) and sorted(indexed) != list(range(len(texts))):
            raise EmbeddingProviderError("embedding provider returned non-contiguous indexes")
        ordered = (
            sorted(zip(indexed, items), key=lambda value: value[0])
            if indexed and indexed[0] is not None
            else list(zip(indexed, items))
        )
        result: list[tuple[float, ...]] = []
        for _index, item in ordered:
            vector = item.get("embedding") if isinstance(item, dict) else None
            if not isinstance(vector, Sequence) or isinstance(vector, (str, bytes)) or len(vector) != self._dimension:
                raise EmbeddingProviderError("embedding provider returned an invalid dimension")
            if any(type(value) not in (int, float) or not math.isfinite(float(value)) for value in vector):
                raise EmbeddingProviderError("embedding provider returned a non-finite vector")
            result.append(tuple(float(value) for value in vector))
        return tuple(result)

    def close(self) -> None:
        """Close an injected client when its owner chooses to do so."""

        close = getattr(self._client, "close", None)
        if callable(close):
            close()


__all__ = ["EmbeddingProviderError", "OpenAICompatibleEmbeddingClient"]
