from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

from scripts.phase8_local_platform_process_server import (
    _validate_dense_loopback_uri,
    _validate_dense_manifest_path,
)


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_dense_platform_process_shadow.py"
    spec = importlib.util.spec_from_file_location("phase8_local_dense_platform_process_shadow", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Process:
    def poll(self):
        return None


def _ok_payload(*, mcp: bool = False) -> dict[str, object]:
    content = {
        "status": "ok",
        "data": {"count": 1},
        "evidence": [
            {
                "asset_id": "asset-a",
                "resource_uri": "knowledge://spaces/space_kb_default/assets/asset-a",
            }
        ],
    }
    if mcp:
        return {"jsonrpc": "2.0", "id": "x", "result": {"structuredContent": content}}
    return content


def test_dense_process_shadow_uses_independent_dense_server_and_keeps_catalog_unchanged(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    started: list[list[str]] = []

    def fake_popen(command, **_kwargs):
        started.append(command)
        return _Process()

    monkeypatch.setattr(module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(module, "_free_loopback_port", lambda: 43123)
    monkeypatch.setattr(
        module,
        "_wait_until_ready",
        lambda *_args, **_kwargs: {
            "status": "ready",
            "capability": "document_rag_query",
            "provider_id": "puddingclaw_platform_candidate_text",
            "binding_present": True,
            "deployment_revision": "platform-local-process-v1",
        },
    )

    def fake_post(_port, path, payload, **_kwargs):
        if path == "/mcp":
            return 200, _ok_payload(mcp=True)
        return 200, _ok_payload()

    monkeypatch.setattr(module, "_post_json", fake_post)
    monkeypatch.setattr(module, "_stop", lambda _process: True)

    result = module.run_shadow(
        catalog=catalog,
        manifest=tmp_path / "manifest.json",
        model_dir=tmp_path / "model",
        output_dir=tmp_path / "report",
    )

    assert result["status"] == "PHASE8_LOCAL_DENSE_PLATFORM_PROCESS_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["transport"]["independent_process"] is True
    assert result["canonical_catalog_unchanged"] is True
    assert result["catalog_binding_written"] is False
    command = started[0]
    assert "--dense-manifest" in command
    assert "--embedding-model-dir" in command
    assert "--embedding-dimension" in command
    assert "--embedding-max-length" in command
    report = json.loads((tmp_path / "report" / "phase8-local-dense-platform-process-shadow-report.json").read_text())
    assert "knowledge platform" not in json.dumps(report)


def test_dense_process_shadow_rejects_invalid_limit_before_starting_child(tmp_path: Path, monkeypatch) -> None:
    module = _load_script()
    catalog = tmp_path / "catalog.sqlite3"
    catalog.write_bytes(b"catalog")
    monkeypatch.setattr(module.subprocess, "Popen", lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError()))

    result = module.run_shadow(catalog=catalog, output_dir=tmp_path / "report", limit=0)

    assert result["status"] == "PHASE8_LOCAL_DENSE_PLATFORM_PROCESS_SHADOW_FAILED"
    assert result["error_type"] == "ValueError"


def test_dense_process_server_rejects_non_loopback_uri_and_symlink_manifest(tmp_path: Path) -> None:
    assert _validate_dense_loopback_uri("http://127.0.0.1:19530") == "http://127.0.0.1:19530"
    for uri in ("https://example.test:19530", "http://127.0.0.1:19530?secret=1", "http://user@127.0.0.1:19530"):
        with pytest.raises(ValueError):
            _validate_dense_loopback_uri(uri)

    target = tmp_path / "manifest.json"
    target.write_text("{}", encoding="utf-8")
    link = tmp_path / "manifest-link.json"
    link.symlink_to(target)
    with pytest.raises(ValueError):
        _validate_dense_manifest_path(link)
