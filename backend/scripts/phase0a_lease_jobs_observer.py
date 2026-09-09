"""Replay the legacy queue lease and runtime-control protocols locally."""

from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

import runtime_control
from knowledge.import_jobs import claim_next_job, get_import_job, mark_job_failed, retry_import_job
from knowledge.models import KnowledgeBase, KnowledgeImportJob, SemanticDimensionBuildJob
from knowledge.queue_repository import LeaseLostError, heartbeat, require_lease
from knowledge.semantic_dimension_jobs import claim_next_semantic_dimension_build_job
from schema_migrations import migrate_to_latest

FIXTURE_PATH = Path(__file__).resolve().parents[2] / "docs/knowledge-platform/golden-fixtures/lease_and_jobs.json"


def _fixture() -> dict[str, Any]:
    document = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    if document.get("format") != "agent-knowledge-platform-golden-fixture/lease-and-jobs/v1":
        raise ValueError("lease fixture format is invalid")
    if document.get("sanitized") is not True or not isinstance(document.get("database"), dict):
        raise ValueError("lease fixture must be explicitly sanitized")
    return document


async def _observe() -> dict[str, object]:
    fixture = _fixture()
    database = fixture["database"]
    with tempfile.TemporaryDirectory(prefix="puddingclaw-golden-lease-") as directory:
        engine = create_async_engine(f"sqlite+aiosqlite:///{Path(directory) / 'queue.sqlite3'}")
        async with engine.begin() as connection:
            await connection.run_sync(migrate_to_latest)
        sessionmaker = async_sessionmaker(engine, expire_on_commit=False)
        try:
            async with sessionmaker() as session:
                base = database["knowledge_base"]
                session.add(KnowledgeBase(**base))
                import_data = database["import_job"]
                session.add(KnowledgeImportJob(**import_data))
                session.add(SemanticDimensionBuildJob(**database["semantic_job"]))
                await session.commit()

            async with sessionmaker() as session:
                claimed = await claim_next_job(session, worker_id="worker-a", lease_seconds=60)
                if claimed is None or claimed.id != "job-golden-lease":
                    raise AssertionError("worker-a did not claim the import job")
                heartbeat_ok = await heartbeat(session, KnowledgeImportJob, claimed.id, "worker-a", lease_seconds=60)
                wrong_owner_heartbeat = await heartbeat(
                    session, KnowledgeImportJob, claimed.id, "worker-b", lease_seconds=60
                )
                await require_lease(session, KnowledgeImportJob, claimed.id, "worker-a")
                await session.commit()

            async with sessionmaker() as session:
                job = await get_import_job(session, "job-golden-lease")
                if job is None:
                    raise AssertionError("claimed import job disappeared")
                await mark_job_failed(session, job, "fixture failure", lease_owner="worker-a")
                retried = await retry_import_job(session, job.id)
                retry_state = {"status": retried.status, "retry_count": retried.retry_count}

            async with sessionmaker() as session:
                reclaimed = await claim_next_job(session, worker_id="worker-b", lease_seconds=60)
                if reclaimed is None:
                    raise AssertionError("worker-b did not reclaim the retried import job")
                reclaimed_state = {
                    "status": reclaimed.status,
                    "attempt": reclaimed.attempt,
                    "lease_owner": reclaimed.lease_owner,
                    "current_step": reclaimed.current_step,
                }
                stale_fence = "not-attempted"
                try:
                    await require_lease(session, KnowledgeImportJob, reclaimed.id, "worker-a")
                except LeaseLostError:
                    stale_fence = "rejected"
                await mark_job_failed(session, reclaimed, "fixture terminal failure", lease_owner="worker-b")

            async with sessionmaker() as session:
                acquired = await runtime_control.acquire_maintenance(
                    session, owner="maintenance-a", reason="golden fixture"
                )
                renewed = await runtime_control.renew_maintenance(session, owner="maintenance-a", lease_seconds=60)
                blocked_claim = await claim_next_semantic_dimension_build_job(
                    session, worker_id="worker-c", lease_seconds=60
                )
                entered = await runtime_control.enter_maintenance(session, owner="maintenance-a", lease_seconds=60)
                writes_allowed = await runtime_control.writes_allowed(session)
                released = await runtime_control.release_maintenance(session, owner="maintenance-a", reason="complete")

            async with sessionmaker() as session:
                semantic_claim = await claim_next_semantic_dimension_build_job(
                    session, worker_id="worker-c", lease_seconds=60
                )
                if semantic_claim is None:
                    raise AssertionError("semantic job was not claimable after release")
                semantic_state = {
                    "status": semantic_claim.status,
                    "attempt": semantic_claim.attempt,
                    "lease_owner": semantic_claim.lease_owner,
                    "current_step": semantic_claim.current_step,
                }
                await session.rollback()

            async with sessionmaker() as session:
                import_count = int((await session.execute(text("SELECT COUNT(*) FROM knowledge_import_jobs"))).scalar_one())
                semantic_count = int(
                    (await session.execute(text("SELECT COUNT(*) FROM semantic_dimension_build_jobs"))).scalar_one()
                )
                event_count = int((await session.execute(text("SELECT COUNT(*) FROM knowledge_import_events"))).scalar_one())
                runtime_count = int((await session.execute(text("SELECT COUNT(*) FROM core_runtime_control"))).scalar_one())
                final_import = dict(
                    zip(
                        ("status", "retry_count", "attempt", "lease_owner"),
                        (
                            await session.execute(
                                text(
                                    "SELECT status, retry_count, attempt, lease_owner "
                                    "FROM knowledge_import_jobs WHERE id = 'job-golden-lease'"
                                )
                            )
                        ).one(),
                    )
                )
        finally:
            await engine.dispose()

    return {
        "result": {
            "heartbeat_owner_a": heartbeat_ok,
            "wrong_owner_heartbeat": wrong_owner_heartbeat,
            "stale_fence": stale_fence,
            "blocked_claim_during_drain": blocked_claim is None,
            "writes_allowed_in_maintenance": writes_allowed,
            "retry_state": retry_state,
            "maintenance_modes": [acquired["write_mode"], renewed["write_mode"], entered["write_mode"], released["write_mode"]],
            "reclaimed_import": reclaimed_state,
            "semantic_claim_after_release": semantic_state,
        },
        "evidence": {
            "maintenance_generations": [acquired["generation"], renewed["generation"], entered["generation"], released["generation"]],
            "final_import": {key: value for key, value in final_import.items() if key != "lease_owner"},
            "row_counts": {"import_jobs": import_count, "semantic_jobs": semantic_count, "import_events": event_count, "runtime_control": runtime_count},
        },
        "database_side_effects": {
            "business_rows_written": import_count + semantic_count + event_count + runtime_count,
            "terminal_import_status": final_import["status"],
            "active_revision_changed": False,
        },
        "filesystem_side_effects": {"business_writes": []},
        "provider_revision": "legacy-queue-lease-runtime-control-v1",
        "failure_semantics": "lost_lease->terminal_write_rejected; drain->claim_rejected; maintenance->writes_blocked",
        "sanitized_fixture_manifest": {"fixtures": ["docs/knowledge-platform/golden-fixtures/lease_and_jobs.json"]},
    }


def observe() -> dict[str, object]:
    return asyncio.run(_observe())
