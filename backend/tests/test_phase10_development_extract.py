from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.distribution.development_source_snapshot import build_development_source_snapshot
from scripts.phase10_development_extract import (
    DevelopmentExtractionError,
    apply_development_extraction,
    build_development_extraction_plan,
)


def _git(root: Path, *args: str) -> str:
    result = subprocess.run(("git", *args), cwd=root, check=True, capture_output=True, text=True)
    return result.stdout.strip()


def test_development_extraction_plan_is_explicit_and_does_not_create_output(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "backend/knowledge_platform").mkdir(parents=True)
    (root / "backend/knowledge_platform/new.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "backend/knowledge_platform/new.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "backend/knowledge_platform/extra.py").write_text("VALUE = 3\n", encoding="utf-8")
    snapshot = build_development_source_snapshot(repo_root=root, selected_paths=("backend/knowledge_platform/**",))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    output = tmp_path / "development-clone"
    plan = build_development_extraction_plan(source_root=root, snapshot_path=snapshot_path, target="puddingknowledge")

    assert plan["status"] == "DEVELOPMENT_EXTRACTION_PLAN_ONLY"
    assert plan["selected_file_count"] == 2
    assert plan["release_boundary"]["signed_tag_created"] is False
    assert plan["root_file_mapping"]["LICENSE"]["source"] == "LICENSE"
    assert plan["root_file_mapping"]["pyproject.toml"]["action"] == "reconcile-and-relocate"
    assert not output.exists()


def test_development_extraction_plan_rejects_source_mutation(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "backend").mkdir()
    (root / "backend/file.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    snapshot = build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    (root / "backend/file.py").write_text("VALUE = 2\n", encoding="utf-8")

    with pytest.raises(DevelopmentExtractionError, match="digest changed"):
        build_development_extraction_plan(source_root=root, snapshot_path=snapshot_path, target="puddingharness")


def test_development_extraction_applies_to_existing_private_clone_and_commits_deletions(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "backend/knowledge_platform").mkdir(parents=True)
    (root / "backend/knowledge_platform/new.py").write_text("VALUE = 1\n", encoding="utf-8")
    (root / "backend/knowledge_platform/delete.py").write_text("DELETE = True\n", encoding="utf-8")
    (root / ".gitignore").write_text("backend/knowledge_platform/extra.py\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    (root / "backend/knowledge_platform/new.py").write_text("VALUE = 2\n", encoding="utf-8")
    (root / "backend/knowledge_platform/extra.py").write_text("VALUE = 3\n", encoding="utf-8")
    (root / "backend/knowledge_platform/delete.py").unlink()
    snapshot = build_development_source_snapshot(repo_root=root, selected_paths=("backend/knowledge_platform/**",))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    clone = tmp_path / "clone"
    _git(root.parent, "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(root), str(clone))
    source_head = _git(root, "rev-parse", "HEAD")
    result = apply_development_extraction(
        source_root=root,
        snapshot_path=snapshot_path,
        output=clone,
        target="puddingknowledge",
        branch="codex/test-development-seed",
        existing_clone=True,
    )

    assert result["status"] == "DEVELOPMENT_EXTRACTION_COMMITTED_IN_TEMP_CLONE"
    assert result["commit"] != source_head
    assert (clone / "backend/knowledge_platform/new.py").read_text(encoding="utf-8") == "VALUE = 2\n"
    assert (clone / "backend/knowledge_platform/extra.py").read_text(encoding="utf-8") == "VALUE = 3\n"
    assert not (clone / "backend/knowledge_platform/delete.py").exists()
    assert (clone / "DEVELOPMENT_EXTRACTION_ROOT_MAPPING.json").is_file()
    assert _git(clone, "status", "--porcelain") == ""
    assert _git(root, "rev-parse", "HEAD") == source_head


def test_development_extraction_rejects_symlink_output_ancestor(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "backend").mkdir()
    (root / "backend/file.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    snapshot = build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")
    real_parent = tmp_path / "real-output-parent"
    real_parent.mkdir()
    alias_parent = tmp_path / "alias-output-parent"
    alias_parent.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(DevelopmentExtractionError, match="symlink ancestor"):
        apply_development_extraction(
            source_root=root,
            snapshot_path=snapshot_path,
            output=alias_parent / "clone",
            target="puddingharness",
            branch="codex/test-symlink-output",
        )


def test_development_extraction_rejects_shared_or_worktree_clone(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")
    (root / "backend").mkdir()
    (root / "backend/file.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", ".")
    _git(root, "commit", "-qm", "baseline")
    snapshot = build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))
    snapshot_path = tmp_path / "snapshot.json"
    snapshot_path.write_text(json.dumps(snapshot), encoding="utf-8")

    with pytest.raises(DevelopmentExtractionError, match="source checkout"):
        apply_development_extraction(
            source_root=root,
            snapshot_path=snapshot_path,
            output=root,
            target="puddingharness",
            branch="codex/test-source-output",
            existing_clone=True,
        )

    clone = tmp_path / "clone"
    _git(root.parent, "clone", "--no-local", "--no-hardlinks", "--no-checkout", str(root), str(clone))
    alternates = clone / ".git/objects/info/alternates"
    alternates.parent.mkdir(parents=True, exist_ok=True)
    alternates.write_text("/untrusted/shared/objects\n", encoding="utf-8")
    with pytest.raises(DevelopmentExtractionError, match="alternates"):
        apply_development_extraction(
            source_root=root,
            snapshot_path=snapshot_path,
            output=clone,
            target="puddingharness",
            branch="codex/test-alternates",
            existing_clone=True,
        )
