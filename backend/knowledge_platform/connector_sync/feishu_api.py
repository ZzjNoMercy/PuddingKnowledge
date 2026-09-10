"""Small, ORM-free Feishu OpenAPI client used by Connector Sync.

The client deliberately owns no credentials other than the access token passed
to its constructor.  Remote responses are treated as untrusted data and all
list methods have bounded pagination.
"""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Protocol
from urllib.parse import urlencode, urlsplit

import httpx


DEFAULT_ENDPOINT = "https://open.feishu.cn"
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{1,4096}$")
_PATH_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,512}$")
_MAX_BINARY_RESPONSE_BYTES = 8 * 1024 * 1024


class FeishuApiError(RuntimeError):
    """Safe error raised for transport, protocol, and Feishu API failures."""

    def __init__(self, message: str, *, status_code: int | None = None, code: Any = None) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes


class AsyncHttpTransport(Protocol):
    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class _HttpxTransport:
    async def request(self, method: str, url: str, *, headers: Mapping[str, str], body: bytes | None,
                      timeout_seconds: float) -> HttpResponse:
        try:
            async with asyncio.timeout(timeout_seconds):
                timeout = httpx.Timeout(timeout_seconds)
                async with httpx.AsyncClient(timeout=timeout, follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, url, headers=dict(headers), content=body) as response:
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes(64 * 1024):
                            total += len(chunk)
                            if total > _MAX_BINARY_RESPONSE_BYTES:
                                raise FeishuApiError("飞书 OpenAPI 响应超过大小上限。", status_code=response.status_code)
                            chunks.append(chunk)
                        return HttpResponse(response.status_code, dict(response.headers), b"".join(chunks))
        except asyncio.TimeoutError as exc:
            raise FeishuApiError("飞书 OpenAPI 请求超时。") from exc
        except FeishuApiError:
            raise
        except httpx.HTTPError as exc:
            raise FeishuApiError("无法连接飞书 OpenAPI。") from exc


# Private compatibility name retained for local OAuth/runtime imports.
_Urllib3Transport = _HttpxTransport


def _endpoint(value: str | None, *, explicit: bool) -> str:
    if value is None:
        return DEFAULT_ENDPOINT
    raw = value.strip().rstrip("/")
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in raw):
        raise ValueError("Feishu endpoint contains control characters")
    parsed = urlsplit(raw)
    if parsed.username or parsed.password or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise ValueError("Feishu endpoint must be an origin without credentials, path, query, or fragment")
    host = (parsed.hostname or "").lower()
    is_loopback = host in {"127.0.0.1", "localhost", "::1"}
    if parsed.scheme == "https" and host == "open.feishu.cn" and parsed.port in (None, 443):
        return "https://open.feishu.cn"
    if explicit and parsed.scheme == "http" and is_loopback:
        return f"http://{parsed.netloc}"
    raise ValueError("Feishu endpoint must be https://open.feishu.cn; HTTP is allowed only for explicit loopback tests")


def _token(value: str) -> str:
    token = str(value or "").strip()
    if not _TOKEN_RE.fullmatch(token):
        raise ValueError("access_token is missing or malformed")
    return token


def _path_token(name: str, value: str) -> str:
    result = str(value or "").strip()
    if not _PATH_TOKEN_RE.fullmatch(result):
        raise ValueError(f"{name} is malformed")
    return result


