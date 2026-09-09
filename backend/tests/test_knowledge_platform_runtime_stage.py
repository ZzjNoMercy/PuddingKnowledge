from __future__ import annotations

import importlib.util
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
        "knowledge_platform",
        "knowledge_contracts",
    }
    assert (output / "pyproject.toml").read_bytes() == (backend / "pyproject.toml").read_bytes()
    assert (output / "uv.lock").read_bytes() == (backend / "uv.lock").read_bytes()
    assert (output / "pyproject.toml").read_bytes() != (runtime_package / "pyproject.toml").read_bytes()
    assert (output / "uv.lock").read_bytes() != (runtime_package / "uv.lock").read_bytes()

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
