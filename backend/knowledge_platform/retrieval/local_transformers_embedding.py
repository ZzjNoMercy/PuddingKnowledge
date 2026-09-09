"""Explicit local Transformers embedding provider.

The provider is intentionally request-scoped and path-explicit.  It loads
only a caller-selected local model with ``local_files_only=True`` and never
looks up ambient credentials, endpoints, Vault values, or remote code.
"""

from __future__ import annotations

import hashlib
from collections.abc import Sequence
from pathlib import Path

from .embedding import EmbeddingProviderError


def _has_symlink_component(path: Path) -> bool:
    current = path
    while current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return False


class LocalTransformersEmbeddingClient:
    """Embed bounded text batches with an explicitly selected local model."""

    provider_version = "local-transformers-v1"

    def __init__(
        self,
        *,
        model_dir: Path,
        dimension: int,
        batch_size: int = 8,
        max_length: int = 1024,
        task_prefix: str = "Passage: ",
    ) -> None:
        if not isinstance(model_dir, Path):
            raise ValueError("local embedding model directory must be a Path")
        model_dir = model_dir.expanduser().absolute()
        if _has_symlink_component(model_dir) or not model_dir.is_dir():
            raise ValueError("local embedding model directory must be a non-symlink directory")
        if type(dimension) is not int or not 1 <= dimension <= 16_384:
            raise ValueError("local embedding dimension is invalid")
        if type(batch_size) is not int or not 1 <= batch_size <= 64:
            raise ValueError("local embedding batch_size is invalid")
        if type(max_length) is not int or not 32 <= max_length <= 8192:
            raise ValueError("local embedding max_length is invalid")
        if not isinstance(task_prefix, str) or len(task_prefix) > 128 or any(ord(c) < 32 for c in task_prefix):
            raise ValueError("local embedding task_prefix is invalid")

        try:
            import torch
            from transformers import AutoModel, AutoTokenizer

            tokenizer = AutoTokenizer.from_pretrained(model_dir, local_files_only=True)
            model = AutoModel.from_pretrained(
                model_dir,
                local_files_only=True,
                low_cpu_mem_usage=True,
                dtype=torch.bfloat16,
            )
        except Exception as error:  # noqa: BLE001 - normalize provider failures
            raise EmbeddingProviderError("local Transformers model could not be loaded offline") from error

        hidden_size = getattr(getattr(model, "config", None), "hidden_size", None)
        if type(hidden_size) is not int or hidden_size != dimension:
            raise ValueError("local embedding model dimension does not match the requested dimension")
        model.eval()
        self._model_dir = model_dir
        self._dimension = dimension
        self._batch_size = batch_size
        self._max_length = max_length
        self._task_prefix = task_prefix
        self._torch = torch
        self._tokenizer = tokenizer
        self._model = model

    @property
    def checkpoint_signature(self) -> str:
        """Stable non-secret identity for a checkpoint-compatible provider configuration."""

        prefix_digest = hashlib.sha256(self._task_prefix.encode("utf-8")).hexdigest()
        return (
            f"{self.provider_version}|dimension={self._dimension}|max_length={self._max_length}"
            f"|task_prefix_sha256={prefix_digest}"
        )

    def embed(self, texts: Sequence[str]) -> tuple[tuple[float, ...], ...]:
        if not isinstance(texts, Sequence) or isinstance(texts, (str, bytes)) or not texts:
            raise EmbeddingProviderError("local embedding input batch is invalid")
        if len(texts) > self._batch_size or any(
            not isinstance(text, str) or not text.strip() or len(text) > 16_384 for text in texts
        ):
            raise EmbeddingProviderError("local embedding input batch is invalid")
        try:
            batch = self._tokenizer(
                [self._task_prefix + text for text in texts],
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self._max_length,
            )
            with self._torch.inference_mode():
                output = self._model(**batch)
            hidden = getattr(output, "last_hidden_state", None)
            if hidden is None or hidden.ndim != 3 or hidden.shape[0] != len(texts) or hidden.shape[2] != self._dimension:
                raise EmbeddingProviderError("local embedding model returned an invalid hidden state")
            mask = batch["attention_mask"].unsqueeze(-1).to(hidden.dtype)
            pooled = (hidden * mask).sum(dim=1) / mask.sum(dim=1).clamp_min(1)
            normalized = self._torch.nn.functional.normalize(pooled.float(), dim=-1)
            if not bool(self._torch.isfinite(normalized).all()):
                raise EmbeddingProviderError("local embedding model returned non-finite vectors")
            return tuple(tuple(float(value) for value in row.tolist()) for row in normalized)
        except EmbeddingProviderError:
            raise
        except Exception as error:  # noqa: BLE001 - normalize provider failures
            raise EmbeddingProviderError("local Transformers embedding failed") from error

    def close(self) -> None:
        """Release the in-process model when the owning shadow exits."""

        self._model = None


__all__ = ["LocalTransformersEmbeddingClient"]
