"""REST-shaped read-only Admin/Processing job observation adapter."""

from __future__ import annotations

import re
from typing import Any

from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.catalog.service import CatalogJobQueryService

_JOB_PATH_RE = re.compile(r"^/v1/jobs/([A-Za-z0-9._:-]{1,160})$")


class RestJobAdapter:
    """Keep job observation separate from Query and Admin mutation routes."""

    def __init__(self, *, jobs: CatalogJobQueryService) -> None:
        self._jobs = jobs

    async def handle(
        self,
        *,
        method: str,
        path: str,
        principal: Principal,
        correlation: Correlation,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        del body
        match = _JOB_PATH_RE.fullmatch(path)
        if method.upper() != "GET" or match is None:
            return QueryResult(
                status="error",
                trace_id=correlation.trace_id,
                error=QueryError(code=QueryErrorCode.INVALID_REQUEST, message="job route is invalid"),
            ).to_dict()
        return self._jobs.read_job(
            principal=principal,
            correlation=correlation,
            job_id=match.group(1),
        ).to_dict()
