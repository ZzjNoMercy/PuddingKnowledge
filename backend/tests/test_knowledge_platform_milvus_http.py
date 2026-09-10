import json

import httpx
import pytest

from knowledge_platform.retrieval.milvus_http import MilvusStorageError, MilvusVectorStore

NAME = 'pkv_' + 'a' * 48


def store(handler):
    return MilvusVectorStore(endpoint='http://127.0.0.1:19530', dimension=2,
        client=httpx.Client(transport=httpx.MockTransport(handler)))


@pytest.mark.parametrize('endpoint', ['http://user:secret@localhost', 'http://localhost/path',
    'file:///tmp/a', 'http://localhost?q=1', 'http://localhost#x', 'http://localhost:bad', 'http://localhost\n'])
def test_invalid_configuration_never_connects(endpoint):
    with pytest.raises(ValueError):
        MilvusVectorStore(endpoint=endpoint, dimension=2)


def test_prepare_validates_count_and_uses_fresh_owned_names():
    calls = []
    def respond(req):
        body = json.loads(req.content); calls.append((req.url.path, body))
        return httpx.Response(200, json={'code': 0, 'data': {'insertCount': len(body.get('data', []))}})
    client = store(respond)
    one = client.prepare([(1., 0.)], identity='sha256:' + 'a' * 64)
    two = client.prepare([(1., 0.)], identity='sha256:' + 'a' * 64)
    assert one != two and one.startswith('pkv_')
    assert calls[1][1]['data'][0]['id'] == 0
    assert calls[1][1]['data'][0]['vector'] == [1., 0.]


@pytest.mark.parametrize('rows', [[], [{'id': 0, 'vector': [0., 1.]}],
    [{'id': True, 'vector': [1., 0.]}], [{'id': 0, 'vector': [float('nan'), 0.]}]])
def test_verify_rejects_missing_replaced_and_invalid_vectors(rows):
    def respond(req):
        body = json.loads(req.content)
        return httpx.Response(200, json={'code': 0, 'data': [{'count(*)': 1}]
            if body['outputFields'] == ['count(*)'] else rows})
    with pytest.raises(MilvusStorageError):
        store(respond).verify(NAME, [(1., 0.)])


@pytest.mark.parametrize('rows', [[{'id': 0, 'distance': 1}, {'id': 0, 'distance': 1}],
    [{'id': -1, 'distance': 1}], [{'id': 0, 'distance': 2}], [{'id': 0, 'distance': float('nan')}],
    [{'id': '0', 'distance': 1}]])
def test_search_rejects_untrusted_identities_and_scores(rows):
    with pytest.raises(MilvusStorageError):
        store(lambda _: httpx.Response(200, json={'code': 0, 'data': rows})).search(NAME, [1., 0.], 2)


def test_error_does_not_expose_provider_text_or_credentials():
    client = store(lambda _: httpx.Response(200, json={'code': 401, 'message': 'sensitive-provider-output'}))
    with pytest.raises(MilvusStorageError, match='Milvus storage operation failed') as caught:
        client.search(NAME, [1., 0.], 1)
    assert 'sensitive' not in str(caught.value)
