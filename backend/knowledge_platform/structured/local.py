"""Deterministic local CSV/TSV provider for Phase 4 development shadowing."""

from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import os
import re
import stat
from collections.abc import Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .ports import (
    SemanticContextBinding,
    StructuredQueryProviderError,
    StructuredSourceProfile,
    TableQueryPayload,
)

_MAX_BYTES = 16 * 1024 * 1024
_MAX_BINDING_BYTES = 512 * 1024 * 1024
# Local shadow queries may read the existing monthly Catalog sources (roughly
# 175k rows); response previews remain bounded by TableQueryPayload.
_MAX_ROWS = 250_000
_TOKEN_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)
_ID_RE = re.compile(r"^[A-Za-z0-9._-]{1,160}$")
_SECRET_COLUMN_RE = re.compile(r"(?i)(?:password|secret|token|authorization|api[_ -]?key|private[_ -]?key)")


class StaticSemanticContextRegistry:
    """Read-only registry adapter for compiled SemanticQueryContext values."""

    def __init__(self, contexts: Sequence[object]) -> None:
        self._contexts: dict[tuple[str, str], object] = {}
        for context in contexts:
            binding = SemanticContextBinding.from_object(context)
            if binding is None:
                raise ValueError("semantic context must not be empty")
            key = (binding.context_id, binding.content_hash)
            if key in self._contexts and self._contexts[key] is not context:
                raise ValueError("duplicate semantic context binding")
            self._contexts[key] = context

    def resolve(self, *, context_id: str, content_hash: str) -> object | None:
        return self._contexts.get((context_id, content_hash))


