"""Replay the local gbrain projection through two independent sidecars."""

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

from scripts.phase8_local_platform_process_shadow import _post_json, _stop, _wait_until_ready

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_OUTPUT_DIR = _ROOT / "artifacts/phase0b-local-catalog"
_DEFAULT_CATALOG = _DEFAULT_OUTPUT_DIR / "knowledge-platform.sqlite3"
_DEFAULT_ASSET_ID = "asset_doc_c9b347e318b5402db2026197_73c066b66ad7"
_DEFAULT_SOURCE_FILE = Path(
    "/Users/pet/Documents/knowledge/imported/20260804/Behind the scenes- How we build, test, and scale Google Agent Skills.md"
)
_SPACE_ID = "space_kb_default"
_IDEMPOTENCY_KEY = "phase8-local-gbrain-platform-process"


def _digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return "sha256:" + digest.hexdigest()


def _path_digest(path: Path) -> str:
    return "sha256:" + hashlib.sha256(str(path.expanduser().absolute()).encode()).hexdigest()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _asset(catalog: Path, asset_id: str) -> dict[str, str] | None:
    import sqlite3

    with sqlite3.connect(catalog) as connection:
        connection.row_factory = sqlite3.Row
        row = connection.execute(
            "SELECT id AS asset_id, space_id, source_uri, revision, content_digest "
            "FROM knowledge_assets WHERE id = ?",
            (asset_id,),
        ).fetchone()
    return dict(row) if row is not None else None


def _summary(status_code: int, payload: dict[str, Any]) -> dict[str, Any]:
    summary: dict[str, Any] = {"http_status": status_code, "status": payload.get("status")}
    error = payload.get("error")
    if isinstance(error, dict):
        summary["error"] = {"code": error.get("code")}
        return summary
    data = payload.get("data")
    projection = data.get("projection") if isinstance(data, dict) else None
    if isinstance(projection, dict):
        summary["projection"] = {
            key: projection.get(key)
            for key in (
                "projection_uri",
                "source_uri",
                "published_uri",
                "source_revision",
                "published_digest",
                "bytes",
                "schema_pack",
                "status",
            )
            if key in projection
        }
    return summary


