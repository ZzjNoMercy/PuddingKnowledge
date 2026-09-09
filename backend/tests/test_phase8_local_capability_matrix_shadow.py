from __future__ import annotations

import importlib.util
import json
from pathlib import Path


def _load_script():
    path = Path(__file__).parents[1] / "scripts" / "phase8_local_capability_matrix_shadow.py"
    spec = importlib.util.spec_from_file_location("phase8_local_capability_matrix_shadow", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_reports(module, root: Path, *, dense_safe: bool = True, missing: str | None = None) -> None:
    root.mkdir()
    for capability, name in module._MATRIX:
        if name == missing:
            continue
        payload = {
            "status": "LOCAL_PASS_NOT_ACTIVATABLE",
            "transport": {"independent_process": True},
            "server_shutdown_clean": True,
            "canonical_catalog_unchanged": True,
        }
        if capability == "document_dense_query":
            payload.update(
                {
                    "candidate_collection_name": "puddingclaw_platform_candidate_text",
                    "catalog_binding_written": not dense_safe,
                    "network_contacted": False,
                    "external_network_contacted": False,
                    "activation_allowed": False,
                }
            )
        (root / name).write_text(json.dumps(payload), encoding="utf-8")


def test_capability_matrix_accepts_all_current_local_process_reports(tmp_path: Path) -> None:
    module = _load_script()
    artifacts = tmp_path / "artifacts"
    _write_reports(module, artifacts)

    result = module.run_shadow(artifact_dir=artifacts, output=tmp_path / "matrix.json")

    assert result["status"] == "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_PASS_NOT_ACTIVATABLE"
    assert result["capability_count"] == 11
    assert result["pass_count"] == 11
    assert result["independent_process_count"] == 11
    assert result["clean_shutdown_count"] == 11
    assert result["canonical_unchanged_count"] == 11
    assert result["entries"][2]["dense_boundary_safe"] is True


def test_capability_matrix_fails_closed_for_missing_or_unsafe_dense_evidence(tmp_path: Path) -> None:
    module = _load_script()
    missing_root = tmp_path / "missing"
    _write_reports(module, missing_root, missing="phase8-local-dense-platform-process-shadow-report.json")
    missing_result = module.run_shadow(artifact_dir=missing_root, output=tmp_path / "missing.json")
    assert missing_result["status"] == "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_BLOCKED"
    assert missing_result["error_type"] == "FileNotFoundError"

    unsafe_root = tmp_path / "unsafe"
    _write_reports(module, unsafe_root, dense_safe=False)
    unsafe_result = module.run_shadow(artifact_dir=unsafe_root, output=tmp_path / "unsafe.json")
    assert unsafe_result["status"] == "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_BLOCKED"
    assert unsafe_result["pass_count"] == 11
    assert unsafe_result["entries"][2]["dense_boundary_safe"] is False


def test_capability_matrix_rejects_symlinked_report_input(tmp_path: Path) -> None:
    module = _load_script()
    artifacts = tmp_path / "artifacts"
    _write_reports(module, artifacts)
    target = artifacts / "real.json"
    target.write_text("{}", encoding="utf-8")
    link = artifacts / "phase8-local-platform-process-shadow-report.json"
    link.unlink()
    link.symlink_to(target)

    result = module.run_shadow(artifact_dir=artifacts, output=tmp_path / "matrix.json")

    assert result["status"] == "PHASE8_LOCAL_CAPABILITY_MATRIX_SHADOW_BLOCKED"
    assert result["error_type"] == "ValueError"
