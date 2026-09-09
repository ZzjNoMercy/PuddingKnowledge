"""Bounded, public-network HTTP capture transport.

This module is deliberately a transport only.  It does not import a Tool,
legacy network helper, or an HTML/document parser.  Every request resolves its
hostname first, validates every returned address, and then connects to one of
those exact addresses while retaining the original Host header and (for TLS)
the original SNI name.
"""

from __future__ import annotations

import ipaddress
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urljoin, urlsplit, urlunsplit

import certifi
import urllib3
from urllib3.connectionpool import HTTPConnectionPool, HTTPSConnectionPool
from urllib3.util import Timeout

MAX_REDIRECTS = 5
MAX_RESPONSE_BYTES = 5 * 1024 * 1024
READ_CHUNK_BYTES = 64 * 1024
_TIMEOUT = Timeout(connect=5.0, read=15.0)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})


class UnsafePublicURL(ValueError):
    """The URL or one of its resolved network targets is not permitted."""


@dataclass(frozen=True, slots=True)
class PublicURLResponse:
    """The bounded bytes returned by one public URL request."""

    url: str
    content_type: str
    body: bytes


@dataclass(frozen=True, slots=True)
class _TransportResponse:
    status: int
    headers: dict[str, str]
    body: bytes


def _hostname(value: str) -> str:
    try:
        return value.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as error:
        raise UnsafePublicURL("invalid hostname") from error


def _origin(scheme: str, hostname: str, port: int) -> str:
    host = f"[{hostname}]" if ":" in hostname else hostname
    default = (scheme == "http" and port == 80) or (scheme == "https" and port == 443)
    return f"{scheme}://{host}" + (f":{port}" if not default else "")


def _parse_url(value: str, *, allowed_origins: frozenset[str]) -> tuple[str, str, int, str, bool]:
    if not isinstance(value, str) or not value or value != value.strip():
        raise UnsafePublicURL("URL must be a non-empty string without surrounding whitespace")
    if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
        raise UnsafePublicURL("URL contains a control character")
    try:
        parsed = urlsplit(value)
        scheme = parsed.scheme.lower()
        hostname_value = parsed.hostname
        port = parsed.port
    except ValueError as error:
        raise UnsafePublicURL("malformed URL") from error
    if scheme not in {"http", "https"} or not hostname_value:
        raise UnsafePublicURL("only HTTP(S) URLs are allowed")
    if parsed.username is not None or parsed.password is not None:
        raise UnsafePublicURL("URL user information is not allowed")
    if parsed.fragment:
        raise UnsafePublicURL("URL fragments are not allowed")
    hostname = _hostname(hostname_value)
    if not hostname:
        raise UnsafePublicURL("invalid hostname")
    if port is None:
        port = 80 if scheme == "http" else 443
    if not 1 <= port <= 65535:
        raise UnsafePublicURL("URL port is invalid")
    canonical_origin = _origin(scheme, hostname, port)
    allowed = canonical_origin in allowed_origins
    # Non-standard ports are only useful for an explicitly configured local
    # fixture/private service.  Public capture stays on the standard ports.
    if not allowed and port != (80 if scheme == "http" else 443):
        raise UnsafePublicURL("non-standard ports require an allowed origin")
    path = parsed.path or "/"
    host = hostname if ":" not in hostname else f"[{hostname}]"
    netloc = host if port == (80 if scheme == "http" else 443) else f"{host}:{port}"
    request_url = urlunsplit((scheme, netloc, path, parsed.query, ""))
    return scheme, hostname, port, request_url, allowed


def _validated_url(value: str) -> tuple[str, str, int, str]:
    """Compatibility helper for boundary tests and transport diagnostics."""

    scheme, hostname, port, request_url, _allowed = _parse_url(value, allowed_origins=frozenset())
    parsed = urlsplit(request_url)
    path = parsed.path + (("?" + parsed.query) if parsed.query else "")
    return scheme, hostname, port, path


def _normalise_allowed_origins(values: object) -> frozenset[str]:
    if values is None:
        return frozenset()
    if isinstance(values, (str, bytes)):
        raise ValueError("allowed_origins must be an iterable of complete origins")
    try:
        candidates = tuple(values)  # type: ignore[arg-type]
    except TypeError as error:
        raise ValueError("allowed_origins must be an iterable of complete origins") from error
    normalized: set[str] = set()
    for value in candidates:
        if not isinstance(value, str) or not value or value != value.strip():
            raise ValueError("allowed origin is invalid")
        if any(ord(character) < 0x20 or ord(character) == 0x7F for character in value):
            raise ValueError("allowed origin is invalid")
        try:
            parsed = urlsplit(value)
            scheme = parsed.scheme.lower()
            host_value = parsed.hostname
            port = parsed.port
        except ValueError as error:
            raise ValueError("allowed origin is invalid") from error
        if scheme not in {"http", "https"} or not host_value or parsed.username is not None or parsed.password is not None:
            raise ValueError("allowed origin is invalid")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("allowed origin must not contain path, query, or fragment")
        hostname = _hostname(host_value)
        if not hostname:
            raise ValueError("allowed origin is invalid")
        normalized.add(_origin(scheme, hostname, port or (80 if scheme == "http" else 443)))
    return frozenset(normalized)


