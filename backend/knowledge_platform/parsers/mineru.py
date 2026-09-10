"""Bounded, in-memory MinerU PDF parser client.

The client talks only to an explicitly supplied loopback HTTP endpoint. It
never reads application configuration, writes files, follows redirects, or
deletes provider-side files.
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass
import io
import json
import math
import posixpath
import re
import urllib.parse
import zipfile
from collections.abc import Mapping

import httpx
from bs4 import BeautifulSoup
from markdown_it import MarkdownIt

from .contracts import ParsedDocument, ParsedMedia


class MinerUError(ValueError):
    pass


@dataclass(frozen=True)
class ParseLimits:
    max_pdf_bytes: int = 64 * 1024 * 1024
    max_response_bytes: int = 64 * 1024 * 1024
    max_uncompressed_bytes: int = 128 * 1024 * 1024
    max_file_bytes: int = 32 * 1024 * 1024
    max_files: int = 512

    def __post_init__(self):
        for name in ("max_pdf_bytes", "max_response_bytes", "max_uncompressed_bytes", "max_file_bytes", "max_files"):
            value = getattr(self, name)
            if type(value) is not int or value <= 0 or not math.isfinite(value):
                raise ValueError(f"{name} must be a positive integer")


_IMAGE_TYPES = {".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".gif": "image/gif", ".webp": "image/webp", ".svg": "image/svg+xml"}
_MARKDOWN_SUFFIXES = {".md", ".markdown"}
_IMAGE_RE = re.compile(r"!\[[^\]]*\]\(([^)\s]+)\)|<img\b[^>]*?\bsrc=[\"']([^\"']+)", re.I)
PARSER_VERSION = "local-http-v1"


def _path(name: str) -> str:
    if not isinstance(name, str) or not name or "\x00" in name or "\\" in name or name.startswith("/") or name.startswith("\\"):
        raise MinerUError("MinerU archive contains an unsafe path")
    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts) or ":" in parts[0]:
        raise MinerUError("MinerU archive contains an unsafe path")
    return "/".join(parts)


def _raw_references(markdown: bytes, *, allow_knowledge_uri: bool = False) -> list[str]:
    text = markdown.decode("utf-8")
    refs = []
    def visit(token):
        if token.type == "image":
            ref = dict(token.attrs or {}).get("src")
            if isinstance(ref, str): refs.append(ref)
        for child in token.children or (): visit(child)
    for token in MarkdownIt("commonmark", {"html": False}).parse(text): visit(token)
    soup = BeautifulSoup(text, "html.parser")
    refs.extend(str(tag.get("src")) for tag in soup.find_all("img") if tag.get("src") is not None)
    definition_keys = {key.lower() for key in re.findall(r"(?m)^\s*\[([^\]]+)\]:", text)}
    for key in re.findall(r"!\[[^\]]*\]\[([^\]]+)\]", text):
        if key.lower() not in definition_keys: raise MinerUError("Markdown image reference definition is missing")
    for ref in refs:
        if urllib.parse.urlparse(ref).scheme or ref.startswith("//"):
            if allow_knowledge_uri and ref.startswith("knowledge://"): continue
            raise MinerUError("MinerU markdown contains an external media URL")
        if ref.startswith("#"):
            continue
    return refs


def _references(markdown: bytes, base_dir: str = "", *, allow_knowledge_uri: bool = False) -> set[str]:
    refs = set()
    for ref in _raw_references(markdown, allow_knowledge_uri=allow_knowledge_uri):
        candidate = urllib.parse.unquote(ref.split("#", 1)[0].split("?", 1)[0])
        normalized = posixpath.normpath(posixpath.join(base_dir, candidate))
        if normalized == ".." or normalized.startswith("../"):
            raise MinerUError("MinerU markdown references an unsafe media path")
        refs.add(normalized)
    return refs


def rewrite_media(markdown: bytes | str, replacements: Mapping[str, str]) -> bytes:
    """Rewrite explicit local image destinations while preserving formatting.

    ``replacements`` maps the source destination as written in Markdown to the
    returned asset relative path. Unsupported/external destinations are
    rejected rather than left as dangling references.
    """
    text = markdown.decode("utf-8") if isinstance(markdown, bytes) else markdown
    if not isinstance(text, str): raise MinerUError("Markdown is invalid")
    def replace_destination(raw: str) -> str:
        if raw.startswith("<") and raw.endswith(">"):
            value = raw[1:-1]; wrapped = True
        else: value = raw; wrapped = False
        if urllib.parse.urlparse(value).scheme or value.startswith("//"):
            if value.startswith("knowledge://") and value in replacements: return raw
            raise MinerUError("Markdown contains an external media URL")
        target = replacements.get(value) or replacements.get(urllib.parse.unquote(value))
        if target is None: raise MinerUError("Markdown contains an unsupported or unrewritten media reference")
        return f"<{target}>" if wrapped else target
    inline = re.compile(r"(!\[[^\]]*\]\()(?P<dest><[^>]+>|[^)\s]+)(?P<tail>[^)]*\))")
    text = inline.sub(lambda m: m.group(1) + replace_destination(m.group("dest")) + m.group("tail"), text)
    html = re.compile(r"(\bsrc\s*=\s*[\"'])(?P<dest>[^\"']+)([\"'])", re.I)
    text = html.sub(lambda m: m.group(1) + replace_destination(m.group("dest")) + m.group(3), text)
    definition = re.compile(r"(?m)^(\s*\[[^\]]+\]:\s+)(?P<dest><[^>]+>|[^\s]+)(.*)$")
    text = definition.sub(lambda m: m.group(1) + replace_destination(m.group("dest")) + m.group(3), text)
    return text.encode("utf-8")


class MinerUClient:
    parser_id = "mineru"

    def __init__(self, endpoint: str, *, timeout: float = 30.0, limits: ParseLimits | None = None, transport=None):
        parsed = urllib.parse.urlparse(endpoint)
        if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"} or parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("MinerU endpoint must be an HTTP loopback URL")
        if timeout <= 0 or timeout > 300:
            raise ValueError("MinerU timeout is invalid")
        self.endpoint = endpoint.rstrip("/")
        self.timeout = timeout
        self.limits = limits or ParseLimits()
        self.transport = transport

    async def parse_pdf(self, pdf: bytes, filename: str = "document.pdf") -> ParsedDocument:
        if not isinstance(pdf, bytes) or not pdf or len(pdf) > self.limits.max_pdf_bytes or not isinstance(filename, str) or not filename or any(char in filename for char in "\r\n\x00"):
            raise MinerUError("PDF input is invalid")
        try:
            response, content_type = await self._post(pdf, filename)
        except TimeoutError as exc:
            raise MinerUError("MinerU request timed out") from exc
        except MinerUError:
            raise
        except httpx.RemoteProtocolError as exc:
            raise MinerUError("MinerU response is truncated") from exc
        except Exception as exc:
            raise MinerUError("MinerU request failed") from exc
        if response[:2] == b"PK" or "application/zip" in content_type:
            return self._zip(response)
        return self._json(response)

    async def _post(self, pdf: bytes, filename: str) -> tuple[bytes, str]:
        files = {"files": (filename, pdf, "application/pdf")}
        data = {"return_images": "true", "response_format_zip": "true"}
        chunks = bytearray()
        async with asyncio.timeout(self.timeout):
            async with httpx.AsyncClient(timeout=None, trust_env=False, follow_redirects=False, transport=self.transport) as client:
                async with client.stream("POST", self.endpoint + "/file_parse", files=files, data=data) as response:
                    if response.status_code < 200 or response.status_code >= 300:
                        raise MinerUError(f"MinerU HTTP request failed ({response.status_code})")
                    expected = response.headers.get("content-length")
                    expected_length = int(expected) if expected is not None else None
                    if expected_length is not None and expected_length > self.limits.max_response_bytes:
                        raise MinerUError("MinerU response exceeds size limit")
                    async for chunk in response.aiter_bytes():
                        chunks.extend(chunk)
                        if len(chunks) > self.limits.max_response_bytes:
                            raise MinerUError("MinerU response exceeds size limit")
                    if expected_length is not None and len(chunks) != expected_length:
                        raise MinerUError("MinerU response is truncated")
                    return bytes(chunks), response.headers.get("content-type", "")

    def _json(self, data: bytes) -> ParsedDocument:
        try:
            payload = json.loads(data.decode("utf-8"))
        except Exception as exc:
            raise MinerUError("MinerU response is not ZIP or valid JSON") from exc
        if not isinstance(payload, dict) or not isinstance(payload.get("markdown"), str):
            raise MinerUError("MinerU JSON response lacks explicit markdown")
        markdown = payload["markdown"].encode("utf-8")
        if not markdown.strip() or len(markdown) > self.limits.max_file_bytes:
            raise MinerUError("MinerU markdown is empty or exceeds size limit")
        assets = []
        asset_paths = set()
        if not isinstance(payload.get("assets", []), list) or len(payload.get("assets", [])) > self.limits.max_files:
            raise MinerUError("MinerU JSON contains too many assets")
        for item in payload.get("assets", []):
            if not isinstance(item, dict) or not isinstance(item.get("relative_path"), str) or not isinstance(item.get("content_base64"), str) or not isinstance(item.get("mime_type"), str):
                raise MinerUError("MinerU JSON asset is invalid")
            path = _path(item["relative_path"])
            if path in asset_paths: raise MinerUError("MinerU JSON contains duplicate assets")
            asset_paths.add(path)
            if path.rsplit(".", 1)[-1].lower() not in {x[1:] for x in _IMAGE_TYPES}:
                raise MinerUError("MinerU JSON asset is not an image")
            try: content = base64.b64decode(item["content_base64"], validate=True)
            except Exception as exc: raise MinerUError("MinerU JSON asset encoding is invalid") from exc
            if len(content) > self.limits.max_file_bytes or len(markdown) + sum(len(asset.content) for asset in assets) + len(content) > self.limits.max_uncompressed_bytes:
                raise MinerUError("MinerU JSON assets exceed size limits")
            assets.append(ParsedMedia(path, content, item["mime_type"]))
        return self._finish(markdown, tuple(assets), PARSER_VERSION)

    def _zip(self, data: bytes) -> ParsedDocument:
        try: archive = zipfile.ZipFile(io.BytesIO(data))
        except zipfile.BadZipFile as exc: raise MinerUError("MinerU ZIP response is invalid") from exc
        infos = archive.infolist()
        if len(infos) > self.limits.max_files: raise MinerUError("MinerU ZIP contains too many files")
        markdown_infos = []; assets = []; total = 0; names = set()
        for info in infos:
            raw_name = info.filename.rstrip("/")
            if not raw_name: continue
            name = _path(raw_name)
            if name in names: raise MinerUError("MinerU ZIP contains duplicate paths")
            names.add(name)
            if (info.external_attr >> 16) & 0o170000 == 0o120000: raise MinerUError("MinerU ZIP contains a link")
            if info.is_dir(): continue
            if info.file_size > self.limits.max_file_bytes or total + info.file_size > self.limits.max_uncompressed_bytes: raise MinerUError("MinerU ZIP exceeds size limits")
            content = archive.read(info); total += len(content)
            suffix = "." + name.rsplit(".", 1)[-1].lower() if "." in name.rsplit("/", 1)[-1] else ""
            if suffix in _MARKDOWN_SUFFIXES: markdown_infos.append((name, content))
            elif suffix in _IMAGE_TYPES: assets.append(ParsedMedia(name, content, _IMAGE_TYPES[suffix]))
        if len(markdown_infos) != 1: raise MinerUError("MinerU ZIP must contain exactly one markdown document")
        markdown_name, markdown = markdown_infos[0]
        if not markdown.strip(): raise MinerUError("MinerU markdown is empty")
        base_dir = markdown_name.rsplit("/", 1)[0] if "/" in markdown_name else ""
        return self._finish(markdown, tuple(assets), PARSER_VERSION, base_dir)

    def _finish(self, markdown: bytes, assets: tuple[ParsedMedia, ...], version: str, base_dir: str = "") -> ParsedDocument:
        paths = {asset.relative_path for asset in assets}
        raw_refs = _raw_references(markdown)
        normalized_refs = _references(markdown, base_dir)
        missing = normalized_refs - paths
        if missing: raise MinerUError("MinerU markdown references missing media")
        replacements = {}
        for raw in raw_refs:
            normalized = posixpath.normpath(posixpath.join(base_dir, urllib.parse.unquote(raw.split("#", 1)[0].split("?", 1)[0])))
            if normalized in paths: replacements[raw] = normalized
        rewritten = rewrite_media(markdown, replacements) if raw_refs else markdown
        if _references(rewritten, "", allow_knowledge_uri=True) - paths: raise MinerUError("MinerU markdown contains an unrewritten media reference")
        return ParsedDocument(rewritten, assets, self.parser_id, version)
