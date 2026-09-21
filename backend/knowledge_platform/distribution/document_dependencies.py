"""Build a bounded, offline dependency graph for document bodies."""
from __future__ import annotations

from html.parser import HTMLParser
import hashlib
import os
import posixpath
from pathlib import Path, PurePosixPath
import re
from typing import Mapping
from urllib.parse import quote, urlsplit, unquote

from . import wiki_archive as files


_PERCENT = re.compile(r"%(?![0-9A-Fa-f]{2})")
_DOCUMENT_SUFFIXES = {".md", ".markdown", ".html", ".htm"}
_EXTERNAL_SCHEMES = {"http", "https", "mailto", "data"}
_HTML_URL_ATTRIBUTES = {"src", "href", "data", "poster"}


def normalize_virtual_roots(virtual_roots):
    """Validate virtual reference roots as (prefix, relative_root) pairs.

    Legacy bodies carry absolute references in the product's virtual namespace
    (e.g. /knowledge/assets/...). Each declared prefix is absolute POSIX and
    rebinds onto a canonical root-relative directory in the inspected tree.
    """
    normalized = []
    for prefix, target in virtual_roots or ():
        if not isinstance(prefix, str) or not prefix.startswith("/") or "\\" in prefix:
            raise ValueError("Invalid virtual reference prefix")
        canonical = posixpath.normpath(prefix)
        if canonical != prefix or prefix != "/" and prefix.endswith("/") or "//" in prefix:
            raise ValueError("Invalid virtual reference prefix")
        if canonical == "/" or any(part in {".", ".."} for part in prefix.split("/")[1:]):
            raise ValueError("Invalid virtual reference prefix")
        if not isinstance(target, str) or "\\" in target:
            raise ValueError("Invalid virtual reference target")
        target_path = PurePosixPath(target)
        if target_path.is_absolute() or target_path.as_posix() != target or any(
                part in {"", ".", ".."} for part in target_path.parts):
            raise ValueError("Invalid virtual reference target")
        normalized.append((prefix, target))
    prefixes = [prefix for prefix, _ in normalized]
    if len(set(prefixes)) != len(prefixes):
        raise ValueError("Duplicate virtual reference prefix")
    return normalized


class _ReferenceParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.references: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in {'base', 'script', 'style', 'iframe', 'object', 'embed'}:
            raise ValueError('Unsupported dynamic or base-changing HTML dependency')
        attributes = dict(attrs)
        if tag.lower() == 'link' and 'stylesheet' in (attributes.get('rel') or '').lower().split():
            raise ValueError('CSS dependency traversal is unsupported')
        if re.search(r'url\s*\(|@import', attributes.get('style') or '', re.I):
            raise ValueError('Inline CSS dependency traversal is unsupported')
        for name, value in attrs:
            if value is None:
                continue
            name = name.lower()
            if name in _HTML_URL_ATTRIBUTES:
                self.references.append(value)
            elif name == "srcset":
                self.references.extend(_srcset(value))
            elif name in {"action", "formaction", "cite", "background", "profile"}:
                # These are URL-bearing HTML attributes too; handling them
                # explicitly prevents silently accepting an untracked local ref.
                self.references.append(value)

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)


def _srcset(value: str) -> list[str]:
    if 'data:' in value.lower():
        raise ValueError('Data URL srcset parsing is unsupported')
    result = []
    for candidate in value.split(","):
        candidate = candidate.strip()
        if candidate:
            result.append(candidate.split()[0])
    return result


def _markdown_references(text: str) -> list[str]:
    try:
        from markdown_it import MarkdownIt
    except ImportError as exc:  # pragma: no cover - backend declares this dependency
        raise ValueError("markdown-it is required for document dependency parsing") from exc
    parser = MarkdownIt()
    refs: list[str] = []
    def visit(token):
        if token.type in {'link_open', 'image'}:
            attrs = dict(token.attrs or [])
            refs.extend(attrs[key] for key in ('href', 'src') if key in attrs)
        elif token.type in {'html_inline', 'html_block'}:
            html = _ReferenceParser(); html.feed(token.content); html.close()
            refs.extend(html.references)
        for child in token.children or []:
            visit(child)
    for token in parser.parse(text):
        visit(token)
    return refs


def _references(path: Path, text: str, mime_type=None) -> list[str]:
    suffix = path.suffix.lower()
    if suffix in {'.md', '.markdown'} or (suffix not in {'.html', '.htm'} and mime_type == 'text/markdown'):
        return _markdown_references(text)
    if mime_type == 'text/html' or suffix in {'.html', '.htm'}:
        parser = _ReferenceParser(); parser.feed(text); parser.close()
        return parser.references
    return []


def _external(value: str) -> str:
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()


def _exists_no_follow(root: Path, relative: str) -> bool:
    try:
        os.lstat(root / Path(*relative.split("/")))
    except OSError:
        return False
    return True


def _virtual_target(root: Path, candidate: str) -> tuple[str, None]:
    normalized = posixpath.normpath(candidate)
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError("Document reference escapes root")
    relative = PurePosixPath(normalized).as_posix()
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("Invalid document reference")
    if not _exists_no_follow(root, relative):
        # Legacy connector assets are stored with percent-encoded file names
        # while bodies reference the decoded names; bind the encoded file.
        encoded = "/".join(quote(part, safe="") for part in relative.split("/"))
        if encoded != relative and _exists_no_follow(root, encoded):
            relative = encoded
    return relative, None


