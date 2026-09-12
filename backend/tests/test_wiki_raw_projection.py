from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from knowledge_platform.distribution.wiki_archive import prepare_wiki_archive, verify_archive
from knowledge_platform.local.wiki_raw import project_raw_assets


def _archive(tmp_path: Path, body: bytes = b"raw body\n", row_extra: dict | None = None) -> Path:
    source = tmp_path / "source"
    (source / "raw").mkdir(parents=True)
    (source / "raw" / "snapshot.txt").write_bytes(body)
    row = {
        "snapshot_path": "snapshot.txt",
        "sha256": hashlib.sha256(body).hexdigest(),
        "size_bytes": len(body),
        "source_id": "legacy-source",
        "asset_id": "legacy-asset",
        "created_at": "2026-09-12T00:00:00Z",
        "bundle_hash": "bundle-digest",
        "title": "A safe title",
        "source_path": "/private/local/source.txt",
        **(row_extra or {}),
    }
    (source / "raw" / "manifest.jsonl").write_text(json.dumps(row) + "\n")
    output = tmp_path / "archive"
    prepare_wiki_archive(source, output, installation_id="projection", source_revision="r1")
    return output


def test_projects_raw_fact_binding_and_portable_metadata(tmp_path: Path) -> None:
    archive = _archive(tmp_path)
    result = project_raw_assets(archive, verify_archive(archive), "space_raw")
    asset_id, asset = next(iter(result["assets"].items()))
    assert asset_id.startswith("asset_raw_")
    assert asset["kind"] == "raw_snapshot"
    assert asset["source_type"] == "local_wiki_raw"
    assert result["file_bindings"][asset_id] == "wiki-evidence/archive/raw/snapshot.txt"
    assert result["titles"][asset_id] == "A safe title"
    assert asset["metadata"] == {
        "snapshot_path": "snapshot.txt",
        "bytes": 9,
        "legacy_source_id": "legacy-source",
        "legacy_asset_id": "legacy-asset",
        "created_at": "2026-09-12T00:00:00Z",
        "bundle_hash": "bundle-digest",
    }
    assert "source_path" not in json.dumps(result)


@pytest.mark.parametrize(
    "row_extra",
    [
        {"source_id": {"not": "portable"}},
        {"asset_id": 42},
        {"created_at": ["bad"]},
        {"bundle_hash": None},
    ],
)
def test_rejects_invalid_optional_types(tmp_path: Path, row_extra: dict) -> None:
    with pytest.raises(ValueError):
        archive = _archive(tmp_path, row_extra=row_extra)
        project_raw_assets(archive, verify_archive(archive), "space_raw")


def test_empty_manifest_is_empty_projection(tmp_path: Path) -> None:
    source = tmp_path / "source"
    (source / "raw").mkdir(parents=True)
    (source / "raw" / "manifest.jsonl").write_text("")
    archive = tmp_path / "archive"
    prepare_wiki_archive(source, archive)
    assert project_raw_assets(archive, verify_archive(archive), "space_raw") == {
        "assets": {},
        "file_bindings": {},
        "titles": {},
    }


@pytest.mark.parametrize("mutation", ["duplicate", "digest", "unknown", "manifest_collision"])
def test_rejects_duplicate_or_unbound_raw_records(tmp_path: Path, mutation: str) -> None:
    body = b"raw body\n"
    archive = _archive(tmp_path)
    manifest = archive / "archive/raw/manifest.jsonl"
    row = json.loads(manifest.read_text())
    if mutation == "duplicate":
        manifest.write_text(json.dumps(row) + "\n" + json.dumps(row) + "\n")
    elif mutation == "digest":
        row["sha256"] = "0" * 64
        manifest.write_text(json.dumps(row) + "\n")
    elif mutation == "unknown":
        row["snapshot_path"] = "missing.txt"
        manifest.write_text(json.dumps(row) + "\n")
    else:
        row["snapshot_path"] = "manifest.jsonl"
        row["sha256"] = hashlib.sha256(manifest.read_bytes()).hexdigest()
        row["size_bytes"] = manifest.stat().st_size
        manifest.write_text(json.dumps(row) + "\n")
    with pytest.raises(ValueError):
        project_raw_assets(archive, verify_archive(archive), "space_raw")


def test_id_is_stable_across_content_revision(tmp_path: Path) -> None:
    first = _archive(tmp_path / "first", b"one\n")
    second = _archive(tmp_path / "second", b"two\n")
    left = project_raw_assets(first, verify_archive(first), "space_raw")
    right = project_raw_assets(second, verify_archive(second), "space_raw")
    left_id = next(iter(left["assets"]))
    right_id = next(iter(right["assets"]))
    assert left_id == right_id
    assert left["assets"][left_id]["content_digest"] != right["assets"][right_id]["content_digest"]
