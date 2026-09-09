from __future__ import annotations

import sqlite3

from fastapi.testclient import TestClient

from knowledge_contracts import Correlation, Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.catalog.service import CatalogJobQueryService
from knowledge_platform.transport import RestJobAdapter, create_platform_app


def _principal(*scopes: str) -> Principal:
    return Principal("job-test", scopes=scopes)


def test_sqlite_job_projection_redacts_host_fields(tmp_path) -> None:
    database = tmp_path / "catalog.sqlite3"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE knowledge_processing_jobs (
            id TEXT PRIMARY KEY, space_id TEXT, kind TEXT, status TEXT,
            asset_id TEXT, source_item_id TEXT, sync_run_id TEXT,
            current_step TEXT, progress INTEGER, retry_count INTEGER, attempt INTEGER,
            created_at TEXT, started_at TEXT, finished_at TEXT,
            input_uri TEXT, metadata_json TEXT, error_message TEXT,
            lease_owner TEXT, lease_expires_at TEXT, heartbeat_at TEXT
        );
        INSERT INTO knowledge_processing_jobs VALUES (
            'processing_job_1', 'space_demo', 'import', 'succeeded',
            'asset_1', '', '', 'completed', 100, 0, 1,
            '2026-09-05T00:00:00Z', '2026-09-05T00:01:00Z', '2026-09-05T00:02:00Z',
            '/Users/pet/private.pdf', '{}', '/Users/pet/secret',
            'worker-secret', '2026-09-05T00:03:00Z', '2026-09-05T00:02:00Z'
        );
        """
    )
    connection.commit()
    connection.close()

    repository = SqliteCatalogQueryRepository(database)
    job = repository.get_job(job_id="processing_job_1")
    assert job == {
        "id": "processing_job_1",
        "kind": "import",
        "status": "succeeded",
        "space_id": "space_demo",
        "asset_id": "asset_1",
        "source_item_id": "",
        "sync_run_id": "",
        "current_step": "completed",
        "progress": 100,
        "retry_count": 0,
        "attempt": 1,
        "created_at": "2026-09-05T00:00:00Z",
        "started_at": "2026-09-05T00:01:00Z",
        "finished_at": "2026-09-05T00:02:00Z",
    }


def test_job_service_requires_processing_scope_and_space_scope() -> None:
    class Repository:
        def get_job(self, *, job_id):
            return {"id": job_id, "kind": "import", "status": "queued", "space_id": "space_demo"}

    service = CatalogJobQueryService(Repository())
    correlation = Correlation("job-service")
    denied = service.read_job(principal=_principal(), correlation=correlation, job_id="job_1")
    assert denied.error.code == "permission_denied"
    wrong_space = service.read_job(
        principal=_principal("knowledge.processing", "knowledge.space:other"),
        correlation=correlation,
        job_id="job_1",
    )
    assert wrong_space.error.code == "permission_denied"
    allowed = service.read_job(
        principal=_principal("knowledge.processing", "knowledge.space:space_demo"),
        correlation=correlation,
        job_id="job_1",
    )
    assert allowed.status == "ok"
    assert allowed.data["job"]["id"] == "job_1"


def test_job_adapter_is_mounted_as_an_optional_admin_processing_route() -> None:
    class Query:
        async def handle(self, **kwargs):
            return {"status": "ok", "path": kwargs["path"]}

    class Repository:
        catalog_revision = "sha256:" + "0" * 64

        def get_job(self, *, job_id):
            return {"id": job_id, "kind": "import", "status": "queued", "space_id": "space_demo"}

    app = create_platform_app(
        query_adapter=Query(),
        job_adapter=RestJobAdapter(jobs=CatalogJobQueryService(Repository())),
        principal_provider=lambda: _principal("knowledge.processing", "knowledge.space:space_demo"),
        correlation_provider=lambda: Correlation("job-http"),
    )
    client = TestClient(app)
    response = client.get("/v1/jobs/job_1")
    assert response.status_code == 200
    assert response.json()["data"]["job"]["status"] == "queued"
    assert "/v1/jobs/{job_id}" in app.openapi()["paths"]
