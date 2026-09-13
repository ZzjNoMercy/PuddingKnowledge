"""Bounded deterministic Wiki lint application service, independent of Claw and gbrain.

The caller supplies an already-resolved schema contract and immutable text inputs.
A lint result is not proof that its schema bundle was independently admitted.
"""
from __future__ import annotations
from collections import Counter
from dataclasses import dataclass
from typing import Any
import re
import yaml

class LlmWikiError(ValueError): pass

@dataclass(frozen=True)
class WikiLintContract:
    bundle_version: str
    bundle_hash: str
    allowed_page_types: tuple[str, ...]
    required_frontmatter: tuple[str, ...]
    prefixes_by_type: dict[str, tuple[str, ...]]

class _StrictLoader(yaml.SafeLoader):
    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise LlmWikiError("YAML aliases are unsupported")
        return super().compose_node(parent, index)
    def construct_mapping(self, node, deep=False):
        result = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in result:
                raise LlmWikiError("YAML mapping keys must be unique strings")
            result[key] = self.construct_object(value_node, deep=deep)
        return result


def _frontmatter(content: str, *, source: str):
    if not content.startswith("---\n"):
        raise LlmWikiError(f"{source}: missing YAML frontmatter")
    end = content.find("\n---\n", 4)
    if end < 0:
        raise LlmWikiError(f"{source}: unterminated YAML frontmatter")
    if end > 65536:
        raise LlmWikiError(f"{source}: frontmatter exceeds limit")
    try:
        value = yaml.load(content[4:end], Loader=_StrictLoader)
    except (yaml.YAMLError, RecursionError, LlmWikiError) as error:
        raise LlmWikiError(f"{source}: invalid YAML frontmatter") from error
    if not isinstance(value, dict):
        raise LlmWikiError(f"{source}: frontmatter must be an object")
    return value, content[end + 5:]


def _validate_inputs(contract, pages, index, log_present, raw_hashes, raw_manifest_sha256):
    if not isinstance(contract, WikiLintContract) or not isinstance(contract.bundle_version, str) or not contract.bundle_version or len(contract.bundle_version) > 160:
        raise ValueError("Invalid Wiki schema contract")
    if not isinstance(contract.bundle_hash, str) or not re.fullmatch(r"[0-9a-f]{64}", contract.bundle_hash):
        raise ValueError("Invalid schema bundle digest")
    for values in (contract.allowed_page_types, contract.required_frontmatter):
        if not isinstance(values, tuple) or not values or len(values)>1000 or any(not isinstance(v,str) or not v or len(v)>160 for v in values) or len(set(values))!=len(values):
            raise ValueError("Invalid schema declarations")
    if not isinstance(contract.prefixes_by_type, dict) or len(contract.prefixes_by_type)>1000:
        raise ValueError("Invalid schema prefixes")
    for key, values in contract.prefixes_by_type.items():
        if not isinstance(key,str) or not isinstance(values,tuple) or len(values)>1000 or any(not isinstance(v,str) or not v or len(v)>1024 or v.startswith('/') or '..' in v.split('/') for v in values):
            raise ValueError("Invalid schema prefixes")
    if type(log_present) is not bool or not isinstance(pages,dict) or len(pages)>5000 or not isinstance(raw_hashes,dict) or len(raw_hashes)>50000:
        raise ValueError("Invalid Wiki input inventory")
    texts=[]
    for slug,content in pages.items():
        if not isinstance(slug,str) or not slug or len(slug)>1024 or slug in {'index','log'} or not isinstance(content,str):
            raise ValueError("Invalid Wiki page input")
        texts.append(content)
    if index is not None:
        if not isinstance(index,str):raise ValueError("Invalid index")
        texts.append(index)
    total=0
    for content in texts:
        size=len(content.encode('utf-8'));total+=size
        if size>8*1024*1024 or total>64*1024*1024:raise ValueError("Wiki text budget exceeded")
    for path,digest in raw_hashes.items():
        if not isinstance(path,str) or not path or len(path)>1024 or path.startswith('/') or any(p in {'','.','..'} for p in path.split('/')) or not isinstance(digest,str) or len(digest)>200:
            raise ValueError("Invalid Raw facts")
        if digest!='missing' and not re.fullmatch(r"[0-9a-f]{64}|mismatch:[0-9a-f]{64}:[0-9a-f]{64}",digest):raise ValueError("Invalid Raw digest")
    if raw_manifest_sha256 is not None and (not isinstance(raw_manifest_sha256,str) or not re.fullmatch(r"[0-9a-f]{64}",raw_manifest_sha256)):
        raise ValueError("Invalid Raw manifest digest")

