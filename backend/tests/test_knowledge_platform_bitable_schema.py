import pytest

from knowledge_platform.connector_sync.bitable_schema import normalize_schema, validate_relations


def fields():
    return [
        {"field_id": "f2", "field_name": "Code", "type": "text", "property": {"ui_type": "Text"}, "sample_values": ["secret"]},
        {"field_id": "f1", "field_name": "ID", "type": "text", "property": {"ui_type": "Text"}, "is_primary": True},
    ]


def test_normalize_schema_is_order_stable_and_does_not_keep_values():
    left = normalize_schema("app", "tbl", "Table", "vew", fields())
    right = normalize_schema("app", "tbl", "Table", "vew", list(reversed(fields())))
    assert left["digest"] == right["digest"]
    assert "sample_values" not in repr(left)
    assert [f["field_id"] for f in left["fields"]] == ["f1", "f2"]


def test_schema_digest_changes_for_name_type_and_property():
    base = normalize_schema("app", "tbl", "Table", "vew", fields())["digest"]
    for key, value in (("field_name", "Changed"), ("type", "number"), ("property", {"format": "url"})):
        changed = fields()
        changed[0][key] = value
        assert normalize_schema("app", "tbl", "Table", "vew", changed)["digest"] != base


def test_duplicate_or_malformed_fields_are_rejected():
    duplicate = fields() + [{"field_id": "f1", "field_name": "Again", "type": "text", "property": {}}]
    with pytest.raises(ValueError):
        normalize_schema("app", "tbl", "Table", None, duplicate)
    with pytest.raises(ValueError):
        normalize_schema("app", "tbl", "Table", None, [{"field_id": "f", "type": "text", "property": []}])


def test_validate_relations_reports_stale_type_and_uniqueness_warning():
    schema = {"a": normalize_schema("app", "a", "A", None, fields()), "b": normalize_schema("app", "b", "B", None, [{"field_id": "g", "field_name": "G", "type": "number", "property": {}}])}
    result = validate_relations([{"id": "r", "source_table_id": "a", "source_field_id": "f1", "target_table_id": "b", "target_field_id": "g", "cardinality": "many_to_one"}], schema)
    assert result["valid"] is True
    assert result["relations"][0]["status"] == "needs_review"
    assert result["relations"][0]["source_table_id"] == "a"
    assert any("类型不同" in warning for warning in result["warnings"])
    assert any("不能证明" in warning for warning in result["warnings"])
    stale = validate_relations([{"id": "s", "cardinality": "many_to_one", "source_table_id": "a", "source_field_id": "missing", "target_table_id": "b", "target_field_id": "g"}], schema)
    assert stale["valid"] is False and stale["relations"][0]["status"] == "stale_endpoint"


def test_reverse_duplicate_and_self_relation_rejected():
    schema = {"a": normalize_schema("app", "a", "A", None, fields()), "b": normalize_schema("app", "b", "B", None, fields())}
    relations = [{"id": "r1", "cardinality": "many_to_one", "source_table_id": "a", "source_field_id": "f1", "target_table_id": "b", "target_field_id": "f1"}, {"id": "r2", "cardinality": "many_to_one", "source_table_id": "b", "source_field_id": "f1", "target_table_id": "a", "target_field_id": "f1"}]
    with pytest.raises(ValueError, match="duplicate"):
        validate_relations(relations, schema)
    with pytest.raises(ValueError, match="itself"):
        validate_relations([{"id": "self", "cardinality": "many_to_one", "source_table_id": "a", "source_field_id": "f1", "target_table_id": "a", "target_field_id": "f1"}], schema)
    with pytest.raises(ValueError, match="relation id"):
        validate_relations([{"id": "same", "cardinality": "many_to_many", "source_table_id": "a", "source_field_id": "f1", "target_table_id": "b", "target_field_id": "f2"}, {"id": "same", "cardinality": "many_to_many", "source_table_id": "a", "source_field_id": "f2", "target_table_id": "b", "target_field_id": "f1"}], schema)


def test_numeric_provider_type_and_exact_field_name():
    schema = normalize_schema("app", "tbl", "Table", "", [{"field_id":"fld", "field_name":" Name ", "type":1}])
    assert schema.document["fields"][0]["type"] == 1
    assert schema.document["fields"][0]["field_name"] == " Name "
    for invalid in (True, False, 0, -1, None, []):
        with pytest.raises(ValueError):
            normalize_schema("app", "tbl", "Table", "", [{"field_id":"fld", "field_name":"Name", "type":invalid}])