def _resolve_public_addresses(
    hostname: str,
    port: int,
    *,
    allow_private: bool = False,
    scheme: str = "https",
) -> tuple[str, ...]:
    del scheme  # The public/private decision is address based; scheme is a diagnostic hint.
    try:
        records = socket.getaddrinfo(hostname, port, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except OSError as error:
        raise UnsafePublicURL("hostname could not be resolved") from error
    addresses: list[str] = []
    for record in records:
        raw = str(record[4][0])
        try:
            address = ipaddress.ip_address(raw)
        except ValueError as error:
            raise UnsafePublicURL("hostname resolved to an invalid address") from error
        if not allow_private and not address.is_global:
            raise UnsafePublicURL("target resolves to a non-public address")
        rendered = str(address)
        if rendered not in addresses:
            addresses.append(rendered)
    if not addresses:
        raise UnsafePublicURL("hostname has no address")
    return tuple(addresses)


def _read_body(response: urllib3.response.HTTPResponse) -> bytes:
    declared = response.headers.get("content-length")
    if declared is not None:
        try:
            declared_bytes = int(declared)
            if declared_bytes < 0:
                raise ValueError("negative length")
            if declared_bytes > MAX_RESPONSE_BYTES:
                raise ValueError("capture response exceeds the 5 MiB limit")
        except ValueError as error:
            if str(error).startswith("capture response"):
                raise
            raise ValueError("capture response length is invalid") from error
    body = bytearray()
    for chunk in response.stream(READ_CHUNK_BYTES, decode_content=True):
        body.extend(chunk)
        if len(body) > MAX_RESPONSE_BYTES:
            raise ValueError("capture response exceeds the 5 MiB limit")
    return bytes(body)


def _request_once(*, scheme: str, hostname: str, port: int, request_url: str, address: str) -> _TransportResponse:
    host_header = f"[{hostname}]" if ":" in hostname else hostname
    if port not in {80, 443}:
        host_header += f":{port}"
    headers = {
        "Host": host_header,
        "User-Agent": "PuddingKnowledge-Capture/1",
        "Accept": "text/html,application/xhtml+xml,application/json,text/plain;q=0.9,*/*;q=0.1",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "close",
    }
    pool: HTTPConnectionPool | HTTPSConnectionPool
    if scheme == "https":
        pool = HTTPSConnectionPool(
            address,
            port=port,
            cert_reqs=ssl.CERT_REQUIRED,
            ca_certs=certifi.where(),
            assert_hostname=hostname,
            server_hostname=hostname,
            timeout=_TIMEOUT,
            retries=False,
            maxsize=1,
        )
    else:
        pool = HTTPConnectionPool(address, port=port, timeout=_TIMEOUT, retries=False, maxsize=1)
    response = None
    try:
        response = pool.urlopen(
            "GET",
            urlsplit(request_url).path + (("?" + urlsplit(request_url).query) if urlsplit(request_url).query else ""),
            headers=headers,
            redirect=False,
            preload_content=False,
            decode_content=False,
        )
        body = _read_body(response)
        headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
        return _TransportResponse(int(response.status), headers, body)
    finally:
        if response is not None:
            response.release_conn()
        pool.close()


def fetch_public_url(url: str, *, allowed_origins: object = ()) -> PublicURLResponse:
    """Fetch one bounded public HTTP(S) resource without ambient proxying.

    ``allowed_origins`` is a host-configured set of complete origins such as
    ``http://127.0.0.1:18080``.  It is the only exception to the public-address
    requirement and is applied independently to every redirect target.
    """

    allowed = _normalise_allowed_origins(allowed_origins)
    current = url
    for redirects in range(MAX_REDIRECTS + 1):
        scheme, hostname, port, request_url, allow_private = _parse_url(current, allowed_origins=allowed)
        addresses = _resolve_public_addresses(hostname, port, allow_private=allow_private)
        response: _TransportResponse | None = None
        last_error: Exception | None = None
        for address in addresses:
            try:
                response = _request_once(
                    scheme=scheme, hostname=hostname, port=port,
                    request_url=request_url, address=address,
                )
                break
            except (OSError, urllib3.exceptions.HTTPError, ssl.SSLError) as error:
                last_error = error
        if response is None:
            raise ConnectionError("public capture request failed") from last_error
        if response.status in _REDIRECT_CODES:
            location = response.headers.get("location")
            if not location:
                raise UnsafePublicURL("redirect response has no Location")
            if any(ord(character) < 0x20 or ord(character) == 0x7F for character in location):
                raise UnsafePublicURL("redirect Location contains a control character")
            current = urljoin(request_url, location)
            continue
        if response.status < 200 or response.status >= 300:
            raise ConnectionError(f"public capture returned HTTP {response.status}")
        content_type = response.headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
        return PublicURLResponse(request_url, content_type or "application/octet-stream", response.body)
    raise UnsafePublicURL("too many redirects")
