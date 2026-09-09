from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from knowledge_platform.baseline import (
    build_source_snapshot,
    fixture_manifest_digest,
    load_fixture_manifest,
    normalized_digest,
    validate_fixture_manifest,
)


def _baseline_record(capability_id: str, source_revision: str) -> dict[str, object]:
    return {
        "format": "agent-knowledge-platform-golden-baseline-record/v1",
        "capability_id": capability_id,
        "source_revision": source_revision,
        "result": {"answer": "ok"},
        "evidence": {"items": ["evidence-1"]},
        "database_side_effects": {"rows_written": 0},
        "filesystem_side_effects": {"files_written": []},
        "provider_revision": "provider-1",
        "failure_semantics": "invalid_request->stable_error",
        "sanitized_fixture_manifest": {"fixtures": ["fixtures/document.json"]},
    }


def _write_baseline_record(path: Path, capability_id: str, source_revision: str) -> tuple[str, int]:
    record = _baseline_record(capability_id, source_revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest(), path.stat().st_size


def _frozen_document(
    source_digest: str,
    fixture_digest: str,
    fixture_bytes: int,
    source_revision: str,
    baseline_record_file: dict[str, object] | None = None,
) -> dict[str, object]:
    record = _baseline_record("document_rag", source_revision)
    baseline = {
        "source_revision": source_revision,
        "normalized_result_digest": normalized_digest(record["result"]),
        "evidence_digest": normalized_digest(record["evidence"]),
        "database_side_effect_digest": normalized_digest(record["database_side_effects"]),
        "filesystem_side_effect_digest": normalized_digest(record["filesystem_side_effects"]),
        "provider_revision": record["provider_revision"],
        "failure_semantics": record["failure_semantics"],
        "sanitized_fixture_manifest_digest": normalized_digest(record["sanitized_fixture_manifest"]),
    }
    capability: dict[str, object] = {
        "id": "document_rag",
        "fixture_files": [
            {
                "path": "fixtures/document.json",
                "bytes": fixture_bytes,
                "sha256": fixture_digest,
                "sanitized": True,
            }
        ],
        "baseline": baseline,
    }
    if baseline_record_file is not None:
        capability["baseline_record_file"] = baseline_record_file
    return {
        "format": "agent-knowledge-platform-golden-fixture-manifest/v1",
        "spec_revision": "v0.7",
        "status": "frozen",
        "source_snapshot": {"path": "snapshots/source.json", "sha256": source_digest},
        "capabilities": [capability],
    }


def _write_source_snapshot(path: Path) -> tuple[str, str]:
    source_file = path.parent / "source.py"
    test_file = path.parent / "test_source.py"
    source_file.write_text("source = 1\n", encoding="utf-8")
    test_file.write_text("def test_source(): pass\n", encoding="utf-8")

    def record(file_path: Path) -> dict[str, object]:
        return {
            "path": str(file_path.relative_to(path.parents[1])),
            "bytes": file_path.stat().st_size,
            "sha256": hashlib.sha256(file_path.read_bytes()).hexdigest(),
        }

    source_records = [record(source_file)]
    test_records = [record(test_file)]
    source_revision = "sha256:" + hashlib.sha256(
        json.dumps(source_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    test_manifest_digest = "sha256:" + hashlib.sha256(
        json.dumps(test_records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    capability = {
        "capability_id": "document_rag",
        "source_files": source_records,
        "test_files": test_records,
        "source_digest": source_revision,
        "test_manifest_digest": test_manifest_digest,
    }
    document = {
        "format": "agent-knowledge-platform-source-snapshot/v1",
        "observation_only": True,
        "raw_contents_included": False,
        "repository_revision": "a" * 40,
        "worktree_clean": True,
        "snapshot_digest": "sha256:" + hashlib.sha256(
            json.dumps([capability], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
        ).hexdigest(),
        "capabilities": [capability],
    }
    path.write_text(json.dumps(document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return hashlib.sha256(path.read_bytes()).hexdigest(), source_revision


def test_frozen_manifest_validates_files_and_digest_without_raw_content(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    fixture = tmp_path / "fixtures" / "document.json"
    source.parent.mkdir()
    fixture.parent.mkdir()
    source_digest, source_revision = _write_source_snapshot(source)
    fixture.write_text('{"question": "sanitized"}\n', encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    record_digest, record_bytes = _write_baseline_record(tmp_path / "baselines/document.json", "document_rag", source_revision)
    document = _frozen_document(
        source_digest,
        fixture_digest,
        fixture.stat().st_size,
        source_revision,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )

    validation = validate_fixture_manifest(document, repo_root=tmp_path, require_frozen=True)

    assert validation.frozen is True
    assert validation.capability_ids == ("document_rag",)
    assert validation.incomplete_capabilities == ()
    assert fixture_manifest_digest(document).startswith("sha256:")
    assert "question" not in fixture_manifest_digest(document)


def test_capture_required_manifest_is_inventory_but_not_a_frozen_claim() -> None:
    document = {
        "format": "agent-knowledge-platform-golden-fixture-manifest/v1",
        "spec_revision": "v0.7",
        "status": "capture-required",
        "source_snapshot": {"path": "snapshots/source.json", "sha256": "pending"},
        "capabilities": [{"id": "document_rag", "fixture_files": [], "baseline": None}],
    }

    validation = validate_fixture_manifest(document)

    assert validation.frozen is False
    assert validation.incomplete_capabilities == ("document_rag",)
    with pytest.raises(ValueError, match="not complete and frozen"):
        validate_fixture_manifest(document, require_frozen=True)


def test_frozen_validation_requires_repository_evidence_root() -> None:
    document = _frozen_document("a" * 64, "b" * 64, 0, "sha256:" + "c" * 64)

    with pytest.raises(ValueError, match="requires repo_root"):
        validate_fixture_manifest(document, require_frozen=True)


def test_frozen_manifest_requires_at_least_one_sanitized_fixture_per_capability(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    source.parent.mkdir()
    source_digest, source_revision = _write_source_snapshot(source)
    document = _frozen_document(source_digest, "1" * 64, 0, source_revision)
    document["capabilities"][0]["fixture_files"] = []

    with pytest.raises(ValueError, match="is incomplete"):
        validate_fixture_manifest(document, repo_root=tmp_path)


def test_manifest_rejects_path_escape_symlink_and_digest_drift(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    fixture = tmp_path / "fixtures" / "document.json"
    source.parent.mkdir()
    fixture.parent.mkdir()
    source_digest, source_revision = _write_source_snapshot(source)
    fixture.write_text("fixture\n", encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    fixture_digest = hashlib.sha256(fixture.read_bytes()).hexdigest()
    record_digest, record_bytes = _write_baseline_record(tmp_path / "baselines/document.json", "document_rag", source_revision)
    document = _frozen_document(
        source_digest,
        fixture_digest,
        fixture.stat().st_size,
        source_revision,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )
    document["source_snapshot"] = {"path": "../source.json", "sha256": source_digest}
    with pytest.raises(ValueError, match="must not escape"):
        validate_fixture_manifest(document, repo_root=tmp_path)

    document = _frozen_document(source_digest, fixture_digest, fixture.stat().st_size, source_revision)
    document["capabilities"][0]["fixture_files"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="fixture digest drift"):
        validate_fixture_manifest(document, repo_root=tmp_path)

    record_digest, record_bytes = _write_baseline_record(
        tmp_path / "baselines/document.json", "document_rag", "sha256:" + "b" * 64
    )
    document = _frozen_document(
        source_digest,
        fixture_digest,
        fixture.stat().st_size,
        "sha256:" + "b" * 64,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )
    with pytest.raises(ValueError, match="does not match source snapshot"):
        validate_fixture_manifest(document, repo_root=tmp_path)


def test_frozen_manifest_binds_behavior_digests_to_sanitized_record(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    fixture = tmp_path / "fixtures" / "document.json"
    source.parent.mkdir()
    fixture.parent.mkdir()
    _write_source_snapshot(source)
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_revision = json.loads(source.read_text(encoding="utf-8"))["capabilities"][0]["source_digest"]
    fixture.write_text("fixture\n", encoding="utf-8")
    record_digest, record_bytes = _write_baseline_record(tmp_path / "baselines/document.json", "document_rag", source_revision)
    document = _frozen_document(
        source_digest,
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
        fixture.stat().st_size,
        source_revision,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )
    document["capabilities"][0]["baseline"]["normalized_result_digest"] = "sha256:" + "0" * 64

    with pytest.raises(ValueError, match="not derived from baseline_record"):
        validate_fixture_manifest(document, repo_root=tmp_path, require_frozen=True)


def test_frozen_manifest_requires_complete_capability_set(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    fixture = tmp_path / "fixtures" / "document.json"
    source.parent.mkdir()
    fixture.parent.mkdir()
    _write_source_snapshot(source)
    source_document = json.loads(source.read_text(encoding="utf-8"))
    extra = dict(source_document["capabilities"][0])
    extra["capability_id"] = "wiki_query_and_compile"
    source_document["capabilities"].append(extra)
    source_document["snapshot_digest"] = "sha256:" + hashlib.sha256(
        json.dumps(source_document["capabilities"], ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    source.write_text(json.dumps(source_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_revision = source_document["capabilities"][0]["source_digest"]
    fixture.write_text("fixture\n", encoding="utf-8")
    record_digest, record_bytes = _write_baseline_record(tmp_path / "baselines/document.json", "document_rag", source_revision)
    document = _frozen_document(
        source_digest,
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
        fixture.stat().st_size,
        source_revision,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )

    with pytest.raises(ValueError, match="capability set"):
        validate_fixture_manifest(document, repo_root=tmp_path, require_frozen=True)


def test_frozen_manifest_requires_clean_git_provenance(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    fixture = tmp_path / "fixtures" / "document.json"
    source.parent.mkdir()
    fixture.parent.mkdir()
    _write_source_snapshot(source)
    source_document = json.loads(source.read_text(encoding="utf-8"))
    source_document["worktree_clean"] = False
    source.write_text(json.dumps(source_document, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    source_digest = hashlib.sha256(source.read_bytes()).hexdigest()
    source_revision = source_document["capabilities"][0]["source_digest"]
    fixture.write_text("fixture\n", encoding="utf-8")
    record_digest, record_bytes = _write_baseline_record(tmp_path / "baselines/document.json", "document_rag", source_revision)
    document = _frozen_document(
        source_digest,
        hashlib.sha256(fixture.read_bytes()).hexdigest(),
        fixture.stat().st_size,
        source_revision,
        {"path": "baselines/document.json", "bytes": record_bytes, "sha256": record_digest},
    )

    with pytest.raises(ValueError, match="clean Git worktree"):
        validate_fixture_manifest(document, repo_root=tmp_path)


def test_source_snapshot_rejects_duplicate_json_keys(tmp_path: Path) -> None:
    source = tmp_path / "snapshots" / "source.json"
    source.parent.mkdir()
    source.write_text('{"format":"agent-knowledge-platform-source-snapshot/v1","format":"duplicate"}\n', encoding="utf-8")
    document = _frozen_document(
        hashlib.sha256(source.read_bytes()).hexdigest(),
        "b" * 64,
        0,
        "sha256:" + "a" * 64,
    )

    with pytest.raises(ValueError, match="valid JSON"):
        validate_fixture_manifest(document, repo_root=tmp_path)


def test_source_snapshot_rejects_nested_symlink(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    tests_root = tmp_path / "tests"
    source_root.mkdir()
    tests_root.mkdir()
    outside = tmp_path.parent / "outside-source.py"
    outside.write_text("secret = True\n", encoding="utf-8")
    (source_root / "linked.py").symlink_to(outside)
    (tests_root / "test_source.py").write_text("def test_source(): pass\n", encoding="utf-8")
    registry = tmp_path / "golden.yaml"
    registry.write_text(
        "format: agent-knowledge-platform-golden-baseline/v1\n"
        "capabilities:\n"
        "  - id: document_rag\n"
        "    source_surfaces: [source]\n"
        "    test_files: [tests/test_source.py]\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="contains a symlink"):
        build_source_snapshot(tmp_path, registry)


def test_loader_rejects_duplicate_yaml_keys(tmp_path: Path) -> None:
    path = tmp_path / "manifest.yaml"
    path.write_text(
        "format: agent-knowledge-platform-golden-fixture-manifest/v1\n"
        "format: duplicate\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="duplicate YAML key"):
        load_fixture_manifest(path)


def test_checked_in_manifest_is_explicitly_capture_required() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    _document, validation = load_fixture_manifest(
        repo_root / "docs/knowledge-platform/golden-fixture-manifest.yaml",
    )

    assert validation.status == "capture-required"
    assert validation.frozen is False
    assert len(validation.capability_ids) == 9


def test_fixture_manifest_cli_reports_capture_state_and_fails_when_frozen_is_required() -> None:
    repo_root = Path(__file__).resolve().parents[2]
    script = repo_root / "backend/scripts/phase0a_fixture_manifest.py"
    command = [sys.executable, str(script), "--repo-root", str(repo_root)]

    observed = subprocess.run(command, check=True, capture_output=True, text=True)
    report = json.loads(observed.stdout)
    assert report["format"] == "agent-knowledge-platform-golden-fixture-report/v1"
    assert report["status"] == "capture-required"
    assert report["frozen"] is False
    assert report["incomplete_capabilities"] == []
    assert "catalog_migration_rehearsal" not in report["incomplete_capabilities"]

    required = subprocess.run(
        [*command, "--require-frozen"],
        check=False,
        capture_output=True,
        text=True,
    )
    assert required.returncode == 1
    assert "not complete and frozen" in json.loads(required.stdout)["error"]
