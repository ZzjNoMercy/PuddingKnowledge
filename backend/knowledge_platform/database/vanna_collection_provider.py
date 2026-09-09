"""Read-only local Vanna Collection gateway for deterministic shadow queries.

The gateway consumes an inactive, file-backed Collection candidate produced
from a validated Knowledge Package.  It is intentionally lexical and
example-only: it can recall portable evidence and return an exact existing
SQL example, but it never calls an LLM, vector service, database, or legacy
Collection.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_CATEGORIES = ("ddl", "documentation", "sql_examples", "entities")
_MAX_ITEMS = 10_000
_MAX_LINE_BYTES = 256 * 1024
_MAX_FILE_BYTES = 16 * 1024 * 1024
_TOKEN_RE = re.compile(r"[A-Za-z0-9_$-]+|[\u3400-\u9fff]+")
_TABLE_REF_RE = re.compile(r"\b(?:from|join|update|into|delete\s+from)\s+([A-Za-z0-9_$.-]+)", re.IGNORECASE)


def _tokens(value: str) -> set[str]:
    tokens: set[str] = set()
    for token in _TOKEN_RE.findall(value):
        if not token.strip():
            continue
        folded = token.casefold()
        tokens.add(folded)
        if re.fullmatch(r"[\u3400-\u9fff]+", token):
            tokens.update(token[index : index + 2] for index in range(len(token) - 1))
    return tokens


def _normalized_question(value: str) -> str:
    return " ".join(value.casefold().split())


class LocalVannaCollectionGateway:
    """Expose a verified local Collection through the narrow Vanna gateway port."""

    def __init__(
        self,
        collection_root: Path,
        *,
        expected_collection_name: str,
        expected_package_revision: str,
        expected_input_digest: str,
    ) -> None:
        self._root = collection_root.expanduser().absolute()
        self._expected_collection_name = expected_collection_name
        self._expected_package_revision = expected_package_revision
        self._expected_input_digest = expected_input_digest
        self._verify_collection()

    def get_related_ddl(self, question: str) -> Sequence[object]:
        return self._ranked("ddl", question, fields=("content",))

    def get_related_documentation(self, question: str) -> Sequence[object]:
        return self._ranked("documentation", question, fields=("content",))

    def get_related_entities(self, question: str) -> Sequence[object]:
        return self._ranked("entities", question, fields=("canonical_name", "table_column", "aliases"))

    def generate_sql(self, question: str, **kwargs: object) -> str:
        if kwargs.get("allow_llm_to_see_data") is not False:
            raise ValueError("local Collection gateway never permits data access during SQL generation")
        table_names = kwargs.get("table_names")
        if not isinstance(table_names, Sequence) or isinstance(table_names, (str, bytes, bytearray)):
            raise ValueError("local Collection gateway requires an explicit table allowlist")
        allowed_tables = {str(item).casefold().split(".")[-1] for item in table_names}
        for item in self._read_category("sql_examples"):
            if _normalized_question(str(item.get("question") or "")) != _normalized_question(question):
                continue
            sql = str(item.get("sql") or "").strip()
            references = {match.casefold().split(".")[-1] for match in _TABLE_REF_RE.findall(sql)}
            if not references or not references.issubset(allowed_tables):
                raise ValueError("local Collection SQL example exceeds the table allowlist")
            return sql
        raise LookupError("local Collection has no exact SQL example for this question")

    def _verify_collection(self) -> dict[str, int]:
        root = self._root
        if root.is_symlink() or not root.is_dir():
            raise ValueError("local Vanna Collection must be a real directory")
        manifest_path = root / "collection-manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            raise ValueError("local Vanna Collection manifest is unavailable")
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("local Vanna Collection manifest is unreadable") from error
        if not isinstance(manifest, Mapping) or manifest.get("format") != "agent-knowledge-platform-vanna-collection-candidate/v1":
            raise ValueError("local Vanna Collection manifest format is invalid")
        if manifest.get("collection_name") != root.name:
            raise ValueError("local Vanna Collection identity does not match its directory")
        if self._expected_collection_name is not None and manifest.get("collection_name") != self._expected_collection_name:
            raise ValueError("local Vanna Collection identity is not the expected binding")
        package_revision = manifest.get("package_revision")
        if not isinstance(package_revision, str) or not _DIGEST_RE.fullmatch(package_revision):
            raise ValueError("local Vanna Collection package revision is invalid")
        if package_revision != self._expected_package_revision:
            raise ValueError("local Vanna Collection package revision is not the expected binding")
        input_digest = manifest.get("input_digest")
        if not isinstance(input_digest, str) or not _DIGEST_RE.fullmatch(input_digest):
            raise ValueError("local Vanna Collection input digest is invalid")
        if input_digest != self._expected_input_digest:
            raise ValueError("local Vanna Collection input digest is not the expected binding")
        if any(manifest.get(field) is not False for field in ("active", "activation_allowed", "provider_io_performed", "legacy_collection_read")):
            raise ValueError("local Vanna Collection must remain inactive and provider-free")
        digests = manifest.get("file_digests")
        counts = manifest.get("counts")
        if not isinstance(digests, Mapping) or set(digests) != {f"{category}.jsonl" for category in _CATEGORIES}:
            raise ValueError("local Vanna Collection file digest manifest is invalid")
        if not isinstance(counts, Mapping) or set(counts) != set(_CATEGORIES):
            raise ValueError("local Vanna Collection counts are invalid")
        expected_files = {"collection-manifest.json", *(f"{category}.jsonl" for category in _CATEGORIES)}
        actual_files = {path.name for path in root.iterdir()}
        if actual_files != expected_files or any(path.is_symlink() for path in root.iterdir()):
            raise ValueError("local Vanna Collection file set is not exact")
        for category in _CATEGORIES:
            digest = digests[f"{category}.jsonl"]
            if not isinstance(digest, str) or not _DIGEST_RE.fullmatch(digest):
                raise ValueError("local Vanna Collection file digest is invalid")
            path = root / f"{category}.jsonl"
            payload = path.read_bytes()
            if len(payload) > _MAX_FILE_BYTES or "sha256:" + hashlib.sha256(payload).hexdigest() != digest:
                raise ValueError("local Vanna Collection file digest does not match")
            if type(counts[category]) is not int:
                raise ValueError("local Vanna Collection count is invalid")
            try:
                count = int(counts[category])
            except (TypeError, ValueError) as error:
                raise ValueError("local Vanna Collection count is invalid") from error
            if count < 0:
                raise ValueError("local Vanna Collection count is invalid")
        return {category: int(counts[category]) for category in _CATEGORIES}

    def _read_category(self, category: str) -> list[dict[str, object]]:
        counts = self._verify_collection()
        path = self._root / f"{category}.jsonl"
        records: list[dict[str, object]] = []
        with path.open("rb") as stream:
            for line_number, raw_line in enumerate(stream, start=1):
                if len(raw_line) > _MAX_LINE_BYTES:
                    raise ValueError(f"local Vanna Collection {category} line is too large")
                if not raw_line.strip():
                    continue
                try:
                    item = json.loads(raw_line.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as error:
                    raise ValueError(f"local Vanna Collection {category} line is invalid") from error
                if not isinstance(item, dict):
                    raise ValueError(f"local Vanna Collection {category} item is invalid")
                records.append(item)
                if len(records) > _MAX_ITEMS:
                    raise ValueError(f"local Vanna Collection {category} is too large")
        if len(records) != counts[category]:
            raise ValueError(f"local Vanna Collection {category} count does not match")
        return records

    def _ranked(self, category: str, question: str, *, fields: Sequence[str]) -> list[dict[str, object]]:
        question_tokens = _tokens(question)
        records = self._read_category(category)
        scored: list[tuple[int, str, dict[str, object]]] = []
        for item in records:
            text_parts: list[str] = []
            for field in fields:
                value = item.get(field)
                if isinstance(value, list):
                    text_parts.extend(str(entry) for entry in value)
                elif isinstance(value, str):
                    text_parts.append(value)
            item_tokens = _tokens(" ".join(text_parts))
            score = len(question_tokens & item_tokens)
            scored.append((score, json.dumps(item, ensure_ascii=False, sort_keys=True), item))
        scored.sort(key=lambda item: (-item[0], item[1]))
        return [item for score, _, item in scored if score > 0][:100] or records[:100]
