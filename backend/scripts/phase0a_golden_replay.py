"""Replay captured Golden observers without mutating the baseline registry.

The report distinguishes byte-identical local replay from a frozen Golden
claim.  A dirty worktree, pending fixture capture, or a changed observer must
remain visible; this command never rewrites fixture manifests or baseline
records.
"""

from __future__ import annotations

import hashlib
import importlib
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from knowledge_platform.baseline import capture_baseline_record_document, load_fixture_manifest, normalized_digest
from knowledge_platform.baseline.legacy_runtime_registry import load_legacy_runtime_probe_registry

_OBSERVATION_FIELDS = (
    "result",
    "evidence",
    "database_side_effects",
    "filesystem_side_effects",
    "provider_revision",
    "failure_semantics",
    "sanitized_fixture_manifest",
)


def _resolve_observer(reference: str) -> Callable[[], object]:
    module_name, function_name = reference.split(":", 1)
    observer = getattr(importlib.import_module(module_name), function_name, None)
    if not callable(observer):
        raise ValueError(f"observer is not callable: {reference}")
    return observer


def _capture(observer: Callable[[], object], *, capability_id: str, source_revision: str) -> dict[str, object]:
    observation = observer()
    if not isinstance(observation, Mapping):
        raise ValueError(f"{capability_id}: observer must return a mapping")
    missing = [field for field in _OBSERVATION_FIELDS if field not in observation]
    if missing:
        raise ValueError(f"{capability_id}: observer result is missing fields: {missing}")
    return capture_baseline_record_document(
        capability_id=capability_id,
        source_revision=source_revision,
        result=observation["result"],
        evidence=observation["evidence"],
        database_side_effects=observation["database_side_effects"],
        filesystem_side_effects=observation["filesystem_side_effects"],
        provider_revision=observation["provider_revision"],
        failure_semantics=observation["failure_semantics"],
        sanitized_fixture_manifest=observation["sanitized_fixture_manifest"],
    )


def _safe_record_path(repo_root: Path, raw_path: object, *, capability_id: str) -> Path:
    if not isinstance(raw_path, str) or not raw_path.strip():
        raise ValueError(f"{capability_id}: baseline record path is invalid")
    relative = Path(raw_path)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"{capability_id}: baseline record path is unsafe")
    candidate = repo_root / relative
    target = candidate.resolve()
    if candidate.is_symlink() or not target.is_relative_to(repo_root.resolve()) or not target.is_file():
        raise ValueError(f"{capability_id}: baseline record is missing, escaping, or symlinked")
    return target


