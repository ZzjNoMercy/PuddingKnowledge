"""Pure HTML-to-Markdown extraction for the independent Capture boundary.

This module deliberately performs no network I/O and no persistence.  Image
URLs remain Markdown links for the caller to cache or resolve separately.
"""

from __future__ import annotations

import ipaddress
import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import html2text
from bs4 import BeautifulSoup, Tag

_ARTICLE_ROOT_SELECTORS = (
    "[itemprop='articleBody']", "#article-body", ".article-content", ".article__body",
    ".post-content", ".entry-content", "article", "main",
)
_ARTICLE_NOISE_SELECTORS = (
    "script", "style", "noscript", "nav", "footer", "header", "aside", "form", "dialog",
    "[hidden]", "[aria-hidden='true']", ".advertisement", ".adsbygoogle", ".social-share",
    ".share-buttons", ".newsletter", ".subscribe", ".related-posts", "#comments", ".comments",
)
_LAZY_IMAGE_ATTRIBUTES = ("data-src", "data-original", "data-lazy-src", "data-actualsrc")
_WECHAT_HEADING_PATTERN = re.compile(r"^(?:\d+[.、．]|[一二三四五六七八九十]+[、.．])\s*\S+")
_TRACKING_PARAMS = {"fbclid", "gclid", "dclid", "msclkid", "mc_cid", "mc_eid", "igshid", "spm", "from"}
_X_HOSTS = {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}
_X_ARTICLE_IMAGE_PATTERN = re.compile(
    r'original_img_url\s*:\s*"(?P<url>https:(?:\\/|/){2}pbs\.twimg\.com(?:\\/|/)media(?:\\/|/)[^"?]+(?:\?[^" ]*)?)"'
)
_MARKDOWN_IMAGE_PATTERN = re.compile(r"!\[[^\]]*\]\(([^)\s]+)")
_X_OG_TITLE_AUTHOR_PATTERN = re.compile(
    r"^(?P<name>.+?)\s*\(@(?P<handle>[^)]+)\)\s+on\s+(?:X|Twitter)\b", re.IGNORECASE
)


def _validate_url(value: str) -> None:
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise ValueError("URL contains control characters")
    parsed = urlsplit(value.strip())
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        raise ValueError("only absolute http(s) URLs are supported")
    if parsed.username or parsed.password:
        raise ValueError("URL user information is not allowed")
    try:
        port = parsed.port
    except ValueError as error:
        raise ValueError("URL port is invalid") from error
    try:
        ipaddress.ip_address(parsed.hostname)
    except ValueError:
        try:
            parsed.hostname.encode("idna")
        except UnicodeError as error:
            raise ValueError("URL hostname is invalid") from error


def _canonical_host(hostname: str) -> str:
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return hostname.encode("idna").decode("ascii").lower()
    return f"[{address.compressed.lower()}]" if address.version == 6 else address.compressed


def canonicalize_url(value: str) -> str:
    """Return a stable HTTP URL without fragments or common tracking params."""

    raw = value.strip()
    _validate_url(raw)
    parsed = urlsplit(raw)
    host = _canonical_host(parsed.hostname)
    default_port = 80 if parsed.scheme.lower() == "http" else 443
    netloc = host if parsed.port in {None, default_port} else f"{host}:{parsed.port}"
    query = [
        (key, item)
        for key, item in parse_qsl(parsed.query, keep_blank_values=True)
        if not key.lower().startswith("utm_") and key.lower() not in _TRACKING_PARAMS
    ]
    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    canonical = urlunsplit((parsed.scheme.lower(), netloc, path, urlencode(sorted(query)), ""))
    if len(canonical.encode("utf-8")) > 1800:
        raise ValueError("canonical URL is too long")
    return canonical


def _meta(soup: BeautifulSoup, *selectors: tuple[str, str]) -> str:
    for attribute, value in selectors:
        node = soup.find("meta", attrs={attribute: value})
        if node and node.get("content"):
            return str(node.get("content")).strip()
    return ""


def _prepare_article_images(root: BeautifulSoup | Tag) -> None:
    for image in root.select("img"):
        lazy_source = next(
            (str(image.get(attribute) or "").strip() for attribute in _LAZY_IMAGE_ATTRIBUTES if image.get(attribute)),
            "",
        )
        if lazy_source:
            image["src"] = lazy_source
        if not image.get("alt"):
            figure = image.find_parent("figure")
            caption = figure.find("figcaption") if figure else None
            image["alt"] = caption.get_text(" ", strip=True) if caption else "文章图片"


