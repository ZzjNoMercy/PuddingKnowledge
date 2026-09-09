"""Replay Wiki Compile through two independent local Platform processes."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from knowledge_platform.catalog import SqliteCatalogQueryRepository
from scripts.phase8_local_platform_process_shadow import (
    _mcp_http_summary,
    _post_json,
    _query_http_summary,
    _stop,
    _wait_until_ready,
)

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_SPACE_ID = "space_kb_default"
_IDEMPOTENCY_KEY = "phase8-local-platform-process-wiki-compile"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _compile_summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"http_status": status_code, "status": payload.get("status")}
    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {"code": error.get("code")}
        return summary
    data = payload.get("data")
    compilation = data.get("compilation") if isinstance(data, dict) else None
    if isinstance(compilation, dict):
        summary["compilation"] = {
            key: compilation.get(key)
            for key in ("snapshot_id", "source_revision", "resource_uri", "status")
            if key in compilation
        }
    summary["evidence_count"] = len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0
    return summary


def _start_server(
    *, catalog: Path, source_file: Path, asset_id: str, temp_root: Path
) -> tuple[subprocess.Popen[bytes], int, Path, dict[str, Any]]:
    temp_root.mkdir(parents=True, exist_ok=True)
    ready_file = temp_root / "ready.json"
    port = _free_loopback_port()
    process = subprocess.Popen(
        [
            sys.executable,
            str(_ROOT / "backend/scripts/phase8_local_platform_process_server.py"),
            "--catalog",
            str(catalog),
            "--wiki-root",
            str(_ROOT / "docs"),
            "--temp-dir",
            str(temp_root / "server-data"),
            "--port",
            str(port),
            "--ready-file",
            str(ready_file),
            "--wiki-compile-mode",
            "--wiki-compile-asset-id",
            asset_id,
            "--wiki-compile-file",
            str(source_file),
        ],
        cwd=str(_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = _wait_until_ready(process, ready_file, port, timeout_seconds=60)
    return process, port, temp_root / "server-data" / "knowledge-platform.sqlite3", ready


def run_shadow(
    *,
    asset_id: str,
    source_file: Path,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
    query: str = "小布丁",
) -> dict[str, Any]:
    source_file = source_file.expanduser().absolute()
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-wiki-compile-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_WIKI_COMPILE_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_processes": 2},
        "asset_id": asset_id,
        "source_path_digest": _path_digest(source_file),
        "source_digest": None,
        "server": {"first": None, "second": None},
        "compile": {"first": None, "second": None},
        "query_plane": {"first": None, "second": None},
        "restart_replay": False,
        "canonical_catalog_unchanged": None,
    }
    first_process: subprocess.Popen[bytes] | None = None
    second_process: subprocess.Popen[bytes] | None = None
    canonical_before = _digest(catalog)
    try:
        if not asset_id.strip() or not source_file.is_file() or not query.strip():
            raise ValueError("explicit Wiki Asset, source file, and query are required")
        asset = next(
            (item for item in SqliteCatalogQueryRepository(catalog).list_assets(space_id=_SPACE_ID) if item.get("id") == asset_id),
            None,
        )
        if not isinstance(asset, dict) or str(asset.get("kind") or "") != "document":
            raise ValueError("Wiki compile Asset is unavailable")
        result["source_digest"] = _digest(source_file)
        request = {
            "snapshot_id": asset_id,
            "source_revision": str(asset.get("revision") or ""),
            "source_uri": str(asset.get("source_uri") or ""),
            "content_digest": str(asset.get("content_digest") or ""),
            "idempotency_key": _IDEMPOTENCY_KEY,
        }
        with tempfile.TemporaryDirectory(
            prefix="phase8-local-wiki-compile-platform-process-",
            **({"dir": "/private/tmp"} if Path("/private/tmp").is_dir() else {}),
        ) as temp_dir:
            root = Path(temp_dir)
            first_root = root / "first"
            first_process, first_port, first_catalog, first_ready = _start_server(
                catalog=catalog, source_file=source_file, asset_id=asset_id, temp_root=first_root
            )
            compile_status, compile_payload = _post_json(
                first_port,
                f"/v1/wiki/assets/{asset_id}:compile",
                request,
                timeout_seconds=60,
            )
            query_body = {"query": query, "space_id": _SPACE_ID, "limit": 1}
            query_status, query_payload = _post_json(
                first_port, "/v1/wiki/query", query_body, timeout_seconds=60
            )
            mcp_status, mcp_payload = _post_json(
                first_port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "wiki-compile-process-mcp-1",
                    "method": "tools/call",
                    "params": {"name": "wiki_query", "arguments": query_body},
                },
                timeout_seconds=60,
            )
            result["server"]["first"] = {
                "capability": first_ready.get("capability"),
                "binding_present": first_ready.get("binding_present"),
                "deployment_revision": first_ready.get("deployment_revision"),
            }
            result["compile"]["first"] = _compile_summary(compile_status, compile_payload)
            result["query_plane"]["first"] = {
                "rest": _query_http_summary(query_status, query_payload),
                "mcp": _mcp_http_summary(mcp_status, mcp_payload),
            }
            result["first_server_shutdown_clean"] = _stop(first_process)
            first_process = None
            second_root = root / "second"
            shutil.copytree(
                first_root / "server-data" / "wiki-published",
                second_root / "server-data" / "wiki-published",
            )
            second_process, second_port, _second_catalog, second_ready = _start_server(
                catalog=first_catalog, source_file=source_file, asset_id=asset_id, temp_root=second_root
            )
            replay_status, replay_payload = _post_json(
                second_port,
                f"/v1/wiki/assets/{asset_id}:compile",
                request,
                timeout_seconds=60,
            )
            second_query_status, second_query_payload = _post_json(
                second_port, "/v1/wiki/query", query_body, timeout_seconds=60
            )
            second_mcp_status, second_mcp_payload = _post_json(
                second_port,
                "/mcp",
                {
                    "jsonrpc": "2.0",
                    "id": "wiki-compile-process-mcp-2",
                    "method": "tools/call",
                    "params": {"name": "wiki_query", "arguments": query_body},
                },
                timeout_seconds=60,
            )
            result["server"]["second"] = {
                "capability": second_ready.get("capability"),
                "binding_present": second_ready.get("binding_present"),
                "deployment_revision": second_ready.get("deployment_revision"),
            }
            result["compile"]["second"] = _compile_summary(replay_status, replay_payload)
            result["query_plane"]["second"] = {
                "rest": _query_http_summary(second_query_status, second_query_payload),
                "mcp": _mcp_http_summary(second_mcp_status, second_mcp_payload),
            }
            first_compilation = (compile_payload.get("data") or {}).get("compilation", {})
            second_compilation = (replay_payload.get("data") or {}).get("compilation", {})
            result["restart_replay"] = (
                compile_payload.get("status") == "ok"
                and replay_payload.get("status") == "ok"
                and first_compilation.get("resource_uri") == second_compilation.get("resource_uri")
                and first_compilation.get("snapshot_id") == second_compilation.get("snapshot_id") == asset_id
                and result["query_plane"]["first"]["rest"].get("status") == "ok"
                and result["query_plane"]["first"]["mcp"].get("status") == "ok"
                and result["query_plane"]["second"]["rest"].get("status") == "ok"
                and result["query_plane"]["second"]["mcp"].get("status") == "ok"
            )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, subprocess.SubprocessError):
        result["error_type"] = "bounded_local_process_failure"
    finally:
        if first_process is not None:
            result["first_server_shutdown_clean"] = _stop(first_process)
        if second_process is not None:
            result["second_server_shutdown_clean"] = _stop(second_process)
        result["canonical_catalog_unchanged"] = _digest(catalog) == canonical_before
    if (
        result["restart_replay"]
        and result.get("first_server_shutdown_clean", True)
        and result.get("second_server_shutdown_clean", True)
        and result["canonical_catalog_unchanged"] is True
        and result["server"]["first"].get("binding_present") is True
        and result["server"]["second"].get("binding_present") is True
        and result["server"]["first"].get("deployment_revision") == "platform-local-process-v1"
        and result["server"]["second"].get("deployment_revision") == "platform-local-process-v1"
    ):
        result["status"] = "PHASE8_LOCAL_WIKI_COMPILE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-wiki-compile-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", required=True)
    parser.add_argument("--file", dest="source_file", type=Path, required=True)
    parser.add_argument("--query", default="小布丁")
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    args = parser.parse_args()
    result = run_shadow(**vars(args))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
