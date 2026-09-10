"""Explicit Milvus REST vector storage; Catalog retains all citation authority.

Remote collections are immutable, unique rebuild products. No drop/delete API is
exposed: failed staging can leave an unreachable collection for later owned GC.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import secrets
from urllib.parse import urlsplit

import httpx


_NAME = re.compile(r'^pkv_[0-9a-f]{48}$')
_MAX_COMPONENTS = 2_000_000
_MAX_RESPONSE = 32 * 1024 * 1024


class MilvusStorageError(RuntimeError):
    pass


class MilvusVectorStore:
    def __init__(self, *, endpoint, dimension, api_key='', timeout=30, client=None):
        parsed = urlsplit(endpoint) if isinstance(endpoint, str) else None
        if (parsed is None or parsed.scheme not in {'http', 'https'} or not parsed.hostname
            or parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment
            or parsed.path not in {'', '/'} or endpoint != endpoint.strip()
            or len(endpoint) > 2048 or any(ord(c) < 33 for c in endpoint)):
            raise ValueError('Invalid Milvus endpoint')
        try:
            parsed.port
        except ValueError:
            raise ValueError('Invalid Milvus endpoint port') from None
        if type(dimension) is not int or not 1 <= dimension <= 16384:
            raise ValueError('Invalid Milvus dimension')
        if not isinstance(api_key, str) or len(api_key) > 4096 or api_key != api_key.strip() or any(ord(c) < 32 for c in api_key):
            raise ValueError('Invalid Milvus credential')
        if type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 120:
            raise ValueError('Invalid Milvus timeout')
        self.endpoint, self.dimension = endpoint.rstrip('/'), dimension
        self._api_key, self._timeout, self._client = api_key, timeout, client
        self.identity = 'sha256:' + hashlib.sha256(json.dumps(
            [self.endpoint, dimension, 'milvus-rest-v2-cosine-v1'], separators=(',', ':')).encode()).hexdigest()

    def _call(self, operation, body):
        owned = self._client is None
        client = self._client or httpx.Client(timeout=self._timeout, trust_env=False, follow_redirects=False)
        headers = {'Content-Type': 'application/json', 'Request-Timeout': str(int(self._timeout))}
        if self._api_key:
            headers['Authorization'] = 'Bearer ' + self._api_key
        try:
            with client.stream('POST', self.endpoint + '/v2/vectordb/' + operation, json=body, headers=headers) as response:
                response.raise_for_status()
                chunks = []; size = 0
                for chunk in response.iter_bytes():
                    size += len(chunk)
                    if size > _MAX_RESPONSE:
                        raise MilvusStorageError('Milvus response exceeds verification bound')
                    chunks.append(chunk)
                result = json.loads(b''.join(chunks))
            if not isinstance(result, dict) or type(result.get('code')) is not int or result['code'] != 0:
                raise MilvusStorageError('Milvus operation refused')
            return result.get('data')
        except Exception as error:
            # Never expose URL, credentials or arbitrary provider response text.
            raise MilvusStorageError('Milvus storage operation failed') from error
        finally:
            if owned:
                client.close()

    def _vectors(self, vectors):
        if not isinstance(vectors, (list, tuple)) or not 1 <= len(vectors) <= 10000 or len(vectors) * self.dimension > _MAX_COMPONENTS:
            raise MilvusStorageError('Invalid Milvus vector count')
        for vector in vectors:
            if (not isinstance(vector, (list, tuple)) or len(vector) != self.dimension
                or any(type(x) not in (int, float) or not math.isfinite(x) or abs(x) > 1.000001 for x in vector)
                or not math.isclose(math.hypot(*vector), 1, rel_tol=1e-5, abs_tol=1e-6)):
                raise MilvusStorageError('Invalid normalized Milvus vector')
        return vectors

    @staticmethod
    def _name(name):
        if not isinstance(name, str) or not _NAME.fullmatch(name):
            raise MilvusStorageError('Invalid owned Milvus collection')
        return name

    def prepare(self, vectors, *, identity):
        vectors = self._vectors(vectors)
        if not isinstance(identity, str) or not re.fullmatch(r'sha256:[0-9a-f]{64}', identity):
            raise MilvusStorageError('Invalid Milvus rebuild identity')
        name = 'pkv_' + secrets.token_hex(24)
        self._call('collections/create', {'collectionName': name, 'dimension': self.dimension,
            'metricType': 'COSINE', 'params': {'consistencyLevel': 'Strong'}})
        # Keep each insertion bounded even at the largest configured dimension.
        batch = max(1, min(128, 100000 // self.dimension))
        for start in range(0, len(vectors), batch):
            rows = [{'id': i, 'vector': list(vectors[i]), 'rebuild_identity': identity}
                for i in range(start, min(start + batch, len(vectors)))]
            result = self._call('entities/insert', {'collectionName': name, 'data': rows})
            if not isinstance(result, dict) or type(result.get('insertCount')) is not int or result['insertCount'] != len(rows):
                raise MilvusStorageError('Milvus insertion incomplete')
        return name

    def verify(self, name, vectors):
        self._name(name); vectors = self._vectors(vectors)
        # Strong count and bounded primary-key windows prove complete content,
        # including extras/duplicates. Verify actual float32 vectors, not a
        # model-supplied digest or a server-generated "ready" flag.
        count = self._call('entities/query', {'collectionName': name, 'filter': '',
            'outputFields': ['count(*)'], 'consistencyLevel': 'Strong'})
        if (not isinstance(count, list) or len(count) != 1 or not isinstance(count[0],dict)
            or type(count[0].get('count(*)')) is not int or count[0]['count(*)'] != len(vectors)):
            raise MilvusStorageError('Milvus collection count changed')
        batch = max(1, min(128, 100000 // self.dimension))
        for start in range(0, len(vectors), batch):
            end = min(start + batch, len(vectors))
            rows = self._call('entities/query', {'collectionName': name,
                'filter': f'id >= {start} and id < {end}', 'outputFields': ['id', 'vector'],
                'limit': end - start, 'consistencyLevel': 'Strong'})
            if not isinstance(rows, list) or len(rows) != end - start:
                raise MilvusStorageError('Milvus collection is incomplete')
            seen = set()
            for row in rows:
                ordinal = row.get('id') if isinstance(row, dict) else None
                if type(ordinal) is not int or not start <= ordinal < end or ordinal in seen:
                    raise MilvusStorageError('Milvus collection IDs changed')
                seen.add(ordinal)
                vector = row.get('vector')
                self._vectors([vector])
                if any(not math.isclose(a, b, rel_tol=1e-6, abs_tol=1e-7) for a, b in zip(vector, vectors[ordinal])):
                    raise MilvusStorageError('Milvus collection content changed')

    def search(self, name, vector, limit):
        self._name(name); self._vectors([vector])
        if type(limit) is not int or not 1 <= limit <= 50:
            raise MilvusStorageError('Invalid Milvus search limit')
        rows = self._call('entities/search', {'collectionName': name, 'data': [list(vector)],
            'limit': limit, 'outputFields': ['id'], 'consistencyLevel': 'Strong'})
        if not isinstance(rows, list) or len(rows) > limit:
            raise MilvusStorageError('Invalid Milvus search results')
        result = []; seen = set()
        for row in rows:
            ordinal = row.get('id') if isinstance(row, dict) else None
            score = row.get('distance') if isinstance(row, dict) else None
            if (type(ordinal) is not int or ordinal < 0 or ordinal in seen
                or type(score) not in (int, float) or not math.isfinite(score) or not -1.00001 <= score <= 1.00001):
                raise MilvusStorageError('Invalid Milvus search identity or score')
            seen.add(ordinal); result.append((ordinal, float(score)))
        return result
