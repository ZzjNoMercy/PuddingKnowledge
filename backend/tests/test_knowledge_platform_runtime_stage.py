from __future__ import annotations

import importlib.util
import hashlib
import json
from pathlib import Path


def _load_stage_module():
    repo_root = Path(__file__).resolve().parents[2]
    stage_path = repo_root / "packages/knowledge-platform-runtime/stage.py"
    spec = importlib.util.spec_from_file_location("knowledge_platform_runtime_stage", stage_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_stage_uses_backend_metadata_and_flat_owned_module_trees(tmp_path: Path) -> None:
    repo_root = Path(__file__).resolve().parents[2]
    runtime_package = repo_root / "packages/knowledge-platform-runtime"
    backend = repo_root / "backend"
    output = tmp_path / "staged-runtime"

    _load_stage_module().stage(output)

    assert {path.name for path in output.iterdir()} == {
        "pyproject.toml",
        "uv.lock",
        "manifest.json",
        "LICENSE",
        "knowledge_platform",
        "knowledge_contracts",
    }
    assert (output / "pyproject.toml").read_bytes() == (backend / "pyproject.toml").read_bytes()
    assert (output / "uv.lock").read_bytes() == (backend / "uv.lock").read_bytes()
    assert (output / "pyproject.toml").read_bytes() != (runtime_package / "pyproject.toml").read_bytes()
    assert (output / "uv.lock").read_bytes() != (runtime_package / "uv.lock").read_bytes()

    assert (output / "LICENSE").read_bytes() == (backend / "LICENSE").read_bytes()

    for owned_name in ("knowledge_platform", "knowledge_contracts"):
        staged_files = {
            path.relative_to(output / owned_name)
            for path in (output / owned_name).rglob("*")
            if path.is_file()
        }
        source_files = {
            path.relative_to(backend / owned_name)
            for path in (backend / owned_name).rglob("*")
            if path.is_file() and path.suffix not in {".pyc"}
        }
        assert staged_files == source_files


def test_stage_manifest_covers_exact_owned_install_tree(tmp_path: Path) -> None:
    output = tmp_path / "runtime"
    _load_stage_module().stage(output)
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["runtime_kind"] == "local-catalog-wiki"
    assert manifest["production_activation_allowed"] is False
    assert manifest["files"] == {
        str(p.relative_to(output)): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in output.rglob("*") if p.is_file() and p != output / "manifest.json"
    }
