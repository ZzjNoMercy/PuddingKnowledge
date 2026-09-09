"""Deterministic, sanitized Golden baseline summaries.

The module deliberately stores digests rather than raw snapshots.  A caller
must supply an already bounded result/evidence/side-effect observation; this
module makes its representation stable and removes common secret and host
path forms before hashing.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import PurePath
from urllib.parse import urlsplit

_SECRET_KEY_MARKERS = frozenset(
    {
        "api_key",
        "authorization",
        "cookie",
        "credential",
        "password",
        "private_key",
        "secret",
        "state",
        "token",
        "verifier",
    }
)
_WINDOWS_PATH_RE = re.compile(r"^(?:[A-Za-z]:[\\/]|\\\\)")


def _key_is_secret(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SECRET_KEY_MARKERS or any(
        marker in normalized for marker in ("token", "password", "secret", "credential", "api_key")
    )


def _string_is_url(value: str) -> bool:
    return urlsplit(value).scheme in {"http", "https", "file", "ftp"}


def _string_is_path(value: str) -> bool:
    return value.startswith(("/", "~/", "./", "../")) or _WINDOWS_PATH_RE.match(value) is not None


def normalize_for_baseline(value: object, *, _key: str = "") -> object:
    """Return JSON-compatible data with secrets and host-specific references removed."""

    if _key and _key_is_secret(_key):
        return "<redacted>"
    if isinstance(value, Mapping):
        return {
            str(key): normalize_for_baseline(item, _key=str(key))
            for key, item in sorted(value.items(), key=lambda entry: str(entry[0]))
        }
    if isinstance(value, (list, tuple)):
        return [normalize_for_baseline(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted(normalize_for_baseline(item) for item in value)
    if isinstance(value, str):
        if _string_is_url(value):
            return "<url-redacted>"
        if _string_is_path(value):
            return "<path-redacted>"
        if any(ord(character) < 32 and character not in "\t\n\r" for character in value):
            return "<control-char-redacted>"
        return value
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def normalized_digest(value: object) -> str:
    normalized = normalize_for_baseline(value)
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(encoded).hexdigest()}"


@dataclass(frozen=True, slots=True)
class GoldenBaselineRecord:
    capability_id: str
    source_revision: str
    normalized_result_digest: str
    evidence_digest: str
    database_side_effect_digest: str
    filesystem_side_effect_digest: str
    provider_revision: str
    failure_semantics: str
    sanitized_fixture_manifest_digest: str

    def __post_init__(self) -> None:
        for field_name in (
            "capability_id",
            "source_revision",
            "normalized_result_digest",
            "evidence_digest",
            "database_side_effect_digest",
            "filesystem_side_effect_digest",
            "provider_revision",
            "failure_semantics",
            "sanitized_fixture_manifest_digest",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"GoldenBaselineRecord.{field_name} must not be empty")

    def to_dict(self) -> dict[str, str]:
        return asdict(self)


def capture_baseline(
    *,
    capability_id: str,
    source_revision: str,
    result: object,
    evidence: object,
    database_side_effects: object,
    filesystem_side_effects: object,
    provider_revision: str,
    failure_semantics: str,
    sanitized_fixture_manifest: object,
) -> GoldenBaselineRecord:
    return GoldenBaselineRecord(
        capability_id=capability_id,
        source_revision=source_revision,
        normalized_result_digest=normalized_digest(result),
        evidence_digest=normalized_digest(evidence),
        database_side_effect_digest=normalized_digest(database_side_effects),
        filesystem_side_effect_digest=normalized_digest(filesystem_side_effects),
        provider_revision=provider_revision,
        failure_semantics=failure_semantics,
        sanitized_fixture_manifest_digest=normalized_digest(sanitized_fixture_manifest),
    )


def _assert_record_keys_are_sanitized(value: object, *, field: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_text = str(key)
            if _key_is_secret(key_text):
                raise ValueError(f"{field} contains a secret-bearing key; sanitize the observation schema first")
            _assert_record_keys_are_sanitized(item, field=field)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            _assert_record_keys_are_sanitized(item, field=field)


def capture_baseline_record_document(
    *,
    capability_id: str,
    source_revision: str,
    result: object,
    evidence: object,
    database_side_effects: object,
    filesystem_side_effects: object,
    provider_revision: str,
    failure_semantics: str,
    sanitized_fixture_manifest: object,
) -> dict[str, object]:
    """Build a validator-compatible, normalized Golden record document.

    The returned observation fields are normalized before they are retained;
    their digests are then calculated from those exact retained values. This
    prevents a caller from recording a digest for one representation and a
    different raw representation in the evidence file.
    """

    for field, value in {
        "capability_id": capability_id,
        "source_revision": source_revision,
        "provider_revision": provider_revision,
        "failure_semantics": failure_semantics,
    }.items():
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")

    if not isinstance(sanitized_fixture_manifest, Mapping):
        raise ValueError("sanitized_fixture_manifest must be a mapping")
    raw_fixture_paths = sanitized_fixture_manifest.get("fixtures")
    if not isinstance(raw_fixture_paths, list) or any(
        not isinstance(path, str)
        or not path.strip()
        or PurePath(path).is_absolute()
        or ".." in PurePath(path).parts
        for path in raw_fixture_paths
    ):
        raise ValueError("sanitized_fixture_manifest fixtures must be repository-relative paths")

    observations = {
        "result": normalize_for_baseline(result),
        "evidence": normalize_for_baseline(evidence),
        "database_side_effects": normalize_for_baseline(database_side_effects),
        "filesystem_side_effects": normalize_for_baseline(filesystem_side_effects),
        "sanitized_fixture_manifest": normalize_for_baseline(sanitized_fixture_manifest),
    }
    for field, value in observations.items():
        _assert_record_keys_are_sanitized(value, field=field)
    if not isinstance(observations["sanitized_fixture_manifest"], Mapping):
        raise ValueError("sanitized_fixture_manifest must be a mapping")
    fixture_manifest = observations["sanitized_fixture_manifest"]
    if set(fixture_manifest) != {"fixtures"} or not isinstance(fixture_manifest["fixtures"], list):
        raise ValueError("sanitized_fixture_manifest must be {fixtures: [...]} after normalization")
    fixture_paths = fixture_manifest["fixtures"]
    if any(
        not isinstance(path, str)
        or not path.strip()
        or PurePath(path).is_absolute()
        or ".." in PurePath(path).parts
        for path in fixture_paths
    ):
        raise ValueError("sanitized_fixture_manifest fixtures must be repository-relative paths")
    fixture_manifest = {"fixtures": sorted(fixture_paths)}
    observations["sanitized_fixture_manifest"] = fixture_manifest

    capture_baseline(
        capability_id=capability_id,
        source_revision=source_revision,
        result=observations["result"],
        evidence=observations["evidence"],
        database_side_effects=observations["database_side_effects"],
        filesystem_side_effects=observations["filesystem_side_effects"],
        provider_revision=provider_revision,
        failure_semantics=failure_semantics,
        sanitized_fixture_manifest=fixture_manifest,
    )
    return {
        "format": "agent-knowledge-platform-golden-baseline-record/v1",
        "capability_id": capability_id,
        "source_revision": source_revision,
        **observations,
        "provider_revision": provider_revision,
        "failure_semantics": failure_semantics,
    }


def compare_baseline(
    expected: GoldenBaselineRecord,
    observed: GoldenBaselineRecord,
) -> tuple[str, ...]:
    """Return field names that differ; equality is the only pass condition."""

    return tuple(
        field_name
        for field_name in expected.__dataclass_fields__
        if getattr(expected, field_name) != getattr(observed, field_name)
    )
