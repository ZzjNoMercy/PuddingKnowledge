"""Explicit-adapter Vanna provider for the Platform Database Query Plane."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import Protocol

from knowledge_contracts import Evidence

from .ports import DatabaseDatasetBinding, DatabaseSqlCandidate

_SECRET_OR_PATH_RE = re.compile(
    r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)\s*[:=]|"
    r"(?:https?://|file:|(?:^|[\s(])/(?:[^\s]+)|(?:^|[\s(])~/)"
)


class VannaGateway(Protocol):
    """The small subset implemented by an injected Vanna runtime."""

    def get_related_ddl(self, question: str) -> Sequence[object]: ...

    def get_related_documentation(self, question: str) -> Sequence[object]: ...

    def get_related_entities(self, question: str) -> Sequence[object]: ...

    def generate_sql(self, question: str, **kwargs: object) -> str: ...


def _text_items(value: object, *, field: str) -> list[str]:
    if isinstance(value, str):
        return [value]
    if not isinstance(value, Sequence) or isinstance(value, (bytes, bytearray)):
        raise ValueError(f"Vanna {field} evidence has an invalid shape")
    values: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item
        elif isinstance(item, Mapping):
            candidates = ("content", "ddl", "documentation", "text", "canonical_name", "name")
            text = next((str(item[key]) for key in candidates if isinstance(item.get(key), str) and item[key].strip()), "")
        else:
            raise ValueError(f"Vanna {field} evidence contains an invalid item")
        if not text.strip() or len(text) > 64 * 1024 or _SECRET_OR_PATH_RE.search(text):
            raise ValueError(f"Vanna {field} evidence contains unsafe content")
        values.append(text)
    return values[:100]


def _evidence(
    *,
    binding: DatabaseDatasetBinding,
    kind: str,
    values: object,
) -> tuple[Evidence, ...]:
    result: list[Evidence] = []
    for text in _text_items(values, field=kind):
        digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
        evidence_id = f"db_{kind}_{digest[:32]}"
        result.append(
            Evidence(
                asset_id=evidence_id,
                resource_uri=(
                    f"knowledge://spaces/{binding.space_id}/datasets/{binding.dataset_id}/database-evidence/{kind}/{digest[:24]}"
                ),
                locator={"section": kind},
                quote=text[:1200],
                revision=binding.source_revision,
                matched_by=("vanna", kind),
            )
        )
    return tuple(result)


class GatewayVannaProvider:
    """Adapt an injected Vanna client without making it Platform state."""

    def __init__(self, gateway: VannaGateway, *, version: str = "vanna-injected") -> None:
        if not version.strip() or len(version) > 100:
            raise ValueError("Vanna provider version is invalid")
        self._gateway = gateway
        self._version = version

    def generate(
        self,
        *,
        question: str,
        binding: DatabaseDatasetBinding,
        semantic_asset_ids: Sequence[str],
    ) -> DatabaseSqlCandidate:
        if not question.strip():
            raise ValueError("question must not be empty")
        ddl = _evidence(binding=binding, kind="ddl", values=self._gateway.get_related_ddl(question))
        documentation = _evidence(
            binding=binding,
            kind="documentation",
            values=self._gateway.get_related_documentation(question),
        )
        entities = _evidence(
            binding=binding,
            kind="entities",
            values=self._gateway.get_related_entities(question),
        )
        sql = self._gateway.generate_sql(
            question,
            allow_llm_to_see_data=False,
            table_names=tuple(binding.allowed_tables),
            semantic_asset_ids=tuple(semantic_asset_ids),
        )
        if not isinstance(sql, str):
            raise ValueError("Vanna SQL result is not text")
        return DatabaseSqlCandidate(
            sql=sql,
            evidence=ddl + documentation + entities,
            provider_version=self._version,
        )

