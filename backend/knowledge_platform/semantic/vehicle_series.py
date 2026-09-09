"""Deterministic vehicle-series Crosswalk builder for local source snapshots."""

from __future__ import annotations

import json
import re
import unicodedata
from collections import defaultdict
from collections.abc import Mapping, Sequence
from hashlib import sha256

from .crosswalk import CrosswalkCompositionError

_REF_RE = re.compile(r"^[A-Za-z0-9._:-]{1,240}$")
_KEY_RE = re.compile(r"^[^\s/\\]{1,240}$")
_NORMALIZE_RE = re.compile(r"[^0-9a-z\u4e00-\u9fff+]+")


def _normalize(value: object) -> str:
    return _NORMALIZE_RE.sub("", unicodedata.normalize("NFKC", str(value or "")).casefold().strip())


def _entity_key(brand: object, series: object) -> str:
    value = f"{_normalize(brand)}::{_normalize(series)}"
    if not _KEY_RE.fullmatch(value):
        raise CrosswalkCompositionError("vehicle-series entity key is invalid")
    return value


def _rows(rows: Sequence[Mapping[str, object]], *, brand_field: str, series_field: str) -> list[dict[str, str]]:
    output: list[dict[str, str]] = []
    for row in rows:
        if not isinstance(row, Mapping):
            raise CrosswalkCompositionError("vehicle-series source row is invalid")
        brand = str(row.get(brand_field) or "").strip()
        series = str(row.get(series_field) or "").strip()
        if brand and series:
            output.append({brand_field: brand, series_field: series})
    return output


def find_vehicle_series_canonical_collisions(
    canonical_rows: Sequence[Mapping[str, object]],
) -> list[dict[str, object]]:
    """Return deterministic summaries for ambiguous normalized canonical keys."""

    canonical = _rows(canonical_rows, brand_field="brand", series_field="serial_name")
    candidates: dict[str, set[tuple[str, str]]] = defaultdict(set)
    for row in canonical:
        candidates[_entity_key(row["brand"], row["serial_name"])].add((row["brand"], row["serial_name"]))
    collisions: list[dict[str, object]] = []
    for key in sorted(candidates):
        pairs = sorted(candidates[key])
        if len(pairs) < 2:
            continue
        candidate_digests = [
            "sha256:" + sha256(f"{brand}\x00{series}".encode()).hexdigest()
            for brand, series in pairs
        ]
        collisions.append(
            {
                "normalized_key": key,
                "candidate_count": len(pairs),
                "candidate_digests": candidate_digests,
            }
        )
    return collisions


def _candidate_digest(brand: str, series: str) -> str:
    return "sha256:" + sha256(f"{brand}\x00{series}".encode()).hexdigest()


