"""Explicit DashScope multimodal embedding client for authorized bytes."""

from __future__ import annotations

import base64
import json
import math
from typing import Any
from urllib.parse import urlsplit

import httpx


_MAX_RESPONSE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BYTES = 8 * 1024 * 1024
_MAX_IMAGE_BATCH_BYTES = 16 * 1024 * 1024
_MAX_INPUT_ITEMS = 10_000
_MAX_TEXT_CHARS = 12_000
_MAX_TEXT_BYTES = 32 * 1024 * 1024
_IMAGE_MIMES = {"image/png", "image/jpeg", "image/gif", "image/webp"}
_MAGIC = {
    "image/png": lambda value: value.startswith(b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": lambda value: value.startswith(b"\xff\xd8\xff"),
    "image/gif": lambda value: value.startswith((b"GIF87a", b"GIF89a")),
    "image/webp": lambda value: value.startswith(b"RIFF") and value[8:12] == b"WEBP",
}


class MultimodalEmbeddingProviderError(RuntimeError):
    """The multimodal embedding provider returned an unusable result."""


def _error(message: str, cause: BaseException | None = None) -> MultimodalEmbeddingProviderError:
    error = MultimodalEmbeddingProviderError(message)
    if cause is not None:
        error.__cause__ = cause
    return error


class DashScopeMultimodalEmbeddingClient:
    def __init__(
        self,
        endpoint: str,
        model: str,
        dimension: int,
        api_key: str = "",
        batch_size: int = 10,
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
            or any(ord(char) < 32 or ord(char) == 127 for char in endpoint)
        ):
            raise ValueError("multimodal embedding endpoint is invalid")
        try:
            parsed.port
        except ValueError as error:
            raise ValueError("multimodal embedding endpoint port is invalid") from error
        if not isinstance(model, str) or not model or len(model) > 160 or any(ord(char) < 32 or ord(char) == 127 for char in model):
            raise ValueError("multimodal embedding model is invalid")
        if model.startswith("qwen2.5-vl"):
            raise ValueError("qwen2.5-vl is fusion-only and cannot provide independent vectors")
        if isinstance(dimension, bool) or not isinstance(dimension, int) or not 1 <= dimension <= 16_384:
            raise ValueError("multimodal embedding dimension is invalid")
        if not isinstance(api_key, str) or len(api_key) > 4096 or api_key != api_key.strip() or any(ord(char) < 32 or ord(char) == 127 for char in api_key):
            raise ValueError("multimodal embedding api_key is invalid")
        if isinstance(batch_size, bool) or not isinstance(batch_size, int) or not 1 <= batch_size <= 256:
            raise ValueError("multimodal embedding batch_size is invalid")
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not math.isfinite(float(timeout)) or not 0 < float(timeout) <= 120:
            raise ValueError("multimodal embedding timeout is invalid")
        self.endpoint = endpoint
        self.model = model
        self.dimension = dimension
        self._api_key = api_key
        self._batch_size = batch_size
        self._timeout = float(timeout)
        self._client = client

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not isinstance(texts, list) or len(texts) > _MAX_INPUT_ITEMS or any(
            not isinstance(text, str) or len(text) > _MAX_TEXT_CHARS for text in texts
        ):
            raise _error("multimodal embedding texts are invalid")
        if sum(len(text.encode("utf-8")) for text in texts) > _MAX_TEXT_BYTES:
            raise _error("multimodal embedding texts exceed size limit")
        return self._embed_items([{"text": text} for text in texts], max_batch=20)

    def embed_images(self, images: list[tuple[bytes, str]]) -> list[list[float]]:
        if not isinstance(images, list) or len(images) > _MAX_INPUT_ITEMS:
            raise _error("multimodal embedding images are invalid")
        raw_images: list[tuple[bytes, str]] = []
        total_bytes = 0
        for image in images:
            if not isinstance(image, tuple) or len(image) != 2:
                raise _error("multimodal embedding image is invalid")
            raw, mime = image
            if not isinstance(raw, bytes) or not isinstance(mime, str) or mime not in _IMAGE_MIMES:
                raise _error("multimodal embedding image is invalid")
            if not 1 <= len(raw) <= _MAX_IMAGE_BYTES or not _MAGIC[mime](raw):
                raise _error("multimodal embedding image bytes are invalid")
            total_bytes += len(raw)
            raw_images.append((raw, mime))
        if total_bytes > 32 * 1024 * 1024:
            raise _error("multimodal embedding image batch is too large")
        # Encode only after every raw input has passed the aggregate budget.
        items = [
            {"image": f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"}
            for raw, mime in raw_images
        ]
        sizes = [len(raw) for raw, _mime in raw_images]
        return self._embed_items(items, max_batch=10, item_sizes=sizes, max_batch_bytes=_MAX_IMAGE_BATCH_BYTES)

    def _embed_items(
        self,
        items: list[dict[str, str]],
        *,
        max_batch: int,
        item_sizes: list[int] | None = None,
        max_batch_bytes: int | None = None,
    ) -> list[list[float]]:
        results: list[list[float]] = []
        batch_limit = min(self._batch_size, max_batch)
        start = 0
        while start < len(items):
            end = min(start + batch_limit, len(items))
            if item_sizes is not None and max_batch_bytes is not None:
                size = 0
                while end > start and size + sum(item_sizes[start:end]) > max_batch_bytes:
                    end -= 1
                if end == start:
                    raise _error("multimodal embedding image batch is too large")
            results.extend(self._request(items[start:end]))
            start = end
        return results

    def _request(self, items: list[dict[str, str]]) -> list[list[float]]:
        parameters: dict[str, object] = {"dimension": self.dimension}
        if self.model == "qwen3-vl-embedding":
            parameters["enable_fusion"] = False
        body = {
            "model": self.model,
            "input": {"contents": items},
            "parameters": parameters,
        }
        headers = {"Content-Type": "application/json"}
        if self._api_key:
            headers["Authorization"] = "Bearer " + self._api_key
        try:
            response_bytes = bytearray()
            owned = self._client is None
            http_client = self._client or httpx.Client(timeout=self._timeout, trust_env=False, follow_redirects=False)
            try:
                with http_client.stream("POST", self.endpoint, headers=headers, json=body) as response:
                    response.raise_for_status()
                    for chunk in response.iter_bytes():
                        response_bytes.extend(chunk)
                        if len(response_bytes) > _MAX_RESPONSE_BYTES:
                            raise _error("multimodal embedding response exceeds size limit")
            finally:
                if owned:
                    http_client.close()
            try:
                payload = json.loads(bytes(response_bytes))
            except (TypeError, ValueError) as error:
                raise _error("multimodal embedding response is not valid JSON", error)
            return self._parse_response(payload, len(items))
        except MultimodalEmbeddingProviderError:
            raise
        except Exception as error:
            raise _error("multimodal embedding provider request failed", error)

    def _parse_response(self, payload: Any, expected: int) -> list[list[float]]:
        output = payload.get("output") if isinstance(payload, dict) else None
        embeddings = output.get("embeddings") if isinstance(output, dict) else None
        if not isinstance(embeddings, list) or len(embeddings) != expected:
            raise _error("multimodal embedding response count is invalid")
        ordered: list[list[float] | None] = [None] * expected
        for item in embeddings:
            if not isinstance(item, dict):
                raise _error("multimodal embedding response item is invalid")
            index = item.get("index")
            vector = item.get("embedding")
            if isinstance(index, bool) or not isinstance(index, int) or not 0 <= index < expected or ordered[index] is not None:
                raise _error("multimodal embedding response index is invalid")
            if not isinstance(vector, list) or len(vector) != self.dimension or any(
                isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value))
                for value in vector
            ):
                raise _error("multimodal embedding response vector is invalid")
            ordered[index] = [float(value) for value in vector]
        if any(vector is None for vector in ordered):
            raise _error("multimodal embedding response indexes are incomplete")
        return [vector for vector in ordered if vector is not None]