def _manifest_count(root: Path) -> int:
    manifest = root / "gbrain-projection" / "projection-manifest.jsonl"
    if not manifest.is_file():
        return 0
    return len([line for line in manifest.read_text(encoding="utf-8").splitlines() if line.strip()])


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
            "--gbrain-projection-mode",
            "--gbrain-asset-id",
            asset_id,
            "--gbrain-file",
            str(source_file),
        ],
        cwd=str(_ROOT),
        env={"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(_ROOT / "backend")},
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    ready = _wait_until_ready(process, ready_file, port, timeout_seconds=30)
    return process, port, temp_root / "server-data" / "knowledge-platform.sqlite3", ready


def run_shadow(
    *,
    asset_id: str = _DEFAULT_ASSET_ID,
    source_file: Path = _DEFAULT_SOURCE_FILE,
    catalog: Path = _DEFAULT_CATALOG,
    output_dir: Path = _DEFAULT_OUTPUT_DIR,
) -> dict[str, Any]:
    asset_id = asset_id.strip()
    source_file = source_file.expanduser().absolute()
    catalog = catalog.expanduser().absolute()
    output_dir = output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result: dict[str, Any] = {
        "format": "agent-knowledge-platform-phase8-local-gbrain-platform-process-shadow/v1",
        "activation": "not-activated",
        "status": "PHASE8_LOCAL_GBRAIN_PLATFORM_PROCESS_SHADOW_FAILED",
        "transport": {"host": "127.0.0.1", "independent_processes": 2},
        "asset_id": asset_id,
        "source_path_digest": _path_digest(source_file),
        "source_digest": None,
        "server": {"first": None, "second": None},
        "projection": {"first": None, "second": None},
        "restart_replay": False,
        "canonical_catalog_unchanged": None,
    }
    first_process: subprocess.Popen[bytes] | None = None
    second_process: subprocess.Popen[bytes] | None = None
    canonical_before = _digest(catalog)
    try:
        asset = _asset(catalog, asset_id)
        if asset is None or asset["space_id"] != _SPACE_ID or not source_file.is_file():
            raise ValueError("projection Asset or source file is unavailable")
        source_digest = _digest(source_file)
        result["source_digest"] = source_digest
        if source_digest != asset["content_digest"] or source_digest != asset["revision"]:
            raise ValueError("projection source digest does not match Catalog")
        request = {
            "space_id": _SPACE_ID,
            "source_revision": asset["revision"],
            "published_digest": asset["content_digest"],
            "idempotency_key": _IDEMPOTENCY_KEY,
            "schema_pack": "puddingclaw-wiki",
        }
        with tempfile.TemporaryDirectory(
            prefix="phase8-local-gbrain-platform-process-",
            **({"dir": "/private/tmp"} if Path("/private/tmp").is_dir() else {}),
        ) as temp_dir:
            root = Path(temp_dir)
            first_root = root / "first"
            first_process, first_port, first_catalog, first_ready = _start_server(
                catalog=catalog, asset_id=asset_id, source_file=source_file, temp_root=first_root
            )
            first_status, first_payload = _post_json(
                first_port,
                f"/v1/wiki/assets/{asset_id}:project-gbrain",
                request,
                timeout_seconds=30,
            )
            result["server"]["first"] = {
                "capability": first_ready.get("capability"),
                "binding_present": first_ready.get("binding_present"),
                "deployment_revision": first_ready.get("deployment_revision"),
            }
            result["projection"]["first"] = _summary(first_status, first_payload)
            first_manifest_count = _manifest_count(first_root / "server-data")
            result["projection"]["first"]["manifest_entries"] = first_manifest_count
            result["first_server_shutdown_clean"] = _stop(first_process)
            first_process = None

            second_root = root / "second"
            shutil.copytree(
                first_root / "server-data" / "gbrain-projection",
                second_root / "server-data" / "gbrain-projection",
            )
            second_process, second_port, _second_catalog, second_ready = _start_server(
                catalog=first_catalog, asset_id=asset_id, source_file=source_file, temp_root=second_root
            )
            second_status, second_payload = _post_json(
                second_port,
                f"/v1/wiki/assets/{asset_id}:project-gbrain",
                request,
                timeout_seconds=30,
            )
            result["server"]["second"] = {
                "capability": second_ready.get("capability"),
                "binding_present": second_ready.get("binding_present"),
                "deployment_revision": second_ready.get("deployment_revision"),
            }
            result["projection"]["second"] = _summary(second_status, second_payload)
            second_manifest_count = _manifest_count(second_root / "server-data")
            result["projection"]["second"]["manifest_entries"] = second_manifest_count
            result["second_server_shutdown_clean"] = _stop(second_process)
            second_process = None
            first_projection = (first_payload.get("data") or {}).get("projection")
            second_projection = (second_payload.get("data") or {}).get("projection")
            result["restart_replay"] = (
                first_payload.get("status") == "ok"
                and second_payload.get("status") == "ok"
                and isinstance(first_projection, dict)
                and first_projection == second_projection
                and first_manifest_count == second_manifest_count == 1
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
        result.get("restart_replay") is True
        and result.get("first_server_shutdown_clean", True)
        and result.get("second_server_shutdown_clean", True)
        and result["canonical_catalog_unchanged"] is True
        and result["server"]["first"].get("binding_present") is True
        and result["server"]["second"].get("binding_present") is True
        and result["server"]["first"].get("deployment_revision") == "platform-local-process-v1"
        and result["server"]["second"].get("deployment_revision") == "platform-local-process-v1"
    ):
        result["status"] = "PHASE8_LOCAL_GBRAIN_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    report_path = output_dir / "phase8-local-gbrain-platform-process-shadow-report.json"
    report_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    result["report"] = str(report_path)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--asset-id", default=_DEFAULT_ASSET_ID)
    parser.add_argument("--file", dest="source_file", type=Path, default=_DEFAULT_SOURCE_FILE)
    parser.add_argument("--catalog", type=Path, default=_DEFAULT_CATALOG)
    parser.add_argument("--output-dir", type=Path, default=_DEFAULT_OUTPUT_DIR)
    result = run_shadow(**vars(parser.parse_args()))
    print(json.dumps({"status": result["status"], "report": result["report"]}, ensure_ascii=False))
    return 0 if str(result["status"]).endswith("NOT_ACTIVATABLE") else 1


if __name__ == "__main__":
    raise SystemExit(main())
