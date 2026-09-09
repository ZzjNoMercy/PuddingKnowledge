from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from scripts.phase8_local_platform_process_server import _configure_console_cors, _validate_console_origin
from scripts.phase8_local_platform_process_shadow import _mcp_http_summary, _query_http_summary


def test_process_shadow_query_summary_drops_query_and_content() -> None:
    summary = _query_http_summary(
        200,
        {
            "status": "ok",
            "data": {"query": "private", "count": 1, "limit": 1},
            "evidence": [{"asset_id": "asset_1", "quote": "private excerpt"}],
        },
    )

    assert summary["status"] == "ok"
    assert summary["http_status"] == 200
    assert summary["data"]["count"] == 1
    assert "private" not in str(summary)


def test_process_shadow_mcp_summary_keeps_only_bounded_result_shape() -> None:
    summary = _mcp_http_summary(
        200,
        {
            "jsonrpc": "2.0",
            "id": "x",
            "result": {
                "structuredContent": {
                    "status": "ok",
                    "data": {"count": 1, "query": "private"},
                    "evidence": [],
                }
            },
        },
    )

    assert summary["status"] == "ok"
    assert summary["data"]["count"] == 1
    assert "private" not in str(summary)


def test_process_server_console_cors_is_explicit_loopback_only() -> None:
    assert _validate_console_origin("http://127.0.0.1:4173/") == "http://127.0.0.1:4173"
    assert _validate_console_origin("http://localhost:4173") == "http://localhost:4173"

    for origin in (
        "https://127.0.0.1:4173",
        "http://example.test:4173",
        "http://127.0.0.1:4173/path",
        "http://127.0.0.1:4173?redirect=1",
    ):
        with pytest.raises(ValueError):
            _validate_console_origin(origin)

    app = FastAPI()
    app.add_api_route("/health", lambda: {"status": "ok"})
    _configure_console_cors(app, "http://127.0.0.1:4173")
    with TestClient(app) as client:
        allowed = client.get("/health", headers={"Origin": "http://127.0.0.1:4173"})
        denied = client.get("/health", headers={"Origin": "http://127.0.0.1:4174"})
    assert allowed.headers["access-control-allow-origin"] == "http://127.0.0.1:4173"
    assert "access-control-allow-origin" not in denied.headers
