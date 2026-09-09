"""Fail-closed validation for sanitized Golden fixture manifests.

The manifest is an evidence index, not a fixture store.  It may contain file
paths and digests, but never raw fixture contents.  A manifest in
``capture-required`` state is useful for inventory, while only a complete
``frozen`` manifest can be accepted by a caller that requests a freeze.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from .normalizer import normalized_digest

_FORMAT = "agent-knowledge-platform-golden-fixture-manifest/v1"
_SPEC_REVISION = "v0.7"
_DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")
_TAGGED_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BASELINE_FIELDS = frozenset(
    {
        "source_revision",
        "normalized_result_digest",
        "evidence_digest",
        "database_side_effect_digest",
        "filesystem_side_effect_digest",
        "provider_revision",
        "failure_semantics",
        "sanitized_fixture_manifest_digest",
    }
)
_BASELINE_RECORD_FIELDS = frozenset(
    {
        "format",
        "capability_id",
        "source_revision",
        "result",
        "evidence",
        "database_side_effects",
        "filesystem_side_effects",
        "provider_revision",
        "failure_semantics",
        "sanitized_fixture_manifest",
    }
)
_HEX_REVISION_RE = re.compile(r"^[0-9a-f]{40}$")


class _UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_unique_mapping(loader: _UniqueKeyLoader, node: yaml.MappingNode, deep: bool = False) -> dict[object, object]:
    mapping: dict[object, object] = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise ValueError(f"duplicate YAML key: {key!r}")
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


_UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG,
    _construct_unique_mapping,
)


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate JSON key: {key!r}")
        result[key] = value
    return result


@dataclass(frozen=True, slots=True)
class FixtureManifestValidation:
    status: str
    capability_ids: tuple[str, ...]
    incomplete_capabilities: tuple[str, ...]

    @property
    def frozen(self) -> bool:
        return self.status == "frozen" and not self.incomplete_capabilities


def _safe_repo_path(raw: object, *, field: str) -> Path:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError(f"{field} must be a non-empty repository-relative path")
    path = Path(raw.strip())
    if path.is_absolute() or ".." in path.parts:
        raise ValueError(f"{field} must not escape the repository: {raw!r}")
    return path


def _require_digest(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or _DIGEST_RE.fullmatch(raw) is None:
        raise ValueError(f"{field} must be a lowercase SHA-256 digest")
    return raw


def _require_tagged_digest(raw: object, *, field: str) -> str:
    if not isinstance(raw, str) or _TAGGED_DIGEST_RE.fullmatch(raw) is None:
        raise ValueError(f"{field} must be a sha256:<64 lowercase hex> digest")
    return raw


def _validate_fixture_files(
    raw_files: object,
    *,
    capability_id: str,
    repo_root: Path | None,
) -> tuple[str, ...]:
    if not isinstance(raw_files, list):
        raise ValueError(f"{capability_id}.fixture_files must be a list")
    seen_paths: set[str] = set()
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping):
            raise ValueError(f"{capability_id}.fixture_files[{index}] must be a mapping")
        relative = _safe_repo_path(raw_file.get("path"), field=f"{capability_id}.fixture_files[{index}].path")
        relative_text = str(relative)
        if relative_text in seen_paths:
            raise ValueError(f"{capability_id} repeats fixture path: {relative_text}")
        seen_paths.add(relative_text)
        if raw_file.get("sanitized") is not True:
            raise ValueError(f"{capability_id}.fixture_files[{index}] must declare sanitized: true")
        byte_count = raw_file.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError(f"{capability_id}.fixture_files[{index}].bytes must be a non-negative integer")
        _require_digest(raw_file.get("sha256"), field=f"{capability_id}.fixture_files[{index}].sha256")
        if repo_root is None:
            continue
        candidate = repo_root / relative
        target = candidate.resolve()
        if candidate.is_symlink() or not target.is_relative_to(repo_root.resolve()) or not target.is_file():
            raise ValueError(f"{capability_id} fixture is missing, escaping, or symlinked: {relative_text}")
        if target.stat().st_size != byte_count:
            raise ValueError(f"{capability_id} fixture byte count drift: {relative_text}")
        actual_digest = hashlib.sha256(target.read_bytes()).hexdigest()
        if actual_digest != raw_file["sha256"]:
            raise ValueError(f"{capability_id} fixture digest drift: {relative_text}")
    return tuple(sorted(seen_paths))


def _validate_source_snapshot_files(
    raw_files: object,
    *,
    capability_id: str,
    kind: str,
    repo_root: Path,
) -> list[dict[str, object]]:
    if not isinstance(raw_files, list) or not raw_files:
        raise ValueError(f"{capability_id}.{kind} must be a non-empty list")
    records: list[dict[str, object]] = []
    seen_paths: set[str] = set()
    root = repo_root.resolve()
    for index, raw_file in enumerate(raw_files):
        if not isinstance(raw_file, Mapping):
            raise ValueError(f"{capability_id}.{kind}[{index}] must be a mapping")
        relative = _safe_repo_path(raw_file.get("path"), field=f"{capability_id}.{kind}[{index}].path")
        relative_text = str(relative)
        if relative_text in seen_paths:
            raise ValueError(f"{capability_id}.{kind} repeats path: {relative_text}")
        seen_paths.add(relative_text)
        byte_count = raw_file.get("bytes")
        if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
            raise ValueError(f"{capability_id}.{kind}[{index}].bytes must be a non-negative integer")
        digest = _require_digest(raw_file.get("sha256"), field=f"{capability_id}.{kind}[{index}].sha256")
        candidate = root / relative
        target = candidate.resolve()
        if candidate.is_symlink() or not target.is_relative_to(root) or not target.is_file():
            raise ValueError(f"{capability_id}.{kind} file is missing, escaping, or symlinked: {relative_text}")
        if target.stat().st_size != byte_count:
            raise ValueError(f"{capability_id}.{kind} byte count drift: {relative_text}")
        if hashlib.sha256(target.read_bytes()).hexdigest() != digest:
            raise ValueError(f"{capability_id}.{kind} digest drift: {relative_text}")
        records.append({"path": relative_text, "bytes": byte_count, "sha256": digest})
    return records


def _snapshot_records_digest(records: list[dict[str, object]]) -> str:
    encoded = json.dumps(records, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


def _reject_unsanitized_keys(value: object, *, field: str) -> None:
    """Reject evidence artifacts that still carry secret-bearing field names."""

    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key).casefold().replace("-", "_")
            if any(
                marker in key_text
                for marker in (
                    "token",
                    "password",
                    "secret",
                    "credential",
                    "api_key",
                    "authorization",
                    "cookie",
                    "private_key",
                    "state",
                    "verifier",
                )
            ):
                raise ValueError(f"{field} contains an unsanitized secret-bearing key")
            _reject_unsanitized_keys(item, field=field)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _reject_unsanitized_keys(item, field=field)


def _validate_baseline_record(
    raw_record: object,
    *,
    capability_id: str,
    baseline: Mapping[object, object],
    fixture_paths: tuple[str, ...],
    repo_root: Path,
) -> None:
    if not isinstance(raw_record, Mapping):
        raise ValueError(f"{capability_id}.baseline_record must be a mapping")
    unknown = set(raw_record) - _BASELINE_RECORD_FIELDS
    missing = _BASELINE_RECORD_FIELDS - set(raw_record)
    if unknown or missing:
        raise ValueError(
            f"{capability_id}.baseline_record fields mismatch: "
            f"unknown={sorted(map(str, unknown))}, missing={sorted(missing)}"
        )
    if raw_record["format"] != "agent-knowledge-platform-golden-baseline-record/v1":
        raise ValueError(f"{capability_id}.baseline_record has an invalid format")
    if raw_record["capability_id"] != capability_id:
        raise ValueError(f"{capability_id}.baseline_record capability binding mismatch")
    _reject_unsanitized_keys(raw_record, field=f"{capability_id}.baseline_record")
    fixture_manifest = raw_record["sanitized_fixture_manifest"]
    if (
        not isinstance(fixture_manifest, Mapping)
        or set(fixture_manifest) != {"fixtures"}
        or fixture_manifest.get("fixtures") != list(fixture_paths)
    ):
        raise ValueError(f"{capability_id}.baseline.sanitized_fixture_manifest is not bound to fixture_files")
    digest_fields = {
        "result": "normalized_result_digest",
        "evidence": "evidence_digest",
        "database_side_effects": "database_side_effect_digest",
        "filesystem_side_effects": "filesystem_side_effect_digest",
        "sanitized_fixture_manifest": "sanitized_fixture_manifest_digest",
    }
    for field, digest_field in digest_fields.items():
        if normalized_digest(raw_record[field]) != baseline[digest_field]:
            raise ValueError(f"{capability_id}.baseline.{field} digest is not derived from baseline_record")
    if raw_record["source_revision"] != baseline["source_revision"]:
        raise ValueError(f"{capability_id}.baseline.source_revision does not match baseline_record")
    if raw_record["provider_revision"] != baseline["provider_revision"]:
        raise ValueError(f"{capability_id}.baseline.provider_revision does not match baseline_record")
    if raw_record["failure_semantics"] != baseline["failure_semantics"]:
        raise ValueError(f"{capability_id}.baseline.failure_semantics does not match baseline_record")
    if not isinstance(raw_record["provider_revision"], str) or not raw_record["provider_revision"].strip():
        raise ValueError(f"{capability_id}.baseline_record.provider_revision must be non-empty")
    if not isinstance(raw_record["failure_semantics"], str) or not raw_record["failure_semantics"].strip():
        raise ValueError(f"{capability_id}.baseline_record.failure_semantics must be non-empty")


def _validate_baseline_record_file(
    raw_file: object,
    *,
    capability_id: str,
    baseline: Mapping[object, object],
    fixture_paths: tuple[str, ...],
    repo_root: Path,
) -> None:
    if not isinstance(raw_file, Mapping):
        raise ValueError(f"{capability_id}.baseline_record_file must be a mapping")
    relative = _safe_repo_path(raw_file.get("path"), field=f"{capability_id}.baseline_record_file.path")
    byte_count = raw_file.get("bytes")
    if not isinstance(byte_count, int) or isinstance(byte_count, bool) or byte_count < 0:
        raise ValueError(f"{capability_id}.baseline_record_file.bytes must be a non-negative integer")
    digest = _require_digest(raw_file.get("sha256"), field=f"{capability_id}.baseline_record_file.sha256")
    candidate = repo_root / relative
    target = candidate.resolve()
    root = repo_root.resolve()
    if candidate.is_symlink() or not target.is_relative_to(root) or not target.is_file():
        raise ValueError(f"{capability_id}.baseline_record_file is missing, escaping, or symlinked")
    if target.stat().st_size != byte_count or hashlib.sha256(target.read_bytes()).hexdigest() != digest:
        raise ValueError(f"{capability_id}.baseline_record_file digest or byte count drift")
    try:
        record = json.loads(target.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError(f"{capability_id}.baseline_record_file is not valid JSON") from exc
    _validate_baseline_record(
        record,
        capability_id=capability_id,
        baseline=baseline,
        fixture_paths=fixture_paths,
        repo_root=repo_root,
    )


def validate_fixture_manifest(
    document: object,
    *,
    repo_root: Path | None = None,
    require_frozen: bool = False,
) -> FixtureManifestValidation:
    if not isinstance(document, Mapping) or document.get("format") != _FORMAT:
        raise ValueError("invalid Golden fixture manifest format")
    if document.get("spec_revision") != _SPEC_REVISION:
        raise ValueError("unsupported Golden fixture manifest spec revision")
    status = document.get("status")
    if status not in {"capture-required", "frozen"}:
        raise ValueError("Golden fixture manifest status must be capture-required or frozen")
    if status == "frozen" and repo_root is None:
        raise ValueError("frozen Golden fixture validation requires repo_root")
    source_snapshot = document.get("source_snapshot")
    if not isinstance(source_snapshot, Mapping):
        raise ValueError("source_snapshot must be a mapping")
    source_relative = _safe_repo_path(source_snapshot.get("path"), field="source_snapshot.path")
    source_digest = source_snapshot.get("sha256")
    if status == "frozen":
        _require_digest(source_digest, field="source_snapshot.sha256")
    elif source_digest != "pending":
        _require_digest(source_digest, field="source_snapshot.sha256")
    source_capability_revisions: dict[str, str] | None = None
    if repo_root is not None and source_digest != "pending":
        source_candidate = repo_root / source_relative
        source_target = source_candidate.resolve()
        if (
            source_candidate.is_symlink()
            or not source_target.is_relative_to(repo_root.resolve())
            or not source_target.is_file()
        ):
            raise ValueError("frozen source snapshot is missing, escaping, or symlinked")
        if hashlib.sha256(source_target.read_bytes()).hexdigest() != source_digest:
            raise ValueError("frozen source snapshot digest drift")
        try:
            source_document = json.loads(source_target.read_text(encoding="utf-8"), object_pairs_hook=_unique_json_object)
        except (OSError, json.JSONDecodeError, ValueError) as exc:
            raise ValueError("source snapshot is not valid JSON") from exc
        if (
            not isinstance(source_document, Mapping)
            or source_document.get("format") != "agent-knowledge-platform-source-snapshot/v1"
            or source_document.get("raw_contents_included") is not False
            or not isinstance(source_document.get("capabilities"), list)
        ):
            raise ValueError("source snapshot has an invalid or unsafe shape")
        if status == "frozen":
            repository_revision = source_document.get("repository_revision")
            if not isinstance(repository_revision, str) or _HEX_REVISION_RE.fullmatch(repository_revision) is None:
                raise ValueError("frozen source snapshot must carry a valid Git revision")
            if source_document.get("worktree_clean") is not True:
                raise ValueError("frozen source snapshot requires a clean Git worktree")
        source_capability_revisions = {}
        normalized_source_capabilities: list[dict[str, object]] = []
        for raw_source_capability in source_document["capabilities"]:
            if not isinstance(raw_source_capability, Mapping):
                raise ValueError("source snapshot capability must be a mapping")
            capability_id = raw_source_capability.get("capability_id")
            revision = raw_source_capability.get("source_digest")
            if (
                not isinstance(capability_id, str)
                or capability_id in source_capability_revisions
                or not isinstance(revision, str)
                or _TAGGED_DIGEST_RE.fullmatch(revision) is None
            ):
                raise ValueError("source snapshot contains an invalid capability source digest")
            # ``capture-required`` is deliberately a target-local fixture
            # contract.  Its source/test paths describe the historical
            # observation and may belong to the mixed legacy checkout, which
            # is not a dependency of the extracted repository.  A frozen
            # manifest still validates every content-addressed source file.
            if status == "frozen":
                source_records = _validate_source_snapshot_files(
                    raw_source_capability.get("source_files"),
                    capability_id=capability_id,
                    kind="source_files",
                    repo_root=repo_root,
                )
                test_records = _validate_source_snapshot_files(
                    raw_source_capability.get("test_files"),
                    capability_id=capability_id,
                    kind="test_files",
                    repo_root=repo_root,
                )
            else:
                source_records = tuple(raw_source_capability.get("source_files") or ())
                test_records = tuple(raw_source_capability.get("test_files") or ())
            if revision != _snapshot_records_digest(source_records):
                raise ValueError(f"{capability_id} source_digest does not match source_files")
            if raw_source_capability.get("test_manifest_digest") != _snapshot_records_digest(test_records):
                raise ValueError(f"{capability_id} test_manifest_digest does not match test_files")
            source_capability_revisions[capability_id] = revision
            normalized_source_capabilities.append(
                {
                    "capability_id": capability_id,
                    "source_files": source_records,
                    "test_files": test_records,
                    "source_digest": revision,
                    "test_manifest_digest": raw_source_capability["test_manifest_digest"],
                }
            )
        expected_snapshot_digest = "sha256:" + hashlib.sha256(
            json.dumps(
                normalized_source_capabilities,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        if source_document.get("snapshot_digest") != expected_snapshot_digest:
            raise ValueError("source snapshot snapshot_digest does not match capability records")

    raw_capabilities = document.get("capabilities")
    if not isinstance(raw_capabilities, list) or not raw_capabilities:
        raise ValueError("Golden fixture manifest must contain capabilities")
    capability_ids: list[str] = []
    incomplete: list[str] = []
    for raw_capability in raw_capabilities:
        if not isinstance(raw_capability, Mapping):
            raise ValueError("Golden fixture capability must be a mapping")
        capability_id = raw_capability.get("id")
        if not isinstance(capability_id, str) or not capability_id.strip() or capability_id in capability_ids:
            raise ValueError(f"invalid or duplicate Golden fixture capability id: {capability_id!r}")
        capability_ids.append(capability_id)
        fixture_paths = _validate_fixture_files(
            raw_capability.get("fixture_files"),
            capability_id=capability_id,
            repo_root=repo_root,
        )
        fixture_file_count = len(raw_capability["fixture_files"])
        baseline = raw_capability.get("baseline")
        complete = isinstance(baseline, Mapping) and _BASELINE_FIELDS.issubset(baseline)
        complete = complete and fixture_file_count > 0
        if isinstance(baseline, Mapping):
            unknown = set(baseline) - _BASELINE_FIELDS
            if unknown:
                raise ValueError(f"{capability_id}.baseline contains unknown fields: {sorted(unknown)}")
            for field in (
                "source_revision",
                "normalized_result_digest",
                "evidence_digest",
                "database_side_effect_digest",
                "filesystem_side_effect_digest",
                "sanitized_fixture_manifest_digest",
            ):
                if field in baseline:
                    _require_tagged_digest(baseline[field], field=f"{capability_id}.baseline.{field}")
            for field in ("provider_revision", "failure_semantics"):
                if field in baseline and (
                    not isinstance(baseline[field], str) or not baseline[field].strip()
                ):
                    raise ValueError(f"{capability_id}.baseline.{field} must be non-empty")
            if "source_revision" in baseline:
                _require_tagged_digest(baseline["source_revision"], field=f"{capability_id}.baseline.source_revision")
        if status == "frozen" and not complete:
            raise ValueError(f"frozen Golden fixture capability is incomplete: {capability_id}")
        if status == "frozen":
            baseline_record_file = raw_capability.get("baseline_record_file")
            if baseline_record_file is None:
                raise ValueError(f"frozen Golden fixture capability lacks baseline_record_file: {capability_id}")
            _validate_baseline_record_file(
                baseline_record_file,
                capability_id=capability_id,
                baseline=baseline,
                fixture_paths=fixture_paths,
                repo_root=repo_root,
            )
        if status == "frozen" and source_capability_revisions is not None:
            expected_revision = source_capability_revisions.get(capability_id)
            if expected_revision is None:
                raise ValueError(f"frozen capability is absent from source snapshot: {capability_id}")
            if baseline["source_revision"] != expected_revision:
                raise ValueError(f"{capability_id}.baseline.source_revision does not match source snapshot")
        if not complete:
            incomplete.append(capability_id)

    if status == "frozen" and source_capability_revisions is not None:
        declared = set(capability_ids)
        expected = set(source_capability_revisions)
        if declared != expected:
            missing = sorted(expected - declared)
            extra = sorted(declared - expected)
            raise ValueError(f"frozen capability set does not match source snapshot: missing={missing}, extra={extra}")

    result = FixtureManifestValidation(status, tuple(capability_ids), tuple(incomplete))
    if require_frozen and not result.frozen:
        raise ValueError("Golden fixture manifest is not complete and frozen")
    return result


def load_fixture_manifest(
    path: Path,
    *,
    repo_root: Path | None = None,
    require_frozen: bool = False,
) -> tuple[dict[object, object], FixtureManifestValidation]:
    document = yaml.load(path.read_text(encoding="utf-8"), Loader=_UniqueKeyLoader)
    validation = validate_fixture_manifest(document, repo_root=repo_root, require_frozen=require_frozen)
    return document, validation


def fixture_manifest_digest(document: Mapping[object, object]) -> str:
    encoded = json.dumps(document, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"
