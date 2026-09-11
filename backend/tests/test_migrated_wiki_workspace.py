from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive
from knowledge_platform.local.migrated_wiki import (
    MigratedWikiWorkspaceError,
    bootstrap_migrated_wiki,
    load_migrated_wiki_workspace,
)


def _archive(tmp_path: Path) -> Path:
    source = tmp_path / "brain"
    (source / "raw").mkdir(parents=True)
    (source / "wiki" / "nested").mkdir(parents=True)
    (source / "wiki" / "page.md").write_text("# Page\narchive body\n")
    (source / "wiki" / "nested" / "second.md").write_text("# Second\n")
    raw = b"raw history\n"
    (source / "raw" / "source.txt").write_bytes(raw)
    (source / "raw" / "manifest.jsonl").write_text(json.dumps({
        "snapshot_path": "source.txt",
        "sha256": hashlib.sha256(raw).hexdigest(),
        "size_bytes": len(raw),
    }, separators=(",", ":")) + "\n")
    output = tmp_path / "archive"
    prepare_wiki_archive(source, output, installation_id="install-1", source_revision="legacy-1")
    return output


def test_bootstrap_preserves_evidence_and_restarts_without_source(tmp_path: Path) -> None:
    candidate = _archive(tmp_path)
    state = tmp_path / "state"
    result = bootstrap_migrated_wiki(candidate, state)
    assert result["pages"] == 2
    assert result["space_ids"] == [result["space_id"]]
    assert (state / "wiki-evidence" / "archive" / "raw" / "source.txt").read_bytes() == b"raw history\n"
    candidate.rename(tmp_path / "candidate-moved")
    restarted = load_migrated_wiki_workspace(state, json.loads((state / "workspace.json").read_text()))
    assert restarted["file_bindings"]
    assert not (state / ".initializing").exists()


def test_tampered_owned_archive_or_catalog_is_rejected(tmp_path: Path) -> None:
    state = tmp_path / "state"
    bootstrap_migrated_wiki(_archive(tmp_path), state)
    page = next((state / "wiki-evidence" / "archive" / "wiki").rglob("*.md"))
    page.write_text("tampered")
    with pytest.raises(MigratedWikiWorkspaceError):
        load_migrated_wiki_workspace(state, json.loads((state / "workspace.json").read_text()))


def test_unknown_evidence_file_and_partial_marker_fail_closed(tmp_path: Path) -> None:
    candidate = _archive(tmp_path)
    state = tmp_path / "state"
    bootstrap_migrated_wiki(candidate, state)
    (state / "wiki-evidence" / "unknown").write_bytes(b"x")
    with pytest.raises(MigratedWikiWorkspaceError):
        load_migrated_wiki_workspace(state, json.loads((state / "workspace.json").read_text()))

    partial = tmp_path / "partial"
    partial.mkdir()
    (partial / ".initializing").write_bytes(b"incomplete")
    with pytest.raises(MigratedWikiWorkspaceError):
        load_migrated_wiki_workspace(partial, {})


def test_source_archive_links_are_rejected_before_bootstrap(tmp_path: Path) -> None:
    source = tmp_path / "brain"
    (source / "raw").mkdir(parents=True)
    (source / "wiki").mkdir()
    (source / "wiki" / "page.md").write_text("page")
    (source / "raw" / "manifest.jsonl").write_text("{}\n")
    (source / "raw" / "link").symlink_to(source / "wiki" / "page.md")
    with pytest.raises(ValueError):
        prepare_wiki_archive(source, tmp_path / "archive")