def _sha256_file(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _dependency_digest_drifts(observed: object, expected: object) -> list[dict[str, str]]:
    def by_path(value: object) -> dict[str, str]:
        if not isinstance(value, list):
            return {}
        return {
            str(item["path"]): str(item["content_digest"])
            for item in value
            if isinstance(item, Mapping) and isinstance(item.get("path"), str) and isinstance(item.get("content_digest"), str)
        }

    observed_by_path = by_path(observed)
    expected_by_path = by_path(expected)
    return [
        {
            "path": path,
            "observed": observed_by_path.get(path, "missing"),
            "expected": expected_by_path.get(path, "missing"),
        }
        for path in sorted(set(observed_by_path) | set(expected_by_path))
        if observed_by_path.get(path) != expected_by_path.get(path)
    ]


def replay_golden_baselines(
    *,
    repo_root: Path,
    fixture_manifest_path: Path,
    runtime_registry_path: Path,
    output_path: Path | None = None,
) -> dict[str, Any]:
    fixture_manifest_path = fixture_manifest_path.expanduser().absolute()
    runtime_registry_path = runtime_registry_path.expanduser().absolute()
    fixture_manifest, fixture_validation = load_fixture_manifest(fixture_manifest_path, repo_root=repo_root)
    runtime_registry, runtime_validation = load_legacy_runtime_probe_registry(runtime_registry_path, repo_root=repo_root)
    probes_by_capability: dict[str, str] = {}
    for raw_probe in runtime_registry["executed_probes"]:
        if not isinstance(raw_probe, Mapping):
            raise ValueError("legacy runtime registry contains an invalid probe")
        capability_id = str(raw_probe["capability_id"])
        if capability_id in probes_by_capability:
            raise ValueError(f"duplicate observer for capability: {capability_id}")
        probes_by_capability[capability_id] = str(raw_probe["reference"])

    results: list[dict[str, Any]] = []
    for raw_capability in fixture_manifest["capabilities"]:
        if not isinstance(raw_capability, Mapping):
            raise ValueError("Golden fixture manifest contains an invalid capability")
        capability_id = str(raw_capability["id"])
        baseline = raw_capability.get("baseline")
        record_file = raw_capability.get("baseline_record_file")
        if not isinstance(baseline, Mapping) or not isinstance(record_file, Mapping):
            results.append({"capability_id": capability_id, "status": "not_replayable", "reason": "baseline_incomplete"})
            continue
        reference = probes_by_capability.get(capability_id)
        if reference is None:
            results.append({"capability_id": capability_id, "status": "not_replayable", "reason": "observer_missing"})
            continue
        expected_path = _safe_record_path(repo_root, record_file.get("path"), capability_id=capability_id)
        expected = json.loads(expected_path.read_text(encoding="utf-8"))
        observer = _resolve_observer(reference)
        first = _capture(observer, capability_id=capability_id, source_revision=str(baseline["source_revision"]))
        second = _capture(observer, capability_id=capability_id, source_revision=str(baseline["source_revision"]))
        differing_fields = sorted(key for key in set(first) | set(expected) if first.get(key) != expected.get(key))
        differing_digests = {
            field: {
                "observed": normalized_digest(first[field]) if field in first else None,
                "expected": normalized_digest(expected[field]) if field in expected else None,
            }
            for field in differing_fields
        }
        dependency_digest_drifts = _dependency_digest_drifts(
            first.get("evidence", {}).get("implementation_dependencies")
            if isinstance(first.get("evidence"), Mapping)
            else None,
            expected.get("evidence", {}).get("implementation_dependencies")
            if isinstance(expected.get("evidence"), Mapping)
            else None,
        )
        results.append(
            {
                "capability_id": capability_id,
                "status": "matched" if first == second == expected else "mismatch",
                "deterministic": first == second,
                "differing_fields": differing_fields,
                "differing_digests": differing_digests,
                "dependency_digest_drifts": dependency_digest_drifts,
                "expected_record_sha256": _sha256_file(expected_path),
            }
        )

    matched = sum(item["status"] == "matched" for item in results)
    mismatched = sum(item["status"] == "mismatch" for item in results)
    not_replayable = sum(item["status"] == "not_replayable" for item in results)
    replay_status = "matched" if mismatched == 0 and not_replayable == 0 else "mismatch"
    report: dict[str, Any] = {
        "format": "agent-knowledge-platform-golden-replay-report/v1",
        "status": (
            "GOLDEN_REPLAY_MATCHED_NOT_FROZEN"
            if replay_status == "matched" and not fixture_validation.frozen
            else "GOLDEN_REPLAY_MATCHED"
            if replay_status == "matched"
            else "GOLDEN_REPLAY_MISMATCH_NOT_FROZEN"
            if not fixture_validation.frozen
            else "GOLDEN_REPLAY_MISMATCH"
        ),
        "fixture_manifest": {
            "status": fixture_validation.status,
            "frozen": fixture_validation.frozen,
            "capability_count": len(fixture_validation.capability_ids),
        },
        "runtime_registry": {
            "status": runtime_validation.status,
            "reviewed": runtime_validation.reviewed,
            "pending_boundary_review_count": len(runtime_validation.pending_boundary_review_ids),
        },
        "summary": {
            "matched_count": matched,
            "mismatched_count": mismatched,
            "not_replayable_count": not_replayable,
            "freeze_claim_allowed": fixture_validation.frozen and runtime_validation.reviewed and replay_status == "matched",
        },
        "capabilities": results,
    }
    if output_path is not None:
        output_path = output_path.expanduser().absolute()
        if output_path.exists() and output_path.is_symlink():
            raise ValueError("Golden replay output must not be a symlink")
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return report


def main() -> int:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--fixture-manifest", type=Path, default=None)
    parser.add_argument("--runtime-registry", type=Path, default=None)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    repo_root = args.repo_root.resolve()
    report = replay_golden_baselines(
        repo_root=repo_root,
        fixture_manifest_path=args.fixture_manifest or repo_root / "docs/knowledge-platform/golden-fixture-manifest.yaml",
        runtime_registry_path=args.runtime_registry or repo_root / "docs/knowledge-platform/phase-0a-legacy-runtime-call-probes.yaml",
        output_path=args.output,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if report["summary"]["mismatched_count"] == 0 and report["summary"]["not_replayable_count"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
