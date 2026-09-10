"""Local authenticated trace inspection and request correlation composition."""
import uuid
from fastapi.responses import JSONResponse
from knowledge_contracts import Correlation
from knowledge_platform.retrieval.trace import request_trace, fact, span


def install_tracing(app, store, principal):
    @app.middleware("http")
    async def trace_request(request, call_next):
        correlation = Correlation(uuid.uuid4().hex)
        with request_trace(correlation) as session:
            fact("principal", [principal.subject_id, principal.tenant_id])
            try:
                with span("http_request"):
                    response = await call_next(request)
                # HTTP status is transport evidence only; QueryResult outcome has its own event.
                fact("http_status", response.status_code, status="ok" if response.status_code < 400 else "error")
            except Exception:
                response = JSONResponse({"status":"error", "trace_id":correlation.trace_id,
                    "error":{"code":"internal_error", "message":"Request failed"}}, status_code=500)
            try:
                if any(event.name == "query_validation" for event in session.events):
                    await store.emit_batch(session.events)
            except Exception:
                # Persistence is required in this host. Do not report a successful audited query
                # when the trace store refused it; earlier writes may already have committed.
                response = JSONResponse({"status":"error", "trace_id":correlation.trace_id,
                    "error":{"code":"trace_unavailable", "message":"Request audit persistence failed"}}, status_code=503)
            response.headers["X-Knowledge-Trace-Id"] = correlation.trace_id
            return response

    @app.get("/v1/traces/{trace_id}")
    async def read_trace(trace_id: str):
        if principal.tenant_id is not None or not ({"knowledge.admin", "knowledge:admin"} & set(principal.scopes)):
            return JSONResponse({"status":"error", "error":{"code":"permission_denied", "message":"Trace inspection requires local administration"}}, status_code=403)
        try:
            events = store.read(trace_id, limit=512)
        except ValueError:
            return JSONResponse({"status":"error", "error":{"code":"invalid_request", "message":"Invalid trace identifier"}}, status_code=400)
        if not events:
            return JSONResponse({"status":"error", "error":{"code":"not_found", "message":"Trace not found"}}, status_code=404)
        return {"status":"ok", "data":{"trace_id":trace_id, "events":events}}
