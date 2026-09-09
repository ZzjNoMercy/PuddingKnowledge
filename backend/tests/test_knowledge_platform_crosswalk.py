from __future__ import annotations

import copy

import pytest

from knowledge_platform.semantic import (
    CrosswalkCompositionError,
    LocalCrosswalkPublisher,
    build_vehicle_series_collision_policy_template,
    build_vehicle_series_crosswalk,
    compose_active_crosswalk,
    find_vehicle_series_canonical_collisions,
    validate_active_crosswalk,
)


def _generated() -> dict[str, object]:
    return {
        "formatter": "entity-resolution-crosswalk",
        "records": [
            {
                "record_kind": "canonical_entity",
                "entity": {"entity_key": "brand-a::series-a", "canonical_brand": "品牌A", "canonical_series": "车系A"},
                "bindings": [
                    {
                        "source_kind": "canonical_reference",
                        "source_ref": "canonical:vehicle",
                        "key_fields": {"brand": "品牌A", "series": "车系A"},
                    }
                ],
                "resolution": {"status": "canonical_only", "join_eligible": False, "evidence": []},
            },
            {
                "record_kind": "canonical_entity",
                "entity": {"entity_key": "brand-b::series-b", "canonical_brand": "品牌B", "canonical_series": "车系B"},
                "bindings": [
                    {
                        "source_kind": "canonical_reference",
                        "source_ref": "canonical:vehicle",
                        "key_fields": {"brand": "品牌B", "series": "车系B"},
                    }
                ],
                "resolution": {"status": "canonical_only", "join_eligible": False, "evidence": []},
            },
        ],
        "source_diagnostics": [
            {
                "record_kind": "source_diagnostic",
                "entity": None,
                "bindings": [
                    {
                        "source_kind": "table_asset",
                        "source_ref": "table_asset:sales",
                        "key_fields": {"brand": "来源品牌", "series": "来源车系"},
                    }
                ],
                "resolution": {"status": "unmatched", "join_eligible": False, "evidence": ["待审核"]},
            }
        ],
    }


def test_bind_moves_diagnostic_to_existing_canonical_without_changing_universe() -> None:
    generated = _generated()
    active = compose_active_crosswalk(
        generated,
        [
            {
                "operation": "bind",
                "source_ref": "table_asset:sales",
                "source_key": {"brand": "来源品牌", "series": "来源车系"},
                "entity_key": "brand-a::series-a",
            }
        ],
    )

    assert len(active["records"]) == 2
    assert len(active["source_diagnostics"]) == 0
    record = active["records"][0]
    assert any(binding["source_ref"] == "table_asset:sales" for binding in record["bindings"])
    assert record["resolution"]["status"] == "accepted"
    assert active["summary"]["overrides_applied"] == 1
    assert active["active_digest"].startswith("sha256:")
    assert validate_active_crosswalk(active) == active


def test_exclude_preserves_source_as_rejected_diagnostic_and_is_replay_deterministic() -> None:
    generated = _generated()
    override = {
        "operation": "exclude",
        "source_ref": "table_asset:sales",
        "source_key": {"brand": "来源品牌", "series": "来源车系"},
    }
    first = compose_active_crosswalk(generated, [override])
    second = compose_active_crosswalk(copy.deepcopy(generated), [override])

    assert first == second
    assert len(first["records"]) == 2
    assert first["source_diagnostics"][0]["resolution"]["status"] == "rejected"
    assert first["source_diagnostics"][0]["resolution"]["join_eligible"] is False


def test_invalid_target_and_missing_source_fail_closed() -> None:
    with pytest.raises(CrosswalkCompositionError):
        compose_active_crosswalk(
            _generated(),
            [
                {
                    "operation": "bind",
                    "source_ref": "table_asset:sales",
                    "source_key": {"brand": "来源品牌", "series": "来源车系"},
                    "entity_key": "not-canonical",
                }
            ],
        )
    with pytest.raises(CrosswalkCompositionError):
        compose_active_crosswalk(
            _generated(),
            [
                {
                    "operation": "exclude",
                    "source_ref": "table_asset:sales",
                    "source_key": {"brand": "不存在", "series": "不存在"},
                }
            ],
        )