def _resolve(root: Path, source: str, reference: str, virtual_roots=()) -> tuple[str | None, str | None]:
    if not isinstance(reference, str) or any(ord(character) < 32 or ord(character) == 127 for character in reference) or "\\" in reference:
        raise ValueError("Invalid document reference")
    if _PERCENT.search(reference):
        raise ValueError("Invalid percent escape in document reference")
    parts = urlsplit(reference)
    if parts.scheme:
        if parts.scheme.lower() in _EXTERNAL_SCHEMES:
            return None, _external(reference)
        raise ValueError("Unsupported document reference scheme")
    if parts.netloc:
        return None, _external(reference)
    try:
        raw_path = unquote(parts.path, errors='strict')
    except UnicodeDecodeError as exc:
        raise ValueError('Invalid document reference encoding') from exc
    if any(ord(character) < 32 or ord(character) == 127 for character in raw_path) or '\\' in raw_path:
        raise ValueError('Invalid document reference')
    decoded = urlsplit(raw_path)
    if decoded.scheme and decoded.scheme.lower() in _EXTERNAL_SCHEMES:
        # A percent-encoded URL decodes to a scheme the pre-decode parse
        # cannot see; classify it external without fetching.
        return None, _external(reference)
    if not raw_path or raw_path == ".":
        return None, None
    if raw_path.startswith("/"):
        for prefix, target in virtual_roots:
            if raw_path == prefix or raw_path.startswith(prefix + "/"):
                remainder = raw_path[len(prefix):].lstrip("/")
                candidate = target if not remainder else target + "/" + remainder
                return _virtual_target(root, candidate)
        raise ValueError("Absolute document reference")
    normalized = posixpath.normpath(posixpath.join(posixpath.dirname(source), raw_path))
    if normalized in {"", "."} or normalized == ".." or normalized.startswith("../"):
        raise ValueError("Document reference escapes root")
    # Keep the graph canonical and reject paths that cannot be represented as
    # the archive's slash-separated relative names.
    relative = PurePosixPath(normalized).as_posix()
    if any(part in {"", ".", ".."} for part in relative.split("/")):
        raise ValueError("Invalid document reference")
    return relative, None


def collect_document_dependencies(root: Path, primary: Mapping[str, str], *, virtual_roots=()) -> dict:
    """Collect hashes and local edges reachable from the primary documents.

    Every read is performed through :mod:`wiki_archive`, so symlinks, hardlinks,
    changed files, and size budgets are checked at the descriptor level.
    Absolute references in a declared virtual namespace rebind onto the
    inspected tree; any other absolute reference is refused.
    """
    virtual_roots = normalize_virtual_roots(virtual_roots)
    root = files._path(Path(root))
    files._check(root.lstat(), directory=True)
    if not isinstance(primary, Mapping):
        raise ValueError("Primary documents must be a mapping")
    if len(primary) > files.MAX_FILES:
        raise ValueError("Document dependency file count exceeds budget")
    queue: list[str] = []
    for relative in primary:
        if not isinstance(relative, str) or "\\" in relative or not relative:
            raise ValueError("Invalid primary document path")
        try:
            files._relative(relative)
        except ValueError as exc:
            raise ValueError("Invalid primary document path") from exc
        queue.append(relative)
    queue.sort()
    scheduled = set(queue)
    facts: dict[str, dict[str, int | str]] = {}
    edges: set[tuple[str, str]] = set()
    external: set[str] = set()
    total = 0
    index = 0
    while index < len(queue):
        relative = queue[index]
        index += 1
        if relative in facts:
            continue
        if len(facts) >= files.MAX_FILES:
            raise ValueError("Document dependency file count exceeds budget")
        path = root / Path(*relative.split("/"))
        try:
            data, fact = files._read(path)
        except (FileNotFoundError, NotADirectoryError) as exc:
            raise ValueError("Missing document dependency") from exc
        total += fact["size_bytes"]
        if total > files.MAX_TOTAL:
            raise ValueError("Document dependency total exceeds budget")
        facts[relative] = fact
        mime_type = primary.get(relative)
        if mime_type not in {'text/markdown', 'text/html'} and path.suffix.lower() not in _DOCUMENT_SUFFIXES:
            continue
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ValueError("Document body is not valid UTF-8") from exc
        for reference in _references(path, text, mime_type):
            target, external_hash = _resolve(root, relative, reference, virtual_roots)
            if external_hash:
                external.add(external_hash)
            elif target is not None:
                # Ensure the target exists and is a regular, non-linked file;
                # it is read when reached, even if it is a binary attachment.
                if target not in scheduled:
                    if len(scheduled) >= files.MAX_FILES:
                        raise ValueError('Dependency discovery exceeds file budget')
                    scheduled.add(target); queue.append(target)
                edges.add((relative, target))
            if len(edges) + len(external) > files.MAX_FILES * 4:
                raise ValueError('Dependency graph exceeds edge budget')
    return {
        "files": {key: facts[key] for key in sorted(facts)},
        "external_references": sorted(external),
        "edges": [{"source": source, "target": target} for source, target in sorted(edges)],
    }
