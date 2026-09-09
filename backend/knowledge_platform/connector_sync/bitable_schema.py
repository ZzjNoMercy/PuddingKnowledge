"""Pure Bitable schema and relationship validation helpers.

This module deliberately has no connector, ORM, or network dependency.  It
models the fields needed to plan joins; row values are never part of the
normalised schema.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Iterator
from dataclasses import dataclass


_VALUE_KEYS = {
    "value", "values", "rows", "records", "record", "samples", "sample",
    "sample_value", "sample_values", "example", "examples", "row_values",
}


@dataclass(frozen=True)
class NormalizedSchema:
    """Canonical schema document and its content revision.

    Mapping-style access is retained for callers that historically consumed a
    plain dict (``schema["fields"]``, ``schema["digest"]``).
    """

    document: dict[str, Any]
    revision: str

    def __getitem__(self, key: str) -> Any:
        if key == "digest":
            return self.revision
        return self.document[key]

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default

    def __contains__(self, key: object) -> bool:
        return key == "digest" or key in self.document

    def __iter__(self) -> Iterator[str]:
        return iter((*self.document.keys(), "digest"))

    def __len__(self) -> int:
        return len(self.document) + 1


def _text(value: Any, label: str, *, required: bool = True) -> str:
    if not isinstance(value, str):
        if required:
            raise ValueError(f"{label} must be a non-empty string")
        return ""
    result = value.strip()
    if required and not result:
        raise ValueError(f"{label} must be a non-empty string")
    return result


def _without_values(value: Any, *, key: str = "") -> Any:
    """Copy metadata while dropping known row/sample value-bearing keys."""
    if key.lower() in _VALUE_KEYS:
        return None
    if isinstance(value, Mapping):
        return {
            str(k): _without_values(v, key=str(k))
            for k, v in value.items()
            if str(k).lower() not in _VALUE_KEYS
        }
    if isinstance(value, (list, tuple)):
        return [_without_values(item) for item in value]
    return value


def _field(field: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(field, Mapping):
        raise ValueError("each Bitable field must be an object")
    field_id = _text(field.get("field_id", field.get("id")), "field_id")
    name = field.get("field_name", field.get("name", field_id))
    _text(name, "field_name")
    field_name = name  # Provider field names are exact, including whitespace.
    field_type = field.get("type", field.get("ui_type"))
    if type(field_type) is int:
        if not 1 <= field_type <= 100000:
            raise ValueError("field type is invalid")
    else:
        field_type = _text(field_type, f"field {field_id} type")
    prop = field.get("property", {})
    if not isinstance(prop, Mapping):
        raise ValueError(f"field {field_id} property must be an object")

    # Keep schema metadata, including provider-specific keys, but make the
    # identity keys canonical and remove values which could be row samples.
    result = _without_values({key: field[key] for key in ("ui_type", "is_primary", "description") if key in field})
    if not isinstance(result, dict):  # defensive; dict input makes this moot
        result = {}
    result["field_id"] = field_id
    result["field_name"] = field_name
    result["type"] = field_type
    result["property"] = _without_values(dict(prop))
    return result


def normalize_schema(
    app_token: str,
    table_id: str,
    table_name: str,
    view_id: str | None,
    fields: Sequence[Mapping[str, Any]],
) -> NormalizedSchema:
    """Validate and canonicalise a table schema, returning a stable digest.

    Field order is intentionally canonicalised by ``field_id``.  The digest
    therefore changes when a field's name, type, or property changes, while
    order-only changes do not affect it.
    """
    app_token = _text(app_token, "app_token")
    table_id = _text(table_id, "table_id")
    table_name = _text(table_name, "table_name")
    view_id = _text(view_id or "", "view_id", required=False)
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        raise ValueError("fields must be a list of field objects")
    normalized = [_field(item) for item in fields]
    ids = [item["field_id"] for item in normalized]
    if len(ids) != len(set(ids)):
        raise ValueError("field_id values must be unique")
    normalized.sort(key=lambda item: item["field_id"])
    payload = {
        "app_token": app_token,
        "table_id": table_id,
        "table_name": table_name,
        "view_id": view_id,
        "fields": normalized,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    revision = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    return NormalizedSchema(document=payload, revision=revision)


def _catalog(schema_map: Mapping[str, Any]) -> dict[str, Any]:
    """Accept normalized schemas keyed by table id, with a small list fallback."""
    if not isinstance(schema_map, Mapping):
        raise ValueError("schema_map must be a mapping of table_id to schema")
    return {str(key): value for key, value in schema_map.items()}


def _find_field(schema: Any, field_id: str) -> Mapping[str, Any] | None:
    if isinstance(schema, NormalizedSchema):
        schema = schema.document
    if not isinstance(schema, Mapping):
        return None
    fields = schema.get("fields")
    if not isinstance(fields, Sequence) or isinstance(fields, (str, bytes)):
        return None
    return next((f for f in fields if isinstance(f, Mapping) and str(f.get("field_id") or "") == field_id), None)


def validate_relations(
    configured: Sequence[Mapping[str, Any]],
    schema_map: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate explicit relation endpoints against known schemas.

    The result includes one status per relation and aggregate warnings.  A
    schema can identify a primary field, but this function never claims that
    its *values* are unique.
    """
    if not isinstance(configured, Sequence) or isinstance(configured, (str, bytes)):
        raise ValueError("configured relations must be a list")
    catalog = _catalog(schema_map)
    results: list[dict[str, Any]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    seen_ids: set[str] = set()
    for relation in configured:
        if not isinstance(relation, Mapping):
            raise ValueError("each relation must be an object")
        rid = _text(relation.get("id"), "relation id")
        if rid in seen_ids:
            raise ValueError(f"duplicate relation id: {rid}")
        seen_ids.add(rid)
        cardinality = _text(relation.get("cardinality"), f"relation {rid} cardinality")
        if cardinality not in {"one_to_one", "one_to_many", "many_to_one", "many_to_many"}:
            raise ValueError(f"relation {rid} has invalid cardinality")
        source = (_text(relation.get("source_table_id"), "source_table_id"), _text(relation.get("source_field_id"), "source_field_id"))
        target = (_text(relation.get("target_table_id"), "target_table_id"), _text(relation.get("target_field_id"), "target_field_id"))
        if source == target:
            raise ValueError(f"relation {rid} cannot join a field to itself")
        identity = frozenset((source, target))
        if identity in seen:
            raise ValueError(f"duplicate or reverse relation: {rid}")
        seen.add(identity)
        warnings: list[str] = []
        sf = _find_field(catalog.get(source[0]), source[1])
        tf = _find_field(catalog.get(target[0]), target[1])
        status = "stale_endpoint"
        review_needed = False
        if sf is None or tf is None:
            warnings.append("关系端点的数据表或字段不存在于当前 Schema。")
        else:
            status = "schema_valid"
            st, tt = str(sf.get("type") or sf.get("ui_type") or ""), str(tf.get("type") or tf.get("ui_type") or "")
            if st and tt and st != tt:
                warnings.append(f"字段类型不同：{st} → {tt}，值兼容性仍需查询结果验证。")
                review_needed = True
            if cardinality in {"one_to_one", "many_to_one"} and not bool(tf.get("is_primary")):
                warnings.append("目标字段不是 Schema 标记的主字段；Schema 不能证明字段值唯一。")
                review_needed = True
            warnings.append("Schema 只描述字段结构，不能证明任何行值唯一。")
            if review_needed:
                status = "needs_review"
        results.append({
            "id": rid,
            "name": str(relation.get("name") or ""),
            "description": str(relation.get("description") or ""),
            "cardinality": cardinality,
            "status": status,
            "validation_status": status,
            "source_table_id": source[0],
            "source_field_id": source[1],
            "target_table_id": target[0],
            "target_field_id": target[1],
            "source": {"table_id": source[0], "field_id": source[1]},
            "target": {"table_id": target[0], "field_id": target[1]},
            "warnings": warnings,
            "validation_warnings": warnings,
            "validation_scope": "schema_only",
        })
    return {"relations": results, "valid": all(item["status"] in {"schema_valid", "needs_review"} for item in results), "warnings": [warning for item in results for warning in item["warnings"]]}
