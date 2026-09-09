"""Build a human-review-only restore/rebind manifest from a local stage report.

The manifest is an approval queue, not an executor.  It records the source
origin, candidate file evidence, and current stage provenance while refusing
to emit an approval-ready item unless all origin digests for that reference
agree.  No database or filesystem content is copied by this command.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import tempfile
from pathlib import Path
from typing import Any

from scripts.phase0b_local_catalog_copy import (
    _consistent_sqlite_snapshot,
    _physical_reference_origins,
    _reference_exists,
)
from scripts.phase1_local_catalog_shadow import _database_fingerprint, _read_only_connection, _read_only_table_counts

_STAGE_FORMAT = "agent-knowledge-platform-local-catalog-stage/v1"
_REVIEW_FORMAT = "agent-knowledge-platform-restore-review/v1"
_CONFIRMED_STATUS = "unique-content-digest-confirmed-review-required"
_AMBIGUOUS_STATUS = "ambiguous-content-digest-confirmed"
_DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
_DIGEST_FIELDS = {
    "knowledge_documents": "content_sha256",
    "knowledge_import_jobs": "source_sha256",
    "knowledge_table_assets": "content_sha256",
}


def _sha256(path: Path) -> str:
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def _canonical_digest(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return _sha256_bytes(payload.encode("utf-8"))


def _sha256_bytes(value: bytes) -> str:
    return "sha256:" + hashlib.sha256(value).hexdigest()


def _require_dict(value: Any, *, label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{label} must be an object")
    return value


def _digest_origins(origins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    allowed = {"table", "field", "locator", "row_key", "source_content_digest"}
    result: list[dict[str, Any]] = []
    for origin in origins:
        if not isinstance(origin, dict) or set(origin) - allowed:
            raise ValueError("origin evidence contains unsupported fields")
        if not isinstance(origin.get("table"), str) or not isinstance(origin.get("field"), str):
            raise ValueError("origin table/field evidence is invalid")
        row_key = origin.get("row_key")
        if not isinstance(row_key, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in row_key.items()):
            raise ValueError("origin row key evidence is invalid")
        result.append({key: origin[key] for key in allowed if key in origin})
    return result


def _verify_candidates(candidates: list[dict[str, Any]], origins: list[dict[str, Any]]) -> list[dict[str, Any]]:
    digest_origins = [origin for origin in origins if origin.get("source_content_digest")]
    verified: list[dict[str, Any]] = []
    for candidate in candidates:
        if not isinstance(candidate, dict):
            raise ValueError("candidate evidence is invalid")
        path = Path(str(candidate.get("path", ""))).expanduser()
        if path.is_symlink() or not path.is_file():
            raise ValueError(f"candidate file is missing or not a regular file: {path}")
        observed_bytes = path.stat().st_size
        observed_sha256 = _sha256(path)
        declared_sha256 = candidate.get("sha256")
        if declared_sha256 != observed_sha256:
            raise ValueError(f"candidate SHA-256 changed or is inconsistent: {path}")
        matching_origin_count = sum(
            origin.get("source_content_digest") == observed_sha256 for origin in digest_origins
        )
        match = bool(
            digest_origins
            and len(digest_origins) == len(origins)
            and matching_origin_count == len(digest_origins)
        )
        verified.append(
            {
                "path": str(path.resolve()),
                "bytes": observed_bytes,
                "sha256": observed_sha256,
                "content_digest_match": match,
                "matching_origin_count": matching_origin_count,
                "digest_origin_count": len(digest_origins),
            }
        )
    return verified


def _validate_source_origins(source_path: Path, origins: list[dict[str, Any]]) -> None:
    connection = _read_only_connection(source_path)
    try:
        for origin in origins:
            table_name = origin["table"]
            digest_field = _DIGEST_FIELDS.get(table_name)
            if not digest_field:
                if origin.get("source_content_digest"):
                    raise ValueError(f"{table_name}: source digest cannot be verified for this origin")
                continue
            row_key = origin["row_key"]
            columns = {str(row[1]) for row in connection.execute(f'PRAGMA table_info("{table_name}")')}
            if digest_field not in columns or set(row_key) - columns:
                raise ValueError(f"{table_name}: origin columns are not valid")
            if "row_digest" in row_key:
                raise ValueError(f"{table_name}: row digest fallback cannot be used for source verification")
            predicates = " AND ".join(f'"{column}" = ?' for column in row_key)
            row = connection.execute(
                f'SELECT "{digest_field}" FROM "{table_name}" WHERE {predicates}',
                tuple(row_key[column] for column in row_key),
            ).fetchall()
            if len(row) != 1:
                raise ValueError(f"{table_name}: origin row is missing or not unique")
            digest = str(row[0][0] or "")
            digest = digest if digest.startswith("sha256:") else f"sha256:{digest}"
            if not _DIGEST.fullmatch(digest) or digest != origin.get("source_content_digest"):
                raise ValueError(f"{table_name}: source content digest is stale or tampered")
    finally:
        connection.close()


def _validate_stage_missing_set(stage_report: dict[str, Any], source_path: Path, missing: list[dict[str, Any]]) -> None:
    current_origins = _physical_reference_origins(source_path)
    current_missing = {
        reference
        for reference in current_origins
        if not _reference_exists(reference, base_dir=source_path.parent)
    }
    reported_missing = {item["reference"] for item in missing}
    if stage_report.get("source", {}).get("physical_reference_count") != len(current_origins):
        raise ValueError("stage report physical-reference count is stale")
    if reported_missing != current_missing:
        raise ValueError("stage report missing-reference set is stale or incomplete")
    for item in missing:
        if item["origins"] != current_origins[item["reference"]]:
            raise ValueError(f"{item['reference']}: origin evidence is stale or tampered")


def _verification_from_candidates(candidates: list[dict[str, Any]]) -> dict[str, Any]:
    confirmed = sum(bool(candidate["content_digest_match"]) for candidate in candidates)
    if not candidates:
        status = "no-candidate"
    elif confirmed == 1 and len(candidates) == 1:
        status = _CONFIRMED_STATUS
    elif confirmed > 0:
        status = _AMBIGUOUS_STATUS
    else:
        status = "basename-only-unconfirmed"
    return {
        "status": status,
        "candidate_count": len(candidates),
        "content_digest_confirmed_candidate_count": confirmed,
    }


def _validate_candidate_evidence(item: dict[str, Any]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    origins = item.get("origins")
    candidates = item.get("candidates")
    if not isinstance(origins, list) or not origins:
        raise ValueError(f"{item.get('reference')}: origin evidence is missing")
    if not isinstance(candidates, list):
        raise ValueError(f"{item.get('reference')}: candidates must be a list")
    normalized_origins = _digest_origins(origins)
    normalized_candidates = _verify_candidates(candidates, normalized_origins)
    verification = _verification_from_candidates(normalized_candidates)
    claimed = _require_dict(item.get("candidate_verification"), label="candidate_verification")
    if claimed != verification:
        raise ValueError(f"{item.get('reference')}: candidate verification is stale or tampered")
    return normalized_candidates, verification


def _decision(status: str) -> str:
    if status == _CONFIRMED_STATUS:
        return "human-review-required-content-matched-single-candidate"
    if status == _AMBIGUOUS_STATUS:
        return "human-review-required-select-one-candidate"
    if status == "no-candidate":
        return "unresolved-no-candidate"
    return "unresolved-candidate-digest-not-confirmed"


def build_restore_review_manifest(stage_report_path: Path, output_path: Path) -> dict[str, Any]:
    stage_report_path = stage_report_path.expanduser().resolve()
    output_path = output_path.expanduser().resolve()
    if stage_report_path == output_path:
        raise ValueError("review output must not overwrite the stage report")
    stage_report_bytes = stage_report_path.read_bytes()
    stage_report = json.loads(stage_report_bytes)
    stage_report_sha256 = _sha256_bytes(stage_report_bytes)
    if stage_report.get("format") != _STAGE_FORMAT:
        raise ValueError("stage report format is unsupported")
    if stage_report.get("activation") != "not-activated":
        raise ValueError("stage report is not an inactive staging report")
    if stage_report.get("status") not in {"STAGED_LOCAL_DATA", "STAGED_LOCAL_DATA_FILE_REACHABILITY_BLOCKED"}:
        raise ValueError("stage report status is not a staging status")
    generator = _require_dict(stage_report.get("generator"), label="generator")
    generator_script = Path(__file__).with_name("phase0b_local_catalog_copy.py")
    if generator.get("script") != str(generator_script.resolve()) or generator.get("sha256") != _sha256(generator_script):
        raise ValueError("stage report was not generated by the current staging script")
    source = _require_dict(stage_report.get("source"), label="source")
    source_path = Path(str(source.get("path", ""))).expanduser()
    if source_path.is_symlink() or not source_path.is_file():
        raise ValueError("stage source Catalog is missing or not a regular file")
    if source.get("live_files") != _database_fingerprint(source_path) or source.get("table_counts") != _read_only_table_counts(source_path):
        raise ValueError("stage source Catalog does not match its provenance")
    with tempfile.TemporaryDirectory(prefix="restore-review-snapshot-") as temp_dir:
        snapshot_path = Path(temp_dir) / "source.sqlite3"
        _consistent_sqlite_snapshot(source_path, snapshot_path)
        if source.get("snapshot_sha256") != _sha256(snapshot_path):
            raise ValueError("stage source snapshot digest is stale or tampered")
    targets = _require_dict(stage_report.get("targets"), label="targets")
    for role in ("platform", "harness"):
        target = _require_dict(targets.get(role), label=f"targets.{role}")
        target_path = Path(str(target.get("path", ""))).expanduser()
        current_files = _database_fingerprint(target_path)
        main_file = current_files.get(target_path.name, {})
        if (
            target_path.is_symlink()
            or not target_path.is_file()
            or target.get("files") != current_files
            or target.get("bytes") != main_file.get("bytes")
            or target.get("sha256") != main_file.get("sha256")
            or target.get("table_counts") != _read_only_table_counts(target_path)
        ):
            raise ValueError(f"stage target {role} does not match its provenance")
    verification = _require_dict(stage_report.get("physical_reference_verification"), label="physical_reference_verification")
    missing = verification.get("missing")
    if not isinstance(missing, list) or type(verification.get("missing_count")) is not int or verification["missing_count"] != len(missing):
        raise ValueError("stage report missing-reference evidence is inconsistent")
    if len({item.get("reference") for item in missing if isinstance(item, dict)}) != len(missing):
        raise ValueError("stage report contains duplicate or invalid references")
    _validate_stage_missing_set(stage_report, source_path, missing)
    for item in missing:
        if not isinstance(item, dict) or not isinstance(item.get("reference"), str):
            raise ValueError("stage report contains an invalid missing-reference item")
    items = []
    for item in missing:
        origins = _digest_origins(item["origins"])
        _validate_source_origins(source_path, origins)
        candidates, candidate_verification = _validate_candidate_evidence(item)
        if item["candidate_verification"]["status"] != candidate_verification["status"]:
            raise ValueError(f"{item['reference']}: candidate status is stale or tampered")
        evidence = {
            "reference": item["reference"],
            "origins": origins,
            "candidates": candidates,
            "candidate_verification": candidate_verification,
        }
        items.append(
            {
                "review_id": _canonical_digest({"stage_report_sha256": stage_report_sha256, **evidence}),
                "decision": _decision(candidate_verification["status"]),
                **evidence,
            }
        )
    items.sort(key=lambda item: item["reference"])
    decision_counts: dict[str, int] = {}
    for item in items:
        decision_counts[item["decision"]] = decision_counts.get(item["decision"], 0) + 1
    result = {
        "format": _REVIEW_FORMAT,
        "status": "REVIEW_REQUIRED",
        "activation": "not-activated",
        "scope": "local development stage; human review only; no restore/rebind executor",
        "stage_report": {
            "path": str(stage_report_path),
            "sha256": stage_report_sha256,
            "status": stage_report["status"],
        },
        "source": {
            key: source[key]
            for key in ("path", "snapshot_sha256", "live_files", "table_counts", "physical_reference_count")
            if key in source
        },
        "targets": {
            role: {
                key: targets[role][key]
                for key in ("path", "bytes", "sha256", "files", "table_counts")
                if key in targets[role]
            }
            for role in ("platform", "harness")
        },
        "summary": {
            "item_count": len(items),
            "decision_counts": dict(sorted(decision_counts.items())),
            "policy": (
                "content digest match is evidence only; approval must confirm source ownership, permissions, "
                "file type, and intended stable binding before any restore/rebind"
            ),
        },
        "items": items,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stage-report", type=Path, default=Path("artifacts/phase0b-local-catalog/local-catalog-stage-report.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/phase0b-local-catalog/restore-rebind-review.json")
    )
    args = parser.parse_args()
    result = build_restore_review_manifest(args.stage_report, args.output)
    print(json.dumps({"status": result["status"], "summary": result["summary"], "report": str(args.output)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
