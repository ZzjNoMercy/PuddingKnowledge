from __future__ import annotations

import json
from pathlib import Path

from scripts.phase8_local_embedding_model_audit import run_audit


def _write_model_dir(path: Path, *, complete: bool = True) -> None:
    path.mkdir()
    (path / "config.json").write_text(json.dumps({"architectures": ["TestEmbeddingModel"], "hidden_size": 3}), encoding="utf-8")
    if complete:
        for name in ("tokenizer.json", "tokenizer_config.json", "vocab.json", "merges.txt", "model.safetensors"):
            (path / name).write_text("local-placeholder", encoding="utf-8")


def test_model_audit_blocks_incomplete_local_artifacts(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _write_model_dir(model_dir, complete=False)

    result = run_audit(model_dir=model_dir, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_EMBEDDING_MODEL_NOT_RUNNABLE"
    assert result["runnable"] is False
    assert "missing_local_model_artifacts" in result["provider_blockers"]
    assert result["network_contacted"] is False
    assert result["model_weights_loaded"] is False


def test_model_audit_accepts_complete_generic_inventory_without_loading_weights(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _write_model_dir(model_dir)

    result = run_audit(model_dir=model_dir, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_EMBEDDING_MODEL_OFFLINE_PROVIDER_READY_NOT_ACTIVATABLE"
    assert result["runnable"] is True
    assert result["model_weights_loaded"] is False
    assert result["hidden_size"] == 3


def test_model_audit_rejects_symlink_model_directory(tmp_path: Path) -> None:
    target = tmp_path / "model-target"
    _write_model_dir(target)
    link = tmp_path / "model-link"
    link.symlink_to(target, target_is_directory=True)

    result = run_audit(model_dir=link, output_dir=tmp_path / "reports")

    assert result["status"] == "PHASE8_LOCAL_EMBEDDING_MODEL_NOT_RUNNABLE"
    assert result["error_type"] == "ValueError"
    assert result["activation_allowed"] is False


def test_model_audit_requires_explicit_runtime_without_generalizing_architecture(tmp_path: Path) -> None:
    model_dir = tmp_path / "model"
    _write_model_dir(model_dir)

    result = run_audit(model_dir=model_dir, output_dir=tmp_path / "reports", required_runtime="vllm")

    assert result["status"] == "PHASE8_LOCAL_EMBEDDING_MODEL_NOT_RUNNABLE"
    assert result["required_runtime"] == "vllm"
    assert result["provider_blockers"] == ["vllm_runtime_required_but_unavailable"]
