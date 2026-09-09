from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from scripts import phase9_local_distribution_matrix_shadow as matrix


def _fake_shadow():
    command = SimpleNamespace(returncode=0)
    archive = object()
    return SimpleNamespace(
        status="PHASE10_PACKAGE_BUILD_SHADOW_PASS_NOT_ACTIVATABLE",
        replay_consistent=True,
        commands=(command,) * 9,
        archive_observations=(archive,) * 4,
        staged_tree_digest="sha256:" + "a" * 64,
    )


def test_phase9_distribution_matrix_schema_is_valid() -> None:
    schema = json.loads(
        (Path(__file__).resolve().parents[2] / "docs/knowledge-platform/phase9-local-distribution-matrix.schema.json").read_text()
    )
    Draft202012Validator.check_schema(schema)


def test_phase9_distribution_matrix_runs_fail_closed_without_activation(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(matrix, "build_package_shadow", lambda **_kwargs: _fake_shadow())
    output = tmp_path / "matrix.json"
    result = matrix.run_shadow(output_path=output)
    assert result["status"] == matrix._PASS_STATUS
    assert result["check_count"] == 6
    assert result["pass_count"] == 6
    assert result["activation_allowed"] is False
    assert result["release_execution_allowed"] is False
    assert result["source_paths_emitted"] is False
    assert result["files_moved"] is False
    assert result["files_deleted"] is False
    persisted = json.loads(output.read_text())
    assert persisted["status"] == result["status"]
    assert persisted["pass_count"] == 6


def test_phase9_distribution_matrix_rejects_symlink_output(tmp_path: Path) -> None:
    target = tmp_path / "real"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        matrix.run_shadow(output_path=link / "matrix.json")


def test_phase9_distribution_matrix_rejects_package_replay_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    failed = SimpleNamespace(
        status="PHASE10_PACKAGE_BUILD_SHADOW_BLOCKED",
        replay_consistent=False,
        commands=(SimpleNamespace(returncode=1),) * 9,
        archive_observations=(),
        staged_tree_digest="sha256:" + "b" * 64,
    )
    monkeypatch.setattr(matrix, "build_package_shadow", lambda **_kwargs: failed)
    result = matrix.run_shadow(output_path=tmp_path / "matrix.json")
    package = next(item for item in result["checks"] if item["check_id"] == "offline_platform_package_replay")
    assert result["status"] == matrix._BLOCKED_STATUS
    assert package["status"] == "blocked"
    assert package["all_commands_passed"] is False