def test_unicode_canonical_entity_key_is_supported() -> None:
    generated = _generated()
    generated["records"][0]["entity"]["entity_key"] = "比亚迪::秦plus"
    active = compose_active_crosswalk(
        generated,
        [
            {
                "operation": "bind",
                "source_ref": "table_asset:sales",
                "source_key": {"brand": "来源品牌", "series": "来源车系"},
                "entity_key": "比亚迪::秦plus",
            }
        ],
    )
    assert active["records"][0]["entity"]["entity_key"] == "比亚迪::秦plus"


def test_manual_override_cannot_mutate_canonical_source_of_truth() -> None:
    generated = _generated()
    canonical = generated["records"][0]["bindings"][0]

    with pytest.raises(CrosswalkCompositionError, match="canonical source of truth"):
        compose_active_crosswalk(
            generated,
            [
                {
                    "operation": "exclude",
                    "source_ref": canonical["source_ref"],
                    "source_key": canonical["key_fields"],
                }
            ],
        )


def test_vehicle_series_builder_uses_database_source_as_canonical_universe() -> None:
    generated = build_vehicle_series_crosswalk(
        canonical_rows=[
            {"brand": "比亚迪", "serial_name": "秦PLUS"},
            {"brand": "丰田", "serial_name": "凯美瑞"},
            {"brand": "丰田", "serial_name": "凯美瑞"},
        ],
        source_rows=[
            {"品牌": "比亚迪", "1-子车型": "秦 PLUS"},
            {"品牌": "未知品牌", "1-子车型": "未知车系"},
        ],
        canonical_source_ref="database:dbs_vehicle:vehicle_model_base",
        source_ref="table_asset:insurance_july",
    )

    assert len(generated["records"]) == 2
    assert generated["summary"]["canonical_entity_count"] == 2
    assert generated["summary"]["canonical_with_source_binding_count"] == 1
    assert len(generated["source_diagnostics"]) == 1
    matched = next(record for record in generated["records"] if record["entity"]["canonical_brand"] == "比亚迪")
    assert matched["resolution"]["status"] == "auto_matched"
    canonical_binding = matched["bindings"][0]
    assert canonical_binding["canonical"] is True


def test_vehicle_builder_rejects_normalized_canonical_collision() -> None:
    rows = [
        {"brand": "品牌", "serial_name": "车系"},
        {"brand": "品牌", "serial_name": "车 系"},
    ]
    collisions = find_vehicle_series_canonical_collisions(rows)
    assert collisions[0]["candidate_count"] == 2
    assert len(collisions[0]["candidate_digests"]) == 2
    with pytest.raises(CrosswalkCompositionError, match="canonical normalized collision"):
        build_vehicle_series_crosswalk(
            canonical_rows=rows,
            source_rows=[{"品牌": "品牌", "1-子车型": "车系"}],
            canonical_source_ref="database:dbs_vehicle:vehicle_model_base",
            source_ref="table_asset:insurance_july",
        )


def test_vehicle_builder_accepts_only_an_explicit_candidate_digest_policy() -> None:
    rows = [
        {"brand": "品牌", "serial_name": "车系"},
        {"brand": "品牌", "serial_name": "车 系"},
    ]
    collisions = find_vehicle_series_canonical_collisions(rows)
    selected = collisions[0]["candidate_digests"][1]
    generated = build_vehicle_series_crosswalk(
        canonical_rows=rows,
        source_rows=[{"品牌": "品牌", "1-子车型": "车系"}],
        canonical_source_ref="database:dbs_vehicle:vehicle_model_base",
        source_ref="table_asset:insurance_july",
        canonical_collision_policy={collisions[0]["normalized_key"]: selected},
    )
    assert generated["summary"]["canonical_entity_count"] == 1
    assert generated["canonical_collision_policy"]["mode"] == "explicit_candidate_digest"
    assert generated["canonical_collision_policy"]["digest"].startswith("sha256:")