def _collision_policy_digest(policy: Mapping[str, str]) -> str:
    payload = json.dumps(dict(sorted(policy.items())), ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return "sha256:" + sha256(payload.encode("utf-8")).hexdigest()


def build_vehicle_series_collision_policy_template(
    canonical_rows: Sequence[Mapping[str, object]],
) -> dict[str, object]:
    """Create a non-selecting policy template for human review.

    The template contains no raw canonical values. Its selection keys are
    digests of normalized keys, so it is a review artifact rather than an
    executable builder policy. A local resolver must map a reviewed key
    digest back to the observed normalized key before execution.
    """

    collisions = find_vehicle_series_canonical_collisions(canonical_rows)
    return {
        "format": "agent-knowledge-platform-vehicle-series-collision-policy-template/v1",
        "status": "decision-required",
        "selection_contract": (
            "normalized_key_digest -> selected observed candidate sha256; "
            "resolve the key digest locally before execution"
        ),
        "selections": {
            "sha256:" + sha256(str(item["normalized_key"]).encode("utf-8")).hexdigest(): {
                "normalized_key_digest": "sha256:"
                + sha256(str(item["normalized_key"]).encode("utf-8")).hexdigest(),
                "candidate_digests": list(item["candidate_digests"]),
                "selected_candidate_digest": None,
            }
            for item in collisions
        },
    }


def _resolve_canonical_rows(
    canonical_rows: Sequence[Mapping[str, object]],
    *,
    collision_policy: Mapping[str, str] | None,
) -> tuple[list[dict[str, str]], str | None]:
    canonical = _rows(canonical_rows, brand_field="brand", series_field="serial_name")
    collisions = find_vehicle_series_canonical_collisions(canonical)
    if not collisions:
        if collision_policy:
            raise CrosswalkCompositionError("vehicle-series collision policy contains no collision keys")
        return canonical, None
    if collision_policy is None:
        raise CrosswalkCompositionError(
            "canonical normalized collision requires an explicit candidate-digest policy"
        )
    normalized_policy = {str(key): str(value) for key, value in collision_policy.items()}
    collision_keys = {str(item["normalized_key"]) for item in collisions}
    if set(normalized_policy) != collision_keys:
        raise CrosswalkCompositionError("vehicle-series collision policy must resolve every collision and no other key")
    candidates_by_key: dict[str, set[str]] = defaultdict(set)
    for row in canonical:
        candidates_by_key[_entity_key(row["brand"], row["serial_name"])].add(
            _candidate_digest(row["brand"], row["serial_name"])
        )
    for key in sorted(collision_keys):
        selected = normalized_policy[key]
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", selected) or selected not in candidates_by_key[key]:
            raise CrosswalkCompositionError(f"vehicle-series collision policy selects an unknown candidate: {key}")
    resolved = [
        row
        for row in canonical
        if _entity_key(row["brand"], row["serial_name"]) not in collision_keys
        or _candidate_digest(row["brand"], row["serial_name"])
        == normalized_policy[_entity_key(row["brand"], row["serial_name"])]
    ]
    return resolved, _collision_policy_digest(normalized_policy)


def build_vehicle_series_crosswalk(
    *,
    canonical_rows: Sequence[Mapping[str, object]],
    source_rows: Sequence[Mapping[str, object]],
    canonical_source_ref: str,
    source_ref: str,
    canonical_collision_policy: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Build canonical entities from ``vehicle_model_base`` and source diagnostics.

    Matching is normalized exact only.  The canonical database source defines
    the entity universe; source values that do not match remain diagnostics and
    are never promoted by fuzzy inference.
    """

    if not _REF_RE.fullmatch(canonical_source_ref) or not _REF_RE.fullmatch(source_ref):
        raise CrosswalkCompositionError("vehicle-series source reference is invalid")
    canonical, collision_policy_digest = _resolve_canonical_rows(
        canonical_rows,
        collision_policy=canonical_collision_policy,
    )
    source = _rows(source_rows, brand_field="品牌", series_field="1-子车型")
    if not canonical or not source:
        raise CrosswalkCompositionError("vehicle-series source snapshots must not be empty")

    canonical_by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in canonical:
        canonical_by_key[_entity_key(row["brand"], row["serial_name"])].append(row)
    source_by_key: dict[str, list[dict[str, str]]] = defaultdict(list)
    for row in source:
        source_by_key[_entity_key(row["品牌"], row["1-子车型"])].append(row)

    records: list[dict[str, object]] = []
    matched_keys: set[str] = set()
    for key in sorted(canonical_by_key):
        canonical_row = canonical_by_key[key][0]
        matching = source_by_key.get(key, [])
        if matching:
            matched_keys.add(key)
        bindings: list[dict[str, object]] = [
            {
                "source_kind": "database_table",
                "source_ref": canonical_source_ref,
                "canonical": True,
                "table_or_sheet": "vehicle_model_base",
                "key_fields": canonical_row,
            }
        ]
        bindings.extend(
            {
                "source_kind": "table_asset",
                "source_ref": source_ref,
                "canonical": False,
                "table_or_sheet": "",
                "key_fields": row,
            }
            for row in matching
        )
        records.append(
            {
                "record_kind": "canonical_entity",
                "entity": {
                    "entity_key": key,
                    "canonical_brand": canonical_row["brand"],
                    "canonical_series": canonical_row["serial_name"],
                },
                "bindings": bindings,
                "resolution": {
                    "status": "auto_matched" if matching else "canonical_only",
                    "join_eligible": bool(matching),
                    "method": "normalized_exact" if matching else "canonical_baseline",
                    "confidence": 1.0,
                    "candidate_series": [],
                    "evidence": [
                        "vehicle_model_base 定义 canonical vehicle-series entity universe；来源键仅使用 NFKC/大小写/标点归一后的精确匹配。"
                        if matching
                        else "vehicle_model_base 定义 canonical vehicle-series entity universe；当前 bounded 来源未命中。"
                    ],
                },
            }
        )

    diagnostics = [
        {
            "record_kind": "source_diagnostic",
            "entity": None,
            "bindings": [
                {
                    "source_kind": "table_asset",
                    "source_ref": source_ref,
                    "canonical": False,
                    "table_or_sheet": "",
                    "key_fields": row,
                }
            ],
            "resolution": {
                "status": "unmatched",
                "join_eligible": False,
                "method": "normalized_exact_not_found",
                "confidence": 0.0,
                "candidate_series": [],
                "evidence": ["来源键未命中 canonical vehicle_model_base；保留为待审核 diagnostic，不进行模糊推断。"],
            },
        }
        for key in sorted(set(source_by_key) - matched_keys)
        for row in source_by_key[key]
    ]
    matched_source_rows = sum(len(source_by_key[key]) for key in matched_keys)
    return {
        "formatter": "entity-resolution-crosswalk",
        "version": "platform-vehicle-series-v1",
        "entity_type": "vehicle_series",
        "canonical_key": {
            "fields": ["canonical_brand", "canonical_series"],
            "entity_key_template": "normalize(canonical_brand) + '::' + normalize(canonical_series)",
        },
        "source_contract": {
            "canonical": {"source_ref": canonical_source_ref, "key_fields": ["brand", "serial_name"]},
            "source": {"source_ref": source_ref, "key_fields": ["品牌", "1-子车型"]},
        },
        "scope": {"grain": "brand + series", "matching": "normalized_exact", "canonical_source": canonical_source_ref},
        "canonical_collision_policy": {
            "mode": "explicit_candidate_digest" if collision_policy_digest else "none",
            "digest": collision_policy_digest,
        },
        "records": records,
        "source_diagnostics": diagnostics,
        "summary": {
            "canonical_entity_count": len(records),
            "canonical_with_source_binding_count": len(matched_keys),
            "source_distinct_key_count": len(source_by_key),
            "source_matched_key_count": len(matched_keys),
            "source_matched_row_count": matched_source_rows,
            "source_diagnostic_count": len(diagnostics),
        },
    }


__all__ = [
    "build_vehicle_series_collision_policy_template",
    "build_vehicle_series_crosswalk",
    "find_vehicle_series_canonical_collisions",
]