class FeishuApiClient:
    """Authenticated Wiki, Drive, Docx, and Bitable HTTP client."""

    def __init__(self, access_token: str, *, endpoint: str | None = None,
                 transport: AsyncHttpTransport | None = None, timeout_seconds: float = 20.0,
                 max_pages: int = 10_000, max_response_bytes: int = 8 * 1024 * 1024,
                 max_items: int = 1_000_000) -> None:
        if timeout_seconds <= 0 or timeout_seconds > 300:
            raise ValueError("timeout_seconds must be between 0 and 300")
        if max_pages < 1 or max_pages > 100_000 or max_response_bytes < 1 or max_items < 1:
            raise ValueError("pagination and response limits are invalid")
        self.endpoint = _endpoint(endpoint, explicit=endpoint is not None)
        self.access_token = _token(access_token)
        self.transport = transport or _HttpxTransport()
        self.timeout_seconds = float(timeout_seconds)
        self.max_pages = max_pages
        self.max_response_bytes = max_response_bytes
        self.max_items = max_items

    async def _request(self, method: str, path: str, *, params: Mapping[str, Any] | None = None,
                       json_body: Mapping[str, Any] | None = None) -> dict[str, Any]:
        if not path.startswith("/open-apis/") or "?" in path or "#" in path:
            raise ValueError("invalid Feishu API path")
        query = ""
        if params:
            pairs: list[tuple[str, str]] = []
            for key, value in params.items():
                if value is None or value == "":
                    continue
                if isinstance(value, (list, tuple)):
                    pairs.extend((str(key), str(item)) for item in value)
                else:
                    pairs.append((str(key), str(value)))
            query = "?" + urlencode(pairs) if pairs else ""
        body = json.dumps(json_body, ensure_ascii=False, separators=(",", ":")).encode() if json_body is not None else None
        headers = {"Authorization": f"Bearer {self.access_token}", "Accept": "application/json"}
        if body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        try:
            response = await self.transport.request(method.upper(), self.endpoint + path + query,
                                                     headers=headers, body=body,
                                                     timeout_seconds=self.timeout_seconds)
        except FeishuApiError:
            raise
        except Exception as exc:
            raise FeishuApiError("无法连接飞书 OpenAPI。") from exc
        if len(response.body) > self.max_response_bytes:
            raise FeishuApiError("飞书 OpenAPI 响应超过大小上限。", status_code=response.status_code)
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise FeishuApiError("飞书 OpenAPI 返回了无法解析的响应。", status_code=response.status_code) from exc
        if not isinstance(payload, dict):
            raise FeishuApiError("飞书 OpenAPI 返回格式不正确。", status_code=response.status_code)
        code = payload.get("code")
        if 300 <= response.status_code < 400 or response.status_code >= 400 or code not in (None, 0):
            raise FeishuApiError("飞书 OpenAPI 请求失败。", status_code=response.status_code, code=code)
        return payload

    async def _request_bytes(self, method: str, path: str, *, max_bytes: int) -> tuple[bytes, str, str]:
        if not path.startswith("/open-apis/") or "?" in path or "#" in path:
            raise ValueError("invalid Feishu API path")
        if type(max_bytes) is not int or not 1 <= max_bytes <= _MAX_BINARY_RESPONSE_BYTES:
            raise ValueError("max_bytes exceeds the bounded Feishu binary response limit")
        try:
            response = await asyncio.wait_for(
                self.transport.request(method.upper(), self.endpoint + path,
                                       headers={"Authorization": f"Bearer {self.access_token}", "Accept": "application/octet-stream",
                                                "Accept-Encoding": "identity"},
                                       body=None, timeout_seconds=self.timeout_seconds),
                timeout=self.timeout_seconds * 2 + 1,
            )
        except FeishuApiError:
            raise
        except asyncio.TimeoutError as exc:
            raise FeishuApiError("飞书二进制文件下载超时。") from exc
        except Exception as exc:
            raise FeishuApiError("无法连接飞书 OpenAPI。") from exc
        if response.status_code >= 300:
            raise FeishuApiError("飞书云盘文件下载失败。", status_code=response.status_code)
        headers = {str(k).lower(): str(v) for k, v in response.headers.items()}
        # The transport exposes the decoded body, while Content-Length describes
        # wire bytes for compressed responses. Reject compression so truncation
        # checks remain meaningful and bounded.
        encoding = headers.get("content-encoding", "").strip().lower()
        if encoding and encoding != "identity":
            raise FeishuApiError("飞书文件下载不接受压缩响应。", status_code=response.status_code)
        content_type = headers.get("content-type", "application/octet-stream").split(";", 1)[0].strip().lower()
        if content_type == "application/json" or content_type.endswith("+json"):
            raise FeishuApiError("飞书文件下载返回了 JSON，而不是二进制内容。", status_code=response.status_code)
        if content_type == "application/octet-stream" and response.body.lstrip()[:1] == b"{":
            try:
                maybe_json = json.loads(response.body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                maybe_json = None
            if isinstance(maybe_json, dict) and any(key in maybe_json for key in ("code", "msg", "error", "error_description")):
                raise FeishuApiError("飞书文件下载返回了 JSON，而不是二进制内容。", status_code=response.status_code)
        expected_length = headers.get("content-length")
        if expected_length is not None:
            try:
                declared_length = int(expected_length)
            except (TypeError, ValueError) as exc:
                raise FeishuApiError("飞书文件下载长度无效。", status_code=response.status_code) from exc
            if declared_length < 0 or declared_length != len(response.body):
                raise FeishuApiError("飞书文件下载响应不完整。", status_code=response.status_code)
            if declared_length > max_bytes:
                raise FeishuApiError("飞书文件超过允许的大小上限。", status_code=response.status_code)
        if len(response.body) > max_bytes:
            raise FeishuApiError("飞书云盘文件超过允许的大小上限。", status_code=response.status_code)
        return response.body, headers.get("content-type", "application/octet-stream").split(";", 1)[0], headers.get("content-disposition", "")

    @staticmethod
    def _data(payload: dict[str, Any]) -> dict[str, Any]:
        data = payload.get("data")
        if not isinstance(data, dict):
            raise FeishuApiError("飞书 OpenAPI 返回缺少有效 data。")
        return data

    async def _paginate(self, path: str, *, item_key: str, page_size: int,
                        params: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
        result: list[dict[str, Any]] = []
        total_bytes = 0
        token = ""
        seen: set[str] = set()
        for _ in range(self.max_pages):
            request_params = dict(params or {})
            request_params["page_size"] = page_size
            if token:
                request_params["page_token"] = token
            data = self._data(await self._request("GET", path, params=request_params))
            total_bytes += len(json.dumps(data, ensure_ascii=False).encode("utf-8"))
            if total_bytes > 32 * 1024 * 1024:
                raise FeishuApiError("飞书分页累计响应超过大小上限。")
            values = data.get(item_key)
            if not isinstance(values, list) or not all(isinstance(value, dict) for value in values):
                raise FeishuApiError("飞书 OpenAPI 分页条目格式不正确。")
            result.extend(values)
            if len(result) > self.max_items:
                raise FeishuApiError("飞书分页条目超过安全上限，请缩小同步范围。")
            has_more = data.get("has_more")
            if not isinstance(has_more, bool):
                raise FeishuApiError("飞书 OpenAPI 分页标记格式不正确。")
            if not has_more:
                return result
            next_token = str(data.get("page_token") or data.get("next_page_token") or "")
            if not next_token or len(next_token) > 4096 or next_token in seen:
                raise FeishuApiError("飞书分页游标异常，同步已停止以避免无限循环。")
            seen.add(next_token)
            token = next_token
        raise FeishuApiError("飞书分页数量超过安全上限，请缩小同步范围。")

    async def list_spaces(self) -> list[dict[str, Any]]:
        return await self._paginate("/open-apis/wiki/v2/spaces", item_key="items", page_size=50)

    async def list_nodes(self, *, space_id: str, parent_node_token: str | None = None) -> list[dict[str, Any]]:
        path = f"/open-apis/wiki/v2/spaces/{_path_token('space_id', space_id)}/nodes"
        return await self._paginate(path, item_key="items", page_size=50,
                                    params={"parent_node_token": parent_node_token or None})

    async def list_drive_files(self, *, folder_token: str = "") -> list[dict[str, Any]]:
        return await self._paginate("/open-apis/drive/v1/files", item_key="files", page_size=200,
                                    params={"folder_token": folder_token})

    async def download_drive_file(self, *, file_token: str, max_bytes: int = _MAX_BINARY_RESPONSE_BYTES) -> tuple[bytes, str, str]:
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        return await self._request_bytes("GET", f"/open-apis/drive/v1/files/{_path_token('file_token', file_token)}/download",
                                         max_bytes=max_bytes)

    async def download_media_file(self, *, file_token: str, max_bytes: int = _MAX_BINARY_RESPONSE_BYTES) -> tuple[bytes, str, str]:
        """Download one Docx media file from Feishu's fixed API origin.

        The media token is path-validated and the request never follows a
        redirect, so provider-controlled URLs cannot redirect the bearer
        token to another host.  Callers must enforce any smaller aggregate
        budget across multiple media references themselves.
        """
        if max_bytes < 1:
            raise ValueError("max_bytes must be positive")
        return await self._request_bytes("GET", f"/open-apis/drive/v1/medias/{_path_token('file_token', file_token)}/download",
                                         max_bytes=max_bytes)

    download_media = download_media_file

    async def download_media_assets(
        self,
        *,
        file_tokens: list[str],
        max_bytes_each: int = _MAX_BINARY_RESPONSE_BYTES,
        max_total_bytes: int = _MAX_BINARY_RESPONSE_BYTES,
    ) -> dict[str, tuple[bytes, str, str]]:
        """Download deduplicated media tokens under per-file and total budgets."""
        if type(max_bytes_each) is not int or not 1 <= max_bytes_each <= _MAX_BINARY_RESPONSE_BYTES:
            raise ValueError("max_bytes_each must be positive")
        if type(max_total_bytes) is not int or not 1 <= max_total_bytes <= 32 * 1024 * 1024:
            raise ValueError("max_total_bytes must be positive")
        tokens = list(dict.fromkeys(token for token in file_tokens if token))
        result: dict[str, tuple[bytes, str, str]] = {}
        total = 0
        for token in tokens:
            remaining = max_total_bytes - total
            if remaining <= 0:
                raise FeishuApiError("飞书素材累计大小超过安全上限。")
            payload = await self.download_media_file(
                file_token=token,
                max_bytes=min(max_bytes_each, remaining),
            )
            total += len(payload[0])
            if total > max_total_bytes:
                raise FeishuApiError("飞书素材累计大小超过安全上限。")
            result[token] = payload
        return result

    async def get_node(self, *, node_token: str) -> dict[str, Any]:
        data = self._data(await self._request("GET", "/open-apis/wiki/v2/spaces/get_node",
                                              params={"token": _path_token('node_token', node_token)}))
        return data.get("node") if isinstance(data.get("node"), dict) else data

    async def get_docx_document(self, *, document_id: str) -> dict[str, Any]:
        path = f"/open-apis/docx/v1/documents/{_path_token('document_id', document_id)}"
        data = self._data(await self._request("GET", path))
        return data.get("document") if isinstance(data.get("document"), dict) else data

    async def list_docx_blocks(self, *, document_id: str, document_revision_id: int = -1) -> list[dict[str, Any]]:
        path = f"/open-apis/docx/v1/documents/{_path_token('document_id', document_id)}/blocks"
        return await self._paginate(path, item_key="items", page_size=500,
                                    params={"document_revision_id": document_revision_id})

    async def list_bitable_tables(self, *, app_token: str) -> list[dict[str, Any]]:
        return await self._paginate(f"/open-apis/bitable/v1/apps/{_path_token('app_token', app_token)}/tables",
                                    item_key="items", page_size=100)

    async def list_bitable_fields(self, *, app_token: str, table_id: str) -> list[dict[str, Any]]:
        path = f"/open-apis/bitable/v1/apps/{_path_token('app_token', app_token)}/tables/{_path_token('table_id', table_id)}/fields"
        return await self._paginate(path, item_key="items", page_size=100)

    async def list_bitable_records_page(self, *, app_token: str, table_id: str, view_id: str = "",
                                        page_size: int = 50, page_token: str = "",
                                        field_names: list[str] | None = None) -> dict[str, Any]:
        if type(page_size) is not int or not 1 <= page_size <= 100:
            raise FeishuApiError("Invalid Bitable page size")
        if not isinstance(page_token, str) or len(page_token) > 2000:
            raise FeishuApiError("Invalid Bitable page token")
        params: dict[str, Any] = {"page_size": page_size}
        if view_id:
            params["view_id"] = view_id
        if page_token:
            params["page_token"] = page_token
        if field_names:
            params["field_names"] = json.dumps(list(dict.fromkeys(field_names[:100])), ensure_ascii=False)
        path = f"/open-apis/bitable/v1/apps/{_path_token('app_token', app_token)}/tables/{_path_token('table_id', table_id)}/records"
        data = self._data(await self._request("GET", path, params=params))
        items = data.get("items")
        has_more = data.get("has_more")
        if not isinstance(items, list) or not all(isinstance(item, dict) for item in items) or not isinstance(has_more, bool):
            raise FeishuApiError("飞书 OpenAPI 记录分页格式不正确。")
        next_token = data.get("page_token", "")
        if next_token is None:
            next_token = ""
        if len(items) > page_size or not isinstance(next_token, str) or len(next_token) > 2000 or (has_more and (not next_token or next_token == page_token)):
            raise FeishuApiError("Invalid Bitable continuation")
        return {"items": items, "has_more": has_more, "page_token": next_token,
                "total": data.get("total")}

    async def get_doc_meta(self, *, doc_token: str, doc_type: str = "docx") -> dict[str, Any]:
        try:
            data = self._data(await self._request("POST", "/open-apis/drive/v1/metas/batch_query",
                                                  json_body={"request_docs": [{"doc_token": doc_token, "doc_type": doc_type}], "with_url": True}))
        except FeishuApiError:
            return {}
        metas = data.get("metas")
        return next((m for m in metas if isinstance(m, dict) and str(m.get("doc_token") or "") == doc_token), {}) if isinstance(metas, list) else {}
