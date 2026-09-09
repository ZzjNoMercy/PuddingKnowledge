from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine


def test_phase9_query_result_artifact_shadow_is_explicit_and_non_activating(tmp_path) -> None:
    from knowledge_platform.catalog import KnowledgeQueryResult, KnowledgeSpace, migrate_to_latest
    from scripts.phase9_local_query_result_artifact_shadow import run_shadow

    catalog = tmp_path / "knowledge-platform.sqlite3"
    engine = create_engine(f"sqlite:///{catalog}")
    now = datetime.now(timezone.utc)
    with engine.begin() as connection:
        migrate_to_latest(connection)
        connection.execute(
            KnowledgeSpace.__table__.insert().values(
                id="space_1",
                name="Local",
                description="",
                permissions_json={},
                created_at=now,
                updated_at=now,
            )
        )
        connection.execute(
            KnowledgeQueryResult.__table__.insert().values(
                id="query_result_1",
                status="ready",
                question="local question",
                sql_digest="sha256:" + "1" * 64,
                columns_json=["name"],
                row_count=1,
                profile_json={},
                artifact_uri="knowledge://query-results/query_result_1/artifact",
                artifact_reference_digest="sha256:" + "2" * 64,
                artifact_format="jsonl",
                correlation_json={},
                created_at=now,
                expires_at=now,
            )
        )
    engine.dispose()

    wiki_root = tmp_path / "wiki"
    wiki_root.mkdir()
    (wiki_root / "local.md").write_text("local fixture", encoding="utf-8")
    report = run_shadow(
        catalog_path=catalog,
        wiki_root=wiki_root,
        output_path=tmp_path / "report.json",
    )

    assert report["status"] == "PHASE9_LOCAL_QUERY_RESULT_ARTIFACT_SHADOW_PASS_NOT_ACTIVATABLE"
    assert report["catalog"]["canonical_unchanged"] is True
    assert report["authorized_read"]["status"] == "ok"
    assert report["authorized_read"]["space_id"] == "space_1"
    assert report["wrong_space_read"]["error_code"] == "permission_denied"
    assert report["unbound_read"]["error_code"] == "binding_unavailable"
