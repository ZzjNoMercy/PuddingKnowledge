"""Independent HTTP tracing proof for the existing hybrid acceptance scenario."""
import json
import pytest
import test_knowledge_platform_hybrid_runtime as hybrid
from test_knowledge_platform_package_runtime import Runtime
from test_knowledge_platform_milvus_runtime import milvus


@pytest.mark.parametrize('provider', ['local', 'milvus'])
def test_hybrid_traces_persist_correlate_and_exclude_source_text(tmp_path, request, monkeypatch, provider):
    observations = []
    class TracedRuntime(Runtime):
        def start(self):
            super().start()
            if observations:
                old = self.call('/v1/traces/' + observations[0]['trace_id'])
                assert old['data']['events'] == observations[0]['events']
        def call(self, path, body=None):
            value = super().call(path, body)
            if path in ('/v1/knowledge/query', '/v1/document-rag/query', '/mcp'):
                result = value['result']['structuredContent'] if path == '/mcp' else value
                trace_id = result['trace_id']
                assert trace_id not in [o['trace_id'] for o in observations]
                trace = super().call('/v1/traces/' + trace_id)
                events = trace['data']['events']
                assert events and all(e['trace_id'] == trace_id and e['correlation']['trace_id'] == trace_id for e in events)
                assert all(e['name'] != 'http_request' or e['phase'] != 'end' or e['status'] == 'ok' for e in events)
                outcomes = [e for e in events if e['name'] == 'query_result']
                assert outcomes and outcomes[-1]['status'] == result['status']
                encoded = json.dumps(events)
                assert all(secret not in encoded for secret in ('ZXQ42', 'General archive', 'FORGED', 'knowledge://', '/private/tmp', 'a_noise', 'b_target'))
                if result['status'] == 'ok':
                    assert any(e['name'] == 'publication_validation' and e['phase'] == 'end' and e['status'] == 'ok' for e in events)
                    assert any(e['name'] == 'bm25' and e['phase'] == 'rank' for e in events)
                observations.append(dict(trace_id=trace_id, events=events))
            return value
    monkeypatch.setattr(hybrid, 'Runtime', TracedRuntime)
    hybrid.test_hybrid_fusion_rerank_restart_and_untrusted_results(tmp_path, request, provider)
    assert len(observations) >= 10
    assert any(e['name'] == 'rerank' and e['status'] == 'error' for o in observations for e in o['events'])
