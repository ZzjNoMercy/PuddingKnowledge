"""Package operations resolve paths solely from explicit host bindings."""
from __future__ import annotations

from pathlib import Path
import sqlite3

from fastapi import APIRouter, Depends
from knowledge_contracts import Correlation, Principal, QueryError, QueryErrorCode, QueryResult
from knowledge_platform.ingestion.admin import _authorized
from knowledge_platform.local.packages import package_principal
from .fastapi_router import _resolve


def create_packages_router(*, publisher, exporter, config, principal_provider):
    router = APIRouter(prefix="/v1/packages", tags=["knowledge-admin"])
    exports = {entry["id"]: entry for entry in config["exports"]}

    async def principal():
        return await _resolve(principal_provider())

    def error(code, message):
        return QueryResult(status="error", trace_id="package-operation",
                           error=QueryError(code=code, message=message)).to_dict()

    @router.post(":export")
    async def export_package(body: dict, who: Principal = Depends(principal)):
        if not all(_authorized(who, space) for space in config["space_ids"]):
            return error(QueryErrorCode.PERMISSION_DENIED, "Admin and configured Space scopes are required")
        if set(body) != {"output_ref", "package_id", "version", "collections"}:
            return error(QueryErrorCode.INVALID_REQUEST, "Package export fields are invalid")
        if not isinstance(body["output_ref"], str):
            return error(QueryErrorCode.INVALID_REQUEST, "Package output binding is invalid")
        binding = exports.get(body["output_ref"])
        if binding is None:
            return error(QueryErrorCode.NOT_FOUND, "Package output binding is unavailable")
        try:
            result = await exporter.export(principal=package_principal(who, config), correlation=Correlation("package-export"),
                output_zip=Path(binding["path"]), package_id=body["package_id"],
                version=body["version"], collections=body["collections"])
            return QueryResult(status="ok", trace_id="package-operation", data=result).to_dict()
        except PermissionError:
            return error(QueryErrorCode.PERMISSION_DENIED, "Package scope is not authorized")
        except (ValueError, OSError, RuntimeError, TypeError, sqlite3.Error):
            return error(QueryErrorCode.INVALID_REQUEST, "Package export failed validation or publication")
    return router