def test_vehicle_collision_policy_template_is_non_selecting_and_raw_value_free() -> None:
    rows = [
        {"brand": "品牌", "serial_name": "车系"},
        {"brand": "品牌", "serial_name": "车 系"},
    ]
    template = build_vehicle_series_collision_policy_template(rows)
    assert template["status"] == "decision-required"
    selection = next(iter(template["selections"].values()))
    assert str(selection["normalized_key_digest"]).startswith("sha256:")
    assert selection["selected_candidate_digest"] is None
    serialized = repr(template)
    assert "车系" not in serialized
    assert "车 系" not in serialized


def test_vehicle_builder_rejects_incomplete_or_unknown_collision_policy() -> None:
    rows = [
        {"brand": "品牌", "serial_name": "车系"},
        {"brand": "品牌", "serial_name": "车 系"},
    ]
    with pytest.raises(CrosswalkCompositionError, match="resolve every collision"):
        build_vehicle_series_crosswalk(
            canonical_rows=rows,
            source_rows=[{"品牌": "品牌", "1-子车型": "车系"}],
            canonical_source_ref="database:dbs_vehicle:vehicle_model_base",
            source_ref="table_asset:insurance_july",
            canonical_collision_policy={},
        )
    collisions = find_vehicle_series_canonical_collisions(rows)
    with pytest.raises(CrosswalkCompositionError, match="unknown candidate"):
        build_vehicle_series_crosswalk(
            canonical_rows=rows,
            source_rows=[{"品牌": "品牌", "1-子车型": "车系"}],
            canonical_source_ref="database:dbs_vehicle:vehicle_model_base",
            source_ref="table_asset:insurance_july",
            canonical_collision_policy={collisions[0]["normalized_key"]: "sha256:" + "0" * 64},
        )


def test_crosswalk_publisher_is_content_addressed_and_idempotent(tmp_path) -> None:
    publisher = LocalCrosswalkPublisher(root=tmp_path / "published")
    override = {
        "operation": "bind",
        "source_ref": "table_asset:sales",
        "source_key": {"brand": "来源品牌", "series": "来源车系"},
        "entity_key": "brand-a::series-a",
    }

    first = publisher.publish(
        space_id="space_kb_default",
        dimension_id="vehicle_series",
        generated=_generated(),
        overrides=[override],
    )
    second = publisher.publish(
        space_id="space_kb_default",
        dimension_id="vehicle_series",
        generated=_generated(),
        overrides=[override],
    )

    assert first == second
    assert publisher.publish_count == 1
    assert first.resource_uri == "knowledge://spaces/space_kb_default/semantic-dimensions/vehicle_series/crosswalk"
    files = list((tmp_path / "published" / "crosswalks").rglob("*.json"))
    assert len(files) == 1
    assert files[0].read_text(encoding="utf-8").endswith("\n")


def test_crosswalk_publisher_rejects_symlinked_root(tmp_path) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    symlink_root = tmp_path / "published"
    symlink_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(OSError):
        LocalCrosswalkPublisher(root=symlink_root).publish(
            space_id="space_kb_default", dimension_id="vehicle_series", generated=_generated()
        )


def test_crosswalk_publisher_rejects_symlinked_intermediate_directory(tmp_path) -> None:
    root = tmp_path / "published"
    (root / "crosswalks").mkdir(parents=True)
    real_dir = tmp_path / "real-space"
    real_dir.mkdir()
    (root / "crosswalks" / "space_kb_default").symlink_to(real_dir, target_is_directory=True)

    with pytest.raises(OSError):
        LocalCrosswalkPublisher(root=root).publish(
            space_id="space_kb_default", dimension_id="vehicle_series", generated=_generated()
        )


def test_active_crosswalk_digest_tampering_is_rejected() -> None:
    active = compose_active_crosswalk(_generated())
    tampered = copy.deepcopy(active)
    tampered["summary"]["canonical_entity_count"] = 99

    with pytest.raises(CrosswalkCompositionError):
        validate_active_crosswalk(tampered)