def _prepare_wechat_article(root: BeautifulSoup | Tag) -> None:
    for node in root.select("[style*='display: none'], [style*='display:none']"):
        node.decompose()
    for block in root.find_all("section"):
        text = block.get_text(" ", strip=True)
        styled_heading = bool(
            block.find("strong")
            and (
                _WECHAT_HEADING_PATTERN.match(text)
                or text in {"写在最后", "结语", "总结"}
                or any(
                    re.search(r"font-size\s*:\s*(?:2[0-9]|[3-9][0-9])px", str(node.get("style") or ""), re.I)
                    for node in block.find_all(style=True)
                )
            )
            and len(text) <= 60
        )
        block.name = "h2" if styled_heading else "div"


def _append_x_article_images(markdown: str, *, source_html: str, page_url: str) -> str:
    if (urlsplit(page_url).hostname or "").lower() not in _X_HOSTS:
        return markdown
    existing = set(_MARKDOWN_IMAGE_PATTERN.findall(markdown))
    supplemental: list[str] = []
    for match in _X_ARTICLE_IMAGE_PATTERN.finditer(source_html):
        image_url = match.group("url").replace("\\/", "/")
        if image_url not in existing and image_url not in supplemental:
            supplemental.append(image_url)
    if not supplemental:
        return markdown
    images = "\n\n".join(f"![原文图片 {index}]({image_url})" for index, image_url in enumerate(supplemental, 1))
    return f"{markdown.rstrip()}\n\n## 原文图片\n\n{images}"


def extract_article(html: str, url: str) -> tuple[dict[str, str], str]:
    """Extract metadata and readable Markdown from already-fetched HTML."""

    if not isinstance(html, str) or not html.strip():
        raise ValueError("HTML must be non-empty")
    _validate_url(url)
    soup = BeautifulSoup(html, "lxml")
    title = _meta(soup, ("property", "og:title"), ("name", "twitter:title"))
    if not title and soup.title:
        title = soup.title.get_text(" ", strip=True)
    if not title:
        heading = soup.find("h1")
        title = heading.get_text(" ", strip=True) if heading else ""
    metadata = {
        "title": title,
        "author": _meta(soup, ("name", "author"), ("property", "article:author")),
        "site_name": _meta(soup, ("property", "og:site_name")),
        "description": _meta(soup, ("name", "description"), ("property", "og:description")),
        "image_url": _meta(soup, ("property", "og:image"), ("name", "twitter:image")),
    }
    if metadata["image_url"]:
        metadata["image_url"] = urljoin(url, metadata["image_url"])
    host = (urlsplit(url).hostname or "").lower()
    is_wechat = host == "mp.weixin.qq.com" or host.endswith(".mp.weixin.qq.com")
    if host in _X_HOSTS:
        author_match = _X_OG_TITLE_AUTHOR_PATTERN.match(metadata["title"])
        if author_match and not metadata["author"]:
            metadata["author"] = author_match.group("name").strip()
        tweet_text = re.sub(r"\s+", " ", metadata["description"]).strip()
        if tweet_text:
            metadata["title"] = tweet_text if len(tweet_text) <= 120 else f"{tweet_text[:117].rstrip()}..."
    if is_wechat:
        root = soup.select_one("#js_content")
        author_node = soup.select_one("#js_name, .rich_media_meta_nickname")
        if not metadata["author"] and author_node:
            metadata["author"] = author_node.get_text(" ", strip=True)
        metadata["site_name"] = metadata["site_name"] or "微信公众平台"
    else:
        root = next((soup.select_one(selector) for selector in _ARTICLE_ROOT_SELECTORS if soup.select_one(selector)), None)
    root = root or soup.body or soup
    for tag in root.select(",".join(_ARTICLE_NOISE_SELECTORS)):
        tag.decompose()
    _prepare_article_images(root)
    if is_wechat:
        _prepare_wechat_article(root)
    else:
        for section in root.find_all("section"):
            section.name = "div"
    converter = html2text.HTML2Text()
    converter.ignore_links = False
    converter.ignore_images = False
    converter.body_width = 0
    converter.baseurl = url
    markdown = converter.handle(str(root)).strip()
    markdown = re.sub(r"^(#{1,6})\s+\*\*(.+)\*\*\s*$", r"\1 \2", markdown, flags=re.M)
    markdown = re.sub(r"^(#{1,6}\s+)(\d+)\\\.", r"\1\2.", markdown, flags=re.M)
    markdown = re.sub(r"[ \t]+\n", "\n", markdown)
    markdown = re.sub(r"\n{3,}", "\n\n", markdown)
    markdown = _append_x_article_images(markdown, source_html=html, page_url=url)
    return metadata, markdown


__all__ = ["canonicalize_url", "extract_article"]