SLUG_SEGMENT_PATTERN = r"[a-z0-9]+(?:-[a-z0-9]+)*"
SLUG_PATTERN = rf"{SLUG_SEGMENT_PATTERN}(?:/{SLUG_SEGMENT_PATTERN})*"
TYPED_SLUG_PATTERN = rf"{SLUG_SEGMENT_PATTERN}(?:/{SLUG_SEGMENT_PATTERN})+"
SLUG_RE = re.compile(rf"^{SLUG_PATTERN}$")
WIKILINK_TARGET_RE = re.compile(rf"^(?P<slug>{TYPED_SLUG_PATTERN})$")
WIKILINK_RE = re.compile(rf"\[\[({TYPED_SLUG_PATTERN})(?:\|[^\]]+)?\]\]")
ANY_WIKILINK_RE = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]+)?\]\]")
SOURCED_FROM_WIKILINK_RE = re.compile(
    rf"\bsourced_from\b[^\n]*?\[\[(?P<slug>{TYPED_SLUG_PATTERN})(?:\|[^\]]+)?\]\]"
)
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

def _markdown_level_two_section(body: str, title: str) -> str:
    """Return one level-two Markdown section without interpreting prose as structure."""

    match = re.search(
        rf"(?ms)^##\s+{re.escape(title)}\s*$\n(?P<body>.*?)(?=^##\s+|\Z)",
        body,
    )
    return match.group("body") if match else ""

def _canonical_raw_source(value: Any, known_sources: set[str] | None = None) -> str:
    """Return the manifest-relative source path used by the Wiki contract."""

    source = str(value or "").strip()
    if known_sources is not None and source in known_sources:
        return source
    legacy_candidate = source[4:] if source.startswith("raw/") else source
    if known_sources is None or legacy_candidate in known_sources:
        return legacy_candidate
    return source


_SOURCE_MARKER_RE = re.compile(r"\bsourced_from\b")
_SOURCE_TARGET_RE = re.compile(rf"\[\[(?P<slug>{TYPED_SLUG_PATTERN})(?:\|[^\]]+)?\]\]")

def _sourced_from_targets(body: str) -> set[str]:
    # Advance independent marker/link iterators instead of rescanning a long
    # suffix for every marker. Preserve the legacy consumed-match semantics.
    targets = set()
    for line in body.split("\n"):
        links = iter(_SOURCE_TARGET_RE.finditer(line))
        link = next(links, None)
        consumed_until = 0
        for marker in _SOURCE_MARKER_RE.finditer(line):
            if marker.start() < consumed_until:
                continue
            while link is not None and link.start() < marker.end():
                link = next(links, None)
            if link is None:
                break
            targets.add(link.group("slug"))
            consumed_until = link.end()
            link = next(links, None)
    return targets

