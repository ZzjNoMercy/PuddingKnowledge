import asyncio
import io
import json

import pytest

from knowledge_platform.local.wiki_model import HttpWikiModelGateway, validate_model_config
from knowledge_platform.wiki.ports import RawSnapshot

SNAPSHOT = RawSnapshot('asset_a', 'v1', 'knowledge://spaces/space_kb_default/assets/asset_a', 'source', 'sha256:' + 'a' * 64)


def response(content=None, finish='stop'):
    return {'choices': [{'finish_reason': finish, 'message': {'content': content if content is not None else json.dumps({'title': 'Title', 'markdown': '# Title\nSource'})}}]}


def generate(monkeypatch, value):
    calls = []
    class Opener:
        def open(self, req, timeout):
            calls.append((req, timeout))
            return io.BytesIO(json.dumps(value).encode())
    monkeypatch.setattr('urllib.request.build_opener', lambda *args: Opener())
    result = asyncio.run(HttpWikiModelGateway({'endpoint': 'http://127.0.0.1:1234/chat', 'model': 'fixture'}).generate(context='bounded', snapshot=SNAPSHOT))
    return result, calls


def test_completed_draft_preserves_host_source_identity(monkeypatch):
    draft, calls = generate(monkeypatch, response())
    assert draft.source_snapshot_id == 'asset_a'
    assert draft.source_revision == 'v1'
    assert draft.path == 'wiki/asset_a.md'
    body = json.loads(calls[0][0].data)
    assert body['stream'] is False
    assert json.loads(body['messages'][1]['content'])['source_text'] == 'bounded'


@pytest.mark.parametrize('value', [response(finish='length'), response(' '), response('{}'), response('not-json'), {'choices': []}, response(json.dumps({'title': 'x', 'markdown': '# x', 'path': '/etc/passwd'}))])
def test_incomplete_or_invalid_output_cannot_publish(monkeypatch, value):
    with pytest.raises(ValueError):
        generate(monkeypatch, value)


@pytest.mark.parametrize('endpoint', ['http://remote.example/chat', 'https://a:secret@example.com/chat', 'https://example.com/chat?token=a', 'file:///tmp/model', 'https://example.com/chat#secret'])
def test_invalid_endpoints_rejected(endpoint):
    with pytest.raises(ValueError):
        validate_model_config({'endpoint': endpoint, 'model': 'test'})


def test_missing_credential_fails_before_network(monkeypatch):
    monkeypatch.delenv('KNOWLEDGE_TEST_MISSING_KEY', raising=False)
    gateway = HttpWikiModelGateway({'endpoint': 'https://example.com/chat', 'model': 'test', 'api_key_env': 'KNOWLEDGE_TEST_MISSING_KEY'})
    with pytest.raises(ValueError, match='credential'):
        asyncio.run(gateway.generate(context='x', snapshot=SNAPSHOT))
