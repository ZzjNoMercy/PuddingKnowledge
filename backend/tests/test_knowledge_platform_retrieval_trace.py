import asyncio
import json
from datetime import datetime
import httpx
from fastapi import FastAPI
from knowledge_contracts import Correlation, Principal
from knowledge_platform.retrieval.trace import request_trace, current_correlation, span, fact
from knowledge_platform.local.tracing import install_tracing
from knowledge_platform.local.trace_store import SqliteTraceSink


def test_concurrent_spans_do_not_share_identity_or_events():
    async def worker(i):
        with request_trace(Correlation(str(i))) as session:
            with span('query_embedding'):
                await asyncio.sleep(.002)
                assert current_correlation().trace_id == str(i)
                fact('identity', i)
            return session.events
    async def run(): return await asyncio.gather(*(worker(i) for i in range(12)))
    for i, events in enumerate(asyncio.run(run())):
        assert len(events) == 3 and all(e.trace_id == str(i) for e in events)
        assert events[0].span_id == events[-1].span_id
        assert datetime.fromisoformat(events[-1].timestamp) >= datetime.fromisoformat(events[0].timestamp)


def test_trace_read_requires_admin_even_with_search_scope(tmp_path):
    app = FastAPI(); store = SqliteTraceSink(tmp_path / 'traces.db')
    install_tracing(app, store, Principal('caller', scopes=('knowledge.search',)))
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            return await client.get('/v1/traces/known')
    response = asyncio.run(run())
    assert response.status_code == 403 and 'events' not in response.text


def test_failed_sink_does_not_report_successful_query_audit(tmp_path):
    app = FastAPI()
    store = SqliteTraceSink(tmp_path / 'traces.db', max_records=1)
    install_tracing(app, store, Principal('admin', scopes=('knowledge.admin',)))
    @app.get('/query')
    async def query():
        with span('query_validation'): fact('query_result', [], status='ok')
        return {'status':'ok', 'trace_id':current_correlation().trace_id}
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://local') as client:
            return await client.get('/query')
    result = asyncio.run(run())
    assert result.status_code == 503 and result.json()['error']['code'] == 'trace_unavailable'


def test_denied_query_does_not_inspect_or_record_assets():
    from knowledge_platform.retrieval.services import DocumentRetrievalService
    class ForbiddenAccess:
        def __getattr__(self, name): raise AssertionError('Denied caller reached data')
    service = DocumentRetrievalService(ForbiddenAccess(), ForbiddenAccess())
    async def run():
        with request_trace(Correlation('denied')) as session:
            result = await service.query(principal=Principal('subject-secret'), correlation=Correlation('denied'),
                query='private query', space_id='secret-space')
            return result, session.events
    result, events = asyncio.run(run())
    assert result.status == 'error'
    assert all(e.phase != 'rank' for e in events)
    assert events[-1].status == 'error'
    from dataclasses import asdict
    encoded = json.dumps([asdict(e) for e in events])
    assert all(value not in encoded for value in ('subject-secret', 'private query', 'secret-space'))
