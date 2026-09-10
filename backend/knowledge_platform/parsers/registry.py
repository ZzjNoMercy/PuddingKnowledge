from __future__ import annotations
from dataclasses import dataclass
import inspect
import math
from typing import Any

from .contracts import ParsedDocument


class ParserRegistryError(ValueError): pass
class ParserUnavailable(ParserRegistryError): pass

class _MinerUAdapter:
    def __init__(self, client): self.client = client
    async def parse(self, filename, content): return await self.client.parse_pdf(content, filename)
    def health(self): return True


@dataclass(frozen=True)
class ParserSpec:
    parser_id: str
    parser: Any
    enabled: bool = True
    priority: int = 0
    extensions: tuple[str, ...] = ()
    max_bytes: int = 32 * 1024 * 1024


class ParserRegistry:
    def __init__(self, specs=()):
        self._specs = {}
        for spec in specs: self.register(spec)

    def register(self, spec: ParserSpec):
        if not spec.parser_id or spec.parser_id in self._specs: raise ParserRegistryError("duplicate parser id")
        if type(spec.max_bytes) is not int or spec.max_bytes <= 0: raise ParserRegistryError("parser max_bytes is invalid")
        self._specs[spec.parser_id] = spec

    def list_available(self):
        result = []
        for spec in sorted(self._specs.values(), key=lambda item: (-item.priority, item.parser_id)):
            if not spec.enabled: continue
            try:
                if callable(getattr(spec.parser, "health", None)) and not spec.parser.health(): continue
                result.append(spec)
            except Exception: continue
        return tuple(result)

    async def parse(self, filename: str, content: bytes, *, parser_id: str | None = None) -> ParsedDocument:
        candidates = [self._specs.get(parser_id)] if parser_id else [spec for spec in self.list_available() if self._matches(spec, filename)]
        if parser_id and (candidates[0] is None or not candidates[0].enabled): raise ParserUnavailable(f"parser {parser_id!r} is unavailable")
        candidates = [spec for spec in candidates if spec is not None and self._matches(spec, filename)]
        if not candidates: raise ParserRegistryError(f"no parser supports {filename!r}")
        last = None
        for spec in candidates:
            try:
                if len(content) > spec.max_bytes: raise ParserRegistryError(f"parser {spec.parser_id} input exceeds configured max_bytes")
                result = spec.parser.parse(filename, content)
                if inspect.isawaitable(result): result = await result
                return result
            except Exception as exc:
                last = exc
                if parser_id: raise ParserRegistryError(f"parser {parser_id!r} failed") from exc
        raise ParserRegistryError("all matching parsers failed") from last

    def parse_sync(self, filename: str, content: bytes, *, parser_id: str | None = None) -> ParsedDocument:
        import asyncio
        return asyncio.run(self.parse(filename, content, parser_id=parser_id))

    @staticmethod
    def _matches(spec, filename):
        suffix = "." + filename.rsplit(".", 1)[-1].lower() if "." in filename.rsplit("/", 1)[-1] else ""
        return not spec.extensions or suffix in {value if value.startswith(".") else "." + value for value in spec.extensions}


def build_registry(config) -> ParserRegistry:
    """Build only explicitly configured local parsers; no config files are read."""
    from .mineru import MinerUClient
    from .native import NativeParser
    if not isinstance(config, list): raise ParserRegistryError("parser config must be a list")
    specs = []
    for item in config:
        if not isinstance(item, dict): raise ParserRegistryError("parser config item is invalid")
        allowed = {"id", "enabled", "priority", "extensions", "max_bytes", "endpoint", "timeout", "limits"}
        if set(item) - allowed: raise ParserRegistryError("parser config contains unknown fields")
        parser_id = item.get("id"); enabled = item.get("enabled", True); priority = item.get("priority", 0)
        if not isinstance(parser_id, str) or not parser_id or type(enabled) is not bool or type(priority) is not int: raise ParserRegistryError("parser config identity is invalid")
        limits = item.get("limits") or {}
        if not isinstance(limits, dict) or set(limits) - {"max_pdf_bytes", "max_response_bytes", "max_uncompressed_bytes", "max_file_bytes", "max_files", "max_zip_files"} or any(type(value) is not int or value <= 0 for value in limits.values()): raise ParserRegistryError("parser limits are invalid")
        max_bytes = item.get("max_bytes", limits.get("max_bytes", 32 * 1024 * 1024))
        if type(max_bytes) is not int or max_bytes <= 0: raise ParserRegistryError("parser max_bytes is invalid")
        extensions = item.get("extensions", ("md", "markdown", "txt", "csv", "tsv", "docx") if parser_id == "native" else ("pdf",))
        if isinstance(extensions, str) or not isinstance(extensions, (list, tuple)) or any(not isinstance(value, str) or not value.strip() for value in extensions): raise ParserRegistryError("parser extensions are invalid")
        timeout = item.get("timeout", 30)
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or timeout <= 0: raise ParserRegistryError("parser timeout is invalid")
        if parser_id == "native": parser = NativeParser(max_bytes=max_bytes, max_zip_files=limits.get("max_zip_files", 256), max_uncompressed_bytes=limits.get("max_uncompressed_bytes", 64 * 1024 * 1024)); extensions = tuple(extensions)
        elif parser_id.startswith("mineru"):
            endpoint = item.get("endpoint")
            if not isinstance(endpoint, str): raise ParserRegistryError("MinerU endpoint is required")
            from .mineru import ParseLimits
            parser = _MinerUAdapter(MinerUClient(endpoint, timeout=float(timeout), limits=ParseLimits(**{key: value for key, value in limits.items() if key != "max_zip_files"}))); extensions = tuple(extensions)
        else: raise ParserRegistryError(f"unknown parser id {parser_id!r}")
        specs.append(ParserSpec(parser_id, parser, enabled, priority, extensions, max_bytes))
    return ParserRegistry(specs)
