"""Replay local Web Capture Processing through two independent sidecars."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from scripts.phase8_local_platform_process_shadow import _post_json, _stop, _wait_until_ready

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_CAPTURE_ID = "web_capture_later_d46aa7bd1c3f4c7c8842abda"
_DEFAULT_SOURCE_FILE = Path(
    "/Users/pet/Documents/knowledge/imported/20260804/Behind the scenes- How we build, test, and scale Google Agent Skills.md"
)
_SPACE_ID = "space_kb_default"
_IDEMPOTENCY_KEY = "phase8-local-capture-platform-process"


def _digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _capture(catalog: Path, capture_id: str) -> dict[str, str] | None:
    with sqlite3.connect(catalog) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT w.id AS capture_id, w.space_id, w.asset_id, w.content_digest, "
            "a.revision, a.source_uri FROM knowledge_web_captures AS w "
            "JOIN knowledge_assets AS a ON a.id = w.asset_id "
            "WHERE w.id = ? AND w.parse_status = 'ready'",
            (capture_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"http_status": status_code, "status": payload.get("status")}
    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {"code": error.get("code")}
        return summary
    data = payload.get("data")
    capture = data.get("capture") if isinstance(data, dict) else None
    if isinstance(capture, dict):
        summary["capture"] = {
            key: capture.get(key)
            for key in ("asset_id", "source_revision", "resource_uri", "status")
            if key in capture
        }
    return summary


def _start_server(
    *, catalog: Path, asset_id: str, source_file: Path, temp_root: Path
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
            "--capture-processing-mode",
            "--capture-asset-id",
            asset_id,
            "--capture-file",
            str(source_file),
        ],
        cwd=str(_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = _wait_until_ready(process, ready_file, port, timeout_seconds=30)
    return process, port, temp_root / "server-data" / "knowledge-platform.sqlite3", ready


def _job_state(catalog: Path) -> dict[str, Any]:
    job_id = "capture_process_" + hashlib.sha256(_IDEMPOTENCY_KEY.encode()).hexdigest()[:48]
    with sqlite3.connect(catalog) as connection:
        row = connection.execute(
            "SELECT status, current_step, progress, input_uri, input_reference_digest, attempt "
            "FROM knowledge_processing_jobs WHERE id = ? AND kind = 'read_later_capture'",
            (job_id,),
        ).fetchone()
    if row is None:
        return {}
    return {
        "status": row[0],
        "current_step": row[1],
        "progress": row[2],
        "resource_uri": row[3],
        "idempotency_digest": row[4],
        "attempt": row[5],
    }


def run_shadow(
    *,
    capture_id: str = _DEFAULT_CAPTURE_ID,
    source_file: Path = _DEFAULT_SOURCE_FILE,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    capture_id = capture_id.strip()
    source_file = source_file.expanduser().absolute()
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-capture-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_CAPTURE_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_processes": 2},
        "capture_id": capture_id,
        "asset_id": None,
        "source_path_digest": _path_digest(source_file),
        "source_digest": None,
        "server": {"first": None, "second": None},
        "processing": {"first": None, "second": None},
        "restart_replay": False,
        "canonical_catalog_unchanged": None,
    }
    first_process: subprocess.Popen[bytes] | None = None
    second_process: subprocess.Popen[bytes] | None = None
    canonical_before = _digest(catalog)
    try:
        asset = _capture(catalog, capture_id)
        if asset is None or not source_file.is_file():
            raise ValueError("ready Capture Asset or source file is unavailable")
        result["asset_id"] = asset["asset_id"]
        result["source_digest"] = _digest(source_file)
        if result["source_digest"] != asset["content_digest"]:
            raise ValueError("Capture source digest does not match Catalog")
        request = {
            "source_revision": asset["revision"],
            "source_uri": asset["source_uri"],
            "content_digest": asset["content_digest"],
            "idempotency_key": _IDEMPOTENCY_KEY,
        }
        with tempfile.TemporaryDirectory(
            prefix="phase8-local-capture-platform-process-",
            **({"dir": "/private/tmp"} if Path("/private/tmp").is_dir() else {}),
        ) as temp_dir:
            root = Path(temp_dir)
            first_root = root / "first"
            first_process, first_port, first_catalog, first_ready = _start_server(
                catalog=catalog,
                asset_id=asset["asset_id"],
                source_file=source_file,
                temp_root=first_root,
            )
            first_status, first_payload = _post_json(
                first_port,
                f"/v1/captures/assets/{asset['asset_id']}:process",
                request,
                timeout_seconds=30,
            )
            result["server"]["first"] = {
                "capability": first_ready.get("capability"),
                "binding_present": first_ready.get("binding_present"),
                "deployment_revision": first_ready.get("deployment_revision"),
            }
            result["processing"]["first"] = _summary(first_status, first_payload)
            first_state = _job_state(first_catalog)
            result["processing"]["first"]["durable_state"] = {
                key: first_state.get(key)
                for key in ("status", "current_step", "progress", "resource_uri", "attempt")
                if key in first_state
            }
            result["first_server_shutdown_clean"] = _stop(first_process)
            first_process = None
            second_root = root / "second"
            shutil.copytree(
                first_root / "server-data" / "capture-published",
                second_root / "server-data" / "capture-published",
            )
            second_process, second_port, second_catalog, second_ready = _start_server(
                catalog=first_catalog,
                asset_id=asset["asset_id"],
                source_file=source_file,
                temp_root=second_root,
            )
            second_status, second_payload = _post_json(
                second_port,
                f"/v1/captures/assets/{asset['asset_id']}:process",
                request,
                timeout_seconds=30,
            )
            result["server"]["second"] = {
                "capability": second_ready.get("capability"),
                "binding_present": second_ready.get("binding_present"),
                "deployment_revision": second_ready.get("deployment_revision"),
            }
            result["processing"]["second"] = _summary(second_status, second_payload)
            second_state = _job_state(second_catalog)
            result["processing"]["second"]["durable_state"] = {
                key: second_state.get(key)
                for key in ("status", "current_step", "progress", "resource_uri", "attempt")
                if key in second_state
            }
            result["second_server_shutdown_clean"] = _stop(second_process)
            second_process = None
            first_uri = (first_payload.get("data") or {}).get("capture", {}).get("resource_uri")
            second_uri = (second_payload.get("data") or {}).get("capture", {}).get("resource_uri")
            result["restart_replay"] = (
                first_payload.get("status") == "ok"
                and second_payload.get("status") == "ok"
                and first_uri == second_uri == second_state.get("resource_uri")
                and first_state.get("status") == "succeeded"
                and second_state.get("status") == "succeeded"
                and first_state.get("attempt") == second_state.get("attempt") == 1
            )
    except (OSError, RuntimeError, TypeError, ValueError, KeyError, sqlite3.Error, subprocess.SubprocessError):
        result["error_type"] = "bounded_local_process_failure"
    finally:
        if first_process is not None:
            result["first_server_shutdown_clean"] = _stop(first_process)
        if second_process is not None:
            result["second_server_shutdown_clean"] = _stop(second_process)
        result["canonical_catalog_unchanged"] = _digest(catalog) == canonical_before
    if (
        result.get("restart_replay") is True
        and result.get("first_server_shutdown_clean", True)
        and result.get("second_server_shutdown_clean", True)
        and result["canonical_catalog_unchanged"] is True
        and result["server"]["first"].get("binding_present") is True
        and result["server"]["second"].get("binding_present") is True
        and result["server"]["first"].get("deployment_revision") == "platform-local-process-v1"
        and result["server"]["second"].get("deployment_revision") == "platform-local-process-v1"
    ):
        result["status"] = "PHASE8_LOCAL_CAPTURE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-capture-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--capture-id", default=_DEFAULT_CAPTURE_ID)
    parser.add_argument("--file", dest="source_file", type=Path, default=_DEFAULT_SOURCE_FILE)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    result = run_shadow(**vars(parser.parse_args()))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