class LocalStructuredFileProvider:
    """Query only an explicit Asset→file/URI binding; no directory scanning."""

    def __init__(
        self,
        *,
        asset_paths: Mapping[str, Path],
        asset_uris: Mapping[str, str],
        asset_sheets: Mapping[str, str | int | None] | None = None,
        logical_asset_sources: Mapping[str, Sequence[tuple[str, Path]]] | None = None,
    ) -> None:
        self._asset_paths = {str(key): Path(value).expanduser().absolute() for key, value in asset_paths.items()}
        self._logical_asset_sources = {
            str(key): tuple((str(source_id), Path(path).expanduser().absolute()) for source_id, path in sources)
            for key, sources in (logical_asset_sources or {}).items()
        }
        if any(not key or not sources for key, sources in self._logical_asset_sources.items()):
            raise ValueError("logical_asset_sources must contain non-empty source lists")
        for sources in self._logical_asset_sources.values():
            source_ids = [source_id for source_id, _ in sources]
            if any(not _ID_RE.fullmatch(source_id) for source_id in source_ids) or len(source_ids) != len(set(source_ids)):
                raise ValueError("logical dataset source IDs must be unique portable identifiers")
        if set(self._asset_paths) & set(self._logical_asset_sources):
            raise ValueError("an Asset cannot have both direct and logical sources")
        self._asset_uris = {str(key): str(value) for key, value in asset_uris.items()}
        if set(self._asset_paths) | set(self._logical_asset_sources) != set(self._asset_uris):
            raise ValueError("asset paths, logical sources, and asset URIs must have identical keys")
        self._asset_sheets = {str(key): value for key, value in (asset_sheets or {}).items()}
        known_sheet_keys = set(self._asset_paths) | set(self._logical_asset_sources)
        source_sheet_keys = {source_id for sources in self._logical_asset_sources.values() for source_id, _ in sources}
        if not set(self._asset_sheets).issubset(known_sheet_keys | source_sheet_keys):
            raise ValueError("asset_sheets contains an unknown Asset or source")

    async def query(
        self,
        *,
        query: str,
        asset_id: str | None,
        space_id: str | None,
        limit: int,
        semantic_context: object | None,
    ) -> Sequence[TableQueryPayload]:
        context_id, context_hash = self._context_metadata(semantic_context)
        known_assets = set(self._asset_paths) | set(self._logical_asset_sources)
        candidates = [asset_id] if asset_id is not None else sorted(known_assets)
        results: list[TableQueryPayload] = []
        for candidate in candidates:
            if candidate not in known_assets:
                continue
            uri = self._asset_uris[candidate]
            if space_id is not None:
                uri_parts = uri.removeprefix("knowledge://").split("/")
                if len(uri_parts) != 5 or uri_parts[0] != "spaces" or uri_parts[1] != space_id:
                    continue
            try:
                if candidate in self._logical_asset_sources:
                    payload = await asyncio.to_thread(
                        self._query_logical_files,
                        candidate,
                        self._logical_asset_sources[candidate],
                        self._asset_sheets,
                        uri,
                        query,
                        context_id,
                        context_hash,
                    )
                else:
                    payload = await asyncio.to_thread(
                        self._query_file,
                        candidate,
                        self._asset_paths[candidate],
                        uri,
                        query,
                        context_id,
                        context_hash,
                        self._asset_sheets.get(candidate),
                    )
            except FileNotFoundError:
                if candidate in self._logical_asset_sources:
                    raise StructuredQueryProviderError("logical dataset source is unavailable") from None
                continue
            except OSError as error:
                raise StructuredQueryProviderError("local structured file is unreadable") from error
            if payload is not None:
                results.append(payload)
        results.sort(key=lambda item: (-(item.score or 0.0), item.asset_id))
        return tuple(results[:limit])

    def inspect_source(self, *, path: object, sheet_name: str | int | None = None) -> StructuredSourceProfile:
        """Read one explicit local source for Processing without exposing its path."""

        columns, rows, digest = self._read_table_data(Path(path).expanduser().absolute(), sheet_name)
        return StructuredSourceProfile(columns=columns, row_count=len(rows), content_digest=digest)

    @staticmethod
    @contextmanager
    def _safe_open(path: Path, *, max_bytes: int = _MAX_BYTES):
        if not path.is_absolute() or len(path.parts) < 2:
            raise FileNotFoundError(path)
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        current_fd = os.open(os.sep, os.O_RDONLY | directory)
        stream = None
        try:
            for component in path.parts[1:-1]:
                if component in {"", ".", ".."}:
                    raise OSError("structured file path contains an unsafe component")
                next_fd = os.open(component, os.O_RDONLY | directory | nofollow, dir_fd=current_fd)
                os.close(current_fd)
                current_fd = next_fd
            final_component = path.parts[-1]
            if final_component in {"", ".", ".."}:
                raise OSError("structured file path contains an unsafe component")
            descriptor = os.open(final_component, os.O_RDONLY | nofollow, dir_fd=current_fd)
            stream = os.fdopen(descriptor, "rb")
            file_stat = os.fstat(stream.fileno())
            if not stat.S_ISREG(file_stat.st_mode):
                raise OSError("structured source must be a regular file")
            if file_stat.st_size > max_bytes:
                raise OSError("structured file exceeds local provider limit")
            yield stream
        finally:
            if stream is not None:
                stream.close()
            os.close(current_fd)

    @classmethod
    def file_digest(cls, path: Path) -> str:
        """Hash an explicitly bound source without parsing or exposing it."""

        digest = hashlib.sha256()
        with cls._safe_open(path.expanduser().absolute(), max_bytes=_MAX_BINDING_BYTES) as stream:
            while chunk := stream.read(1024 * 1024):
                digest.update(chunk)
        return f"sha256:{digest.hexdigest()}"

    @staticmethod
    def _context_metadata(value: object | None) -> tuple[str | None, str | None]:
        if isinstance(value, Mapping):
            return str(value.get("context_id") or "") or None, str(
                value.get("content_hash") or value.get("semantic_context_hash") or ""
            ) or None
        if value is None:
            return None, None
        return (
            str(getattr(value, "context_id", "") or "") or None,
            str(getattr(value, "content_hash", "") or "") or None,
        )

    @classmethod
    def _query_file(
        cls,
        asset_id: str,
        path: Path,
        uri: str,
        query: str,
        context_id: str | None,
        context_hash: str | None,
        sheet_name: str | int | None,
    ) -> TableQueryPayload | None:
        columns, rows, digest = cls._read_table_data(path, sheet_name)
        return cls._payload_from_rows(
            asset_id=asset_id,
            uri=uri,
            query=query,
            context_id=context_id,
            context_hash=context_hash,
            columns=columns,
            rows=rows,
            digest=digest,
            answer_prefix="本地表格",
        )

    @classmethod
    def _query_logical_files(
        cls,
        asset_id: str,
        sources: Sequence[tuple[str, Path]],
        asset_sheets: Mapping[str, str | int | None],
        uri: str,
        query: str,
        context_id: str | None,
        context_hash: str | None,
    ) -> TableQueryPayload | None:
        canonical_columns: tuple[str, ...] | None = None
        rows: list[dict[str, object]] = []
        source_digests: list[tuple[str, str]] = []
        for source_id, path in sources:
            columns, source_rows, digest = cls._read_table_data(path, asset_sheets.get(source_id))
            if canonical_columns is None:
                canonical_columns = columns
            elif columns != canonical_columns:
                raise StructuredQueryProviderError("logical dataset source schemas do not match")
            rows.extend(source_rows)
            source_digests.append((source_id, digest))
            if len(rows) > _MAX_ROWS:
                raise StructuredQueryProviderError("logical dataset exceeds local row limit")
        if canonical_columns is None:
            return None
        return cls._payload_from_rows(
            asset_id=asset_id,
            uri=uri,
            query=query,
            context_id=context_id,
            context_hash=context_hash,
            columns=canonical_columns,
            rows=rows,
            digest=cls.logical_content_digest(source_digests),
            answer_prefix="本地逻辑数据集",
        )

    @staticmethod
    def logical_content_digest(source_digests: Sequence[tuple[str, str]]) -> str:
        digest = hashlib.sha256()
        for source_id, source_digest in source_digests:
            digest.update(source_id.encode("utf-8"))
            digest.update(b"\0")
            digest.update(source_digest.encode("ascii"))
            digest.update(b"\0")
        return f"sha256:{digest.hexdigest()}"

    @classmethod
    def _read_table_data(
        cls, path: Path, sheet_name: str | int | None
    ) -> tuple[tuple[str, ...], list[dict[str, object]], str]:
        suffix = path.suffix.casefold()
        if suffix not in {".csv", ".tsv", ".xlsx", ".xls"}:
            raise StructuredQueryProviderError("local Phase 4 provider supports CSV/TSV/XLSX/XLS only")
        with cls._safe_open(path) as stream:
            raw = stream.read()
        digest = f"sha256:{hashlib.sha256(raw).hexdigest()}"
        if suffix in {".xlsx", ".xls"}:
            frame = cls._read_excel(raw, sheet_name)
            columns = tuple(str(column or "").strip() for column in frame.columns)
            rows = [
                {column: cls._cell_value(row.get(column)) for column in columns}
                for row in frame.to_dict(orient="records")
            ]
        else:
            text = raw.decode("utf-8-sig")
            reader = csv.DictReader(text.splitlines(), delimiter="\t" if suffix == ".tsv" else ",")
            columns = tuple(str(item or "").strip() for item in (reader.fieldnames or ()))
            rows = [{column: row.get(column) for column in columns} for row in reader]
        if len(rows) > _MAX_ROWS:
            raise StructuredQueryProviderError("structured file exceeds local row limit")
        safe_columns = tuple(column for column in columns if column and not _SECRET_COLUMN_RE.search(column))
        if not safe_columns:
            return (), [], digest
        rows = [{column: row.get(column) for column in safe_columns} for row in rows]
        return safe_columns, rows, digest

    @classmethod
    def _payload_from_rows(
        cls,
        *,
        asset_id: str,
        uri: str,
        query: str,
        context_id: str | None,
        context_hash: str | None,
        columns: tuple[str, ...],
        rows: list[dict[str, object]],
        digest: str,
        answer_prefix: str,
    ) -> TableQueryPayload | None:
        if not columns:
            return None
        tokens = {token.casefold() for token in _TOKEN_RE.findall(query) if token.strip()}
        haystack = " ".join((*columns, *(str(value or "") for row in rows[:20] for value in row.values()))).casefold()
        matched = sum(1 for token in tokens if token in haystack)
        if tokens and matched == 0:
            return None
        score = min(1.0, matched / max(1, len(tokens)))
        return TableQueryPayload(
            asset_id=asset_id,
            resource_uri=uri,
            answer=f"{answer_prefix}包含 {len(rows)} 行、{len(columns)} 列；查询词命中 {matched}/{len(tokens)}。",
            columns=columns,
            preview_rows=tuple(rows[:10]),
            row_count=len(rows),
            score=float(score),
            content_digest=digest,
            semantic_context_id=context_id,
            semantic_context_hash=context_hash,
        )

    @staticmethod
    def _read_excel(raw: bytes, sheet_name: str | int | None):
        try:
            import pandas as pd

            return pd.read_excel(io.BytesIO(raw), sheet_name=sheet_name if sheet_name is not None else 0)
        except ImportError as error:
            raise StructuredQueryProviderError("pandas is required for local Excel queries") from error
        except (OSError, ValueError, TypeError) as error:
            raise StructuredQueryProviderError("local Excel file could not be parsed") from error

    @staticmethod
    def _cell_value(value: Any) -> object:
        if value is None:
            return None
        try:
            import pandas as pd

            if bool(pd.isna(value)):
                return None
        except (ImportError, TypeError, ValueError):
            pass
        if hasattr(value, "isoformat"):
            return value.isoformat()
        if hasattr(value, "item"):
            try:
                return value.item()
            except (TypeError, ValueError):
                pass
        return value if isinstance(value, (str, int, float, bool)) else str(value)