def lint_workspace(*, contract: WikiLintContract, pages: dict[str, str], index: str | None, log_present: bool, raw_hashes: dict[str, str], raw_manifest_sha256: str | None) -> dict[str, Any]:
    _validate_inputs(contract, pages, index, log_present, raw_hashes, raw_manifest_sha256)
    allowed_types = set(contract.allowed_page_types)
    prefixes_by_type = dict(contract.prefixes_by_type)
    required = list(contract.required_frontmatter)
    bundle_version = contract.bundle_version
    errors: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    pages = dict(sorted(pages.items(), key=lambda item: tuple((item[0] + ".md").split("/"))))
    page_types: dict[str, str] = {}
    page_bodies: dict[str, str] = {}
    raw_names = set(raw_hashes)

    def finding(collection: list[dict[str, str]], code: str, path: str, message: str) -> None:
        if len(errors) + len(warnings) >= 10000:
            raise ValueError("Wiki lint finding budget exceeded")
        collection.append({"code": code, "path": path, "message": message})

    for slug, content in pages.items():
        path = f"wiki/{slug}.md"
        if not SLUG_RE.fullmatch(slug):
            finding(errors, "invalid_slug", path, "filename must be a lowercase hyphen slug")
        elif slug.startswith("wiki/"):
            finding(
                errors,
                "duplicate_wiki_root",
                path,
                "page slug is already relative to the wiki/ root and must not start with wiki/",
            )
        try:
            metadata, body = _frontmatter(content, source=path)
        except LlmWikiError as exc:
            finding(errors, "invalid_frontmatter", path, str(exc))
            continue
        for field in required:
            if field not in metadata or metadata[field] in (None, "", []):
                finding(errors, "missing_frontmatter", path, f"required field is missing: {field}")
        page_type = str(metadata.get("type") or "")
        page_types[slug] = page_type
        page_bodies[slug] = body
        if page_type and page_type not in allowed_types:
            finding(errors, "unknown_page_type", path, f"type {page_type!r} is not in the resolved Schema")
        elif page_type:
            prefixes = prefixes_by_type.get(page_type, [])
            relative_page_path = f"{slug}.md"
            if prefixes and not any(relative_page_path.startswith(prefix) for prefix in prefixes):
                finding(
                    errors,
                    "page_path_type_mismatch",
                    path,
                    f"type {page_type!r} requires one of these path prefixes: {', '.join(prefixes)}",
                )
        updated = str(metadata.get("updated") or "")
        if updated and not DATE_RE.fullmatch(updated):
            finding(errors, "invalid_updated", path, "updated must be YYYY-MM-DD")
        schema_version = str(metadata.get("schema_version") or "")
        if schema_version and schema_version != bundle_version:
            finding(errors, "schema_drift", path, f"expected schema_version {bundle_version!r}")
        sources = metadata.get("sources")
        if not isinstance(sources, list):
            finding(errors, "invalid_sources", path, "sources must be a list")
        else:
            for source in sources:
                canonical_source = _canonical_raw_source(source, raw_names)
                if canonical_source not in raw_names:
                    finding(errors, "unknown_source", path, f"source is not in raw manifest: {source}")
                elif canonical_source != str(source):
                    finding(
                        warnings,
                        "legacy_source_prefix",
                        path,
                        f"source should use snapshot_path without the raw/ prefix: {canonical_source}",
                    )
        for target in ANY_WIKILINK_RE.findall(body):
            match = WIKILINK_TARGET_RE.fullmatch(target)
            if match is None:
                finding(
                    errors,
                    "invalid_wikilink",
                    path,
                    f"target must include its type directory: [[<type-directory>/<slug>]], got [[{target}]]",
                )
            elif match.group("slug").startswith("wiki/"):
                finding(
                    errors,
                    "duplicate_wiki_root_link",
                    path,
                    f"wikilink is already relative to the wiki/ root: [[{target}]]",
                )
            elif match.group("slug") not in pages:
                finding(errors, "broken_wikilink", path, f"target does not exist: {target}")

    sourced_from_by_media: dict[str, set[str]] = {}
    for media_slug, body in page_bodies.items():
        if page_types.get(media_slug) != "media":
            continue
        source_slugs = _sourced_from_targets(body)
        sourced_from_by_media[media_slug] = source_slugs
        for source_slug in sorted(source_slugs):
            if source_slug not in pages:
                continue  # The ordinary broken_wikilink finding already reports this.
            if page_types.get(source_slug) != "source":
                finding(
                    errors,
                    "invalid_sourced_from_target",
                    f"wiki/{media_slug}.md",
                    f"sourced_from target must be a source page: {source_slug}",
                )
                continue
            source_links = set(WIKILINK_RE.findall(page_bodies.get(source_slug, "")))
            if media_slug not in source_links:
                finding(
                    errors,
                    "missing_source_backlink",
                    f"wiki/{source_slug}.md",
                    f"source must link collected media page in 已收录内容: {media_slug}",
                )

    for source_slug, body in page_bodies.items():
        if page_types.get(source_slug) != "source":
            continue
        collected_section = _markdown_level_two_section(body, "已收录内容")
        collected_media = WIKILINK_RE.findall(collected_section)
        seen_media: set[str] = set()
        for media_slug in collected_media:
            if media_slug in seen_media:
                finding(
                    errors,
                    "duplicate_source_collection_link",
                    f"wiki/{source_slug}.md",
                    f"已收录内容 contains duplicate media link: {media_slug}",
                )
                continue
            seen_media.add(media_slug)
            if media_slug not in pages:
                continue  # The ordinary broken_wikilink finding already reports this.
            if page_types.get(media_slug) != "media":
                finding(
                    errors,
                    "invalid_source_collection_target",
                    f"wiki/{source_slug}.md",
                    f"已收录内容 must link a media page: {media_slug}",
                )
            elif source_slug not in sourced_from_by_media.get(media_slug, set()):
                finding(
                    errors,
                    "missing_media_source_relation",
                    f"wiki/{media_slug}.md",
                    f"media listed by {source_slug} must declare sourced_from [[{source_slug}]]",
                )

    if index is None:
        finding(errors, "missing_index", "wiki/index.md", "index.md is required")
    else:
        index_raw = index
        for target in ANY_WIKILINK_RE.findall(index_raw):
            match = WIKILINK_TARGET_RE.fullmatch(target)
            if match is None:
                finding(
                    errors,
                    "invalid_index_wikilink",
                    "wiki/index.md",
                    f"target must include its type directory: [[<type-directory>/<slug>]], got [[{target}]]",
                )
            elif match.group("slug").startswith("wiki/"):
                finding(
                    errors,
                    "duplicate_wiki_root_link",
                    "wiki/index.md",
                    f"wikilink is already relative to the wiki/ root: [[{target}]]",
                )
        index_links = set(WIKILINK_RE.findall(index_raw))
        for slug in sorted(set(pages) - index_links):
            finding(errors, "index_omission", "wiki/index.md", f"page is not indexed: {slug}")
        for slug in sorted(index_links - set(pages)):
            finding(errors, "index_broken_link", "wiki/index.md", f"index target does not exist: {slug}")

    if not log_present:
        finding(errors, "missing_log", "wiki/log.md", "log.md is required")
    for path, digest in raw_hashes.items():
        if digest == "missing" or digest.startswith("mismatch:"):
            finding(errors, "raw_hash_mismatch", f"raw/{path}", digest)

    inbound = Counter(WIKILINK_RE.findall("\n".join(pages.values())))
    for slug in pages:
        if inbound[slug] == 0 and len(pages) > 1:
            finding(warnings, "orphan_page", f"wiki/{slug}.md", "page has no inbound Wiki link")
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "counts": {"pages": len(pages), "errors": len(errors), "warnings": len(warnings)},
        "bundle_hash": contract.bundle_hash,
        "raw_manifest_sha256": raw_manifest_sha256,
    }
