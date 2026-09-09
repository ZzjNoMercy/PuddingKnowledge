from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from knowledge_platform.distribution.development_source_snapshot import (
    DevelopmentSourceSnapshotError,
    build_development_source_snapshot,
)


def _git(root: Path, *args: str) -> None:
    subprocess.run(("git", *args), cwd=root, check=True, capture_output=True)


def _make_git_repo(root: Path) -> None:
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "snapshot@example.invalid")
    _git(root, "config", "user.name", "Snapshot Test")


def test_development_snapshot_records_dirty_tracked_and_untracked_without_payloads(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "backend/knowledge_platform").mkdir(parents=True)
    (root / "legacy.py").write_text("LEGACY = 1\n", encoding="utf-8")
    (root / "packages/knowledge-platform-web").mkdir(parents=True)
    (root / "backend/knowledge_platform/service.py").write_text("VERSION = 1\n", encoding="utf-8")
    (root / "backend/knowledge_platform/secret.py").write_text("def redact(value): return value\n", encoding="utf-8")
    (root / "backend/knowledge_platform/credential_rehearsal.py").write_text("def rehearse(): return True\n", encoding="utf-8")
    (root / "packages/knowledge-platform-web/package.json").write_text('{"version":"0.1.0"}\n', encoding="utf-8")
    _git(root, "add", "backend/knowledge_platform/service.py", "packages/knowledge-platform-web/package.json", "legacy.py")
    _git(root, "commit", "-qm", "baseline")

    (root / "backend/knowledge_platform/service.py").write_text("VERSION = 2\n", encoding="utf-8")
    (root / "legacy.py").write_text("LEGACY = 2\n", encoding="utf-8")
    (root / "backend/knowledge_platform/new.py").write_text("NEW = True\n", encoding="utf-8")
    (root / "backend/knowledge_platform/.env.production").write_text("TOKEN=do-not-read\n", encoding="utf-8")
    (root / "backend/knowledge_platform/cache.sqlite3").write_bytes(b"sqlite bytes")
    (root / "packages/knowledge-platform-web/node_modules").mkdir()
    (root / "packages/knowledge-platform-web/node_modules/bad.js").write_text("do-not-read\n", encoding="utf-8")

    payload = build_development_source_snapshot(
        repo_root=root,
        selected_paths=("backend/knowledge_platform/**", "packages/knowledge-platform-web/**"),
    )

    assert payload["status"] == "DEVELOPMENT_SOURCE_SNAPSHOT_NOT_A_RELEASE_SOURCE"
    assert payload["source_worktree_clean"] is False
    assert payload["execution"] == {
        "git_commit": False,
        "git_tag": False,
        "git_filter_repo": False,
        "files_copied": False,
    }
    assert [item["path"] for item in payload["dirty_tracked_diff"]] == [
        "backend/knowledge_platform/service.py",
        "legacy.py",
    ]
    assert payload["dirty_tracked_diff"][0]["selected"] is True
    assert payload["dirty_tracked_diff"][1]["selected"] is False
    assert [item["path"] for item in payload["untracked_files"]] == [
        "backend/knowledge_platform/credential_rehearsal.py",
        "backend/knowledge_platform/new.py",
        "backend/knowledge_platform/secret.py",
    ]
    excluded = {item["path"]: item["reason"] for item in payload["excluded_paths"]}
    assert excluded["backend/knowledge_platform/.env.production"] == "secret-or-credential-path"
    assert excluded["backend/knowledge_platform/cache.sqlite3"] == "artifact-or-database-file"
    assert excluded["packages/knowledge-platform-web/node_modules/bad.js"] == "cache-or-environment-directory"
    rendered = json.dumps(payload, ensure_ascii=False)
    assert "do-not-read" not in rendered
    assert len(payload["snapshot_digest"]) == 64


def test_development_snapshot_rejects_empty_selection_and_symlink(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "backend").mkdir()
    (root / "backend/file.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "backend/file.py")
    _git(root, "commit", "-qm", "baseline")

    with pytest.raises(DevelopmentSourceSnapshotError, match="must not be empty"):
        build_development_source_snapshot(repo_root=root, selected_paths=())

    (root / "backend/link.py").symlink_to(root / "backend/file.py")
    payload = build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))
    assert {item["path"] for item in payload["excluded_paths"]} == {"backend/link.py"}


def test_development_snapshot_rejects_symlink_parent_without_following_children(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "backend/real").mkdir(parents=True)
    (root / "backend/real/child.py").write_text("VALUE = 1\n", encoding="utf-8")
    _git(root, "add", "backend/real/child.py")
    _git(root, "commit", "-qm", "baseline")
    (root / "backend/linkdir").symlink_to(root / "backend/real", target_is_directory=True)

    payload = build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))
    assert {item["path"] for item in payload["excluded_paths"]} == {"backend/linkdir"}
    assert not any(item["path"].startswith("backend/linkdir/") for item in payload["selected_files"])


def test_development_snapshot_fails_if_git_state_changes_during_scan(tmp_path: Path, monkeypatch) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    (root / "backend").mkdir()
    (root / "backend/file.py").write_text("x = 1\n", encoding="utf-8")
    _git(root, "add", "backend/file.py")
    _git(root, "commit", "-qm", "baseline")

    import knowledge_platform.distribution.development_source_snapshot as snapshot

    original = snapshot._git_state_token
    calls = 0

    def mutate_state(repo_root: Path):
        nonlocal calls
        calls += 1
        value = original(repo_root)
        return value if calls == 1 else (*value[:3], "mutated")

    monkeypatch.setattr(snapshot, "_git_state_token", mutate_state)
    with pytest.raises(DevelopmentSourceSnapshotError, match="mutated during snapshot"):
        build_development_source_snapshot(repo_root=root, selected_paths=("backend/**",))


def test_development_snapshot_rejects_symlinked_repository_root(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    root.mkdir()
    _make_git_repo(root)
    alias = tmp_path / "repo-alias"
    alias.symlink_to(root, target_is_directory=True)

    with pytest.raises(DevelopmentSourceSnapshotError, match="root must not be a symlink"):
        build_development_source_snapshot(repo_root=alias, selected_paths=("backend/**",))
