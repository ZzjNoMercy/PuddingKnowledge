"""Explicit host configuration for local persistent vector indexes."""
from __future__ import annotations
import json
import math
import os
import re
from urllib.parse import urlsplit
from pathlib import Path

from knowledge_platform.retrieval.embedding import OpenAICompatibleEmbeddingClient

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
_ENV = re.compile(r'^KNOWLEDGE_EMBEDDING_[A-Z0-9_]{1,100}$')
_MILVUS_ENV = re.compile(r'^KNOWLEDGE_MILVUS_[A-Z0-9_]{1,100}$')


def _validate_milvus_endpoint(endpoint: object) -> str:
    if not isinstance(endpoint, str) or not endpoint or endpoint != endpoint.strip():
        raise ValueError('Milvus endpoint is invalid')
    try:
        parsed = urlsplit(endpoint)
        port = parsed.port
    except ValueError as error:
        raise ValueError('Milvus endpoint is invalid') from error
    if parsed.scheme not in {'http', 'https'} or not parsed.hostname or any(ord(c) < 33 for c in endpoint) or len(endpoint)>2048:
        raise ValueError('Milvus endpoint is invalid')
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError('Milvus endpoint is invalid')
    if parsed.path not in {'', '/'}:
        raise ValueError('Milvus endpoint must use the root path')
    return endpoint


def load_index_config(path: Path) -> dict:
    if not path.is_absolute() or any(p.is_symlink() for p in (path, *path.parents)):
        raise ValueError('Index configuration must be an absolute regular file')
    with path.open('rb') as stream:raw=stream.read(65537)
    if len(raw)>65536:raise ValueError('Index configuration exceeds bound')
    def unique(pairs):
        value={}
        for key,item in pairs:
            if key in value:raise ValueError('Duplicate Index configuration field')
            value[key]=item
        return value
    value=json.loads(raw,object_pairs_hook=unique)
    if not isinstance(value, dict) or type(value.get('version')) is not int or value['version'] != 1:
        raise ValueError('Index configuration fields are invalid')
    provider_id = value.get('provider_id')
    if provider_id == 'knowledge_local_vector':
        if set(value)-{'retrieval'} != {'version','provider_id','space_ids','embedding','batch_size','max_chars'}:
            raise ValueError('Index configuration fields are invalid')
    elif provider_id == 'knowledge_milvus_vector':
        if set(value)-{'retrieval'} != {'version','provider_id','space_ids','embedding','batch_size','max_chars','milvus'}:
            raise ValueError('Index configuration fields are invalid')
        milvus = value['milvus']
        if not isinstance(milvus, dict) or set(milvus) != {'endpoint', 'api_key_env'}:
            raise ValueError('Milvus configuration is invalid')
        _validate_milvus_endpoint(milvus['endpoint'])
        if milvus['api_key_env'] is not None and (not isinstance(milvus['api_key_env'], str) or not _MILVUS_ENV.fullmatch(milvus['api_key_env'])):
            raise ValueError('Milvus credential reference is invalid')
    else:
        raise ValueError('Index configuration fields are invalid')
    spaces=value['space_ids']
    if not isinstance(spaces,list) or not 1<=len(spaces)<=1000 or any(not isinstance(s,str) or not _ID.fullmatch(s) for s in spaces) or len(spaces)!=len(set(spaces)):
        raise ValueError('Index Space bindings are invalid')
    embedding=value['embedding']
    if not isinstance(embedding,dict) or set(embedding)!={'endpoint','model','dimension','api_key_env'}:
        raise ValueError('Embedding configuration is invalid')
    ref=embedding['api_key_env']
    if ref is not None and (not isinstance(ref,str) or not _ENV.fullmatch(ref)):
        raise ValueError('Embedding credential reference is invalid')
    if type(value['batch_size']) is not int or not 1<=value['batch_size']<=256:
        raise ValueError('Index batch size is invalid')
    if type(value['max_chars']) is not int or not 100<=value['max_chars']<=12000:
        raise ValueError('Index chunk size is invalid')
    # Pure validation; this constructor does not contact the endpoint.
    OpenAICompatibleEmbeddingClient(endpoint=embedding['endpoint'],model=embedding['model'],
        dimension=embedding['dimension'],batch_size=value['batch_size'])
    if 'retrieval' in value: validate_retrieval(value['retrieval'])
    return value


def embedding_client(config):
    embedding=config['embedding'];ref=embedding['api_key_env']
    if ref is not None and ref not in os.environ:
        raise ValueError('Explicit embedding credential environment is missing')
    return OpenAICompatibleEmbeddingClient(endpoint=embedding['endpoint'],model=embedding['model'],
        dimension=embedding['dimension'],batch_size=config['batch_size'],api_key=os.environ[ref] if ref else '',timeout=30)


def vector_storage(config):
    """Construct the configured provider storage without contacting it."""
    if config['provider_id'] == 'knowledge_local_vector':
        return None
    from knowledge_platform.retrieval.milvus_http import MilvusVectorStore
    milvus = config['milvus']
    ref = milvus['api_key_env']
    if ref is not None and ref not in os.environ:
        raise ValueError('Explicit Milvus credential environment is missing')
    return MilvusVectorStore(
        endpoint=milvus['endpoint'],
        dimension=config['embedding']['dimension'],
        api_key=os.environ[ref] if ref else '',
    )


def validate_retrieval(value):
    if not isinstance(value,dict) or set(value)!={'candidate_limit','vector_weight','bm25_weight','rrf_k','rerank'}:
        raise ValueError('Hybrid retrieval configuration fields are invalid')
    if type(value['candidate_limit']) is not int or not 1<=value['candidate_limit']<=50:
        raise ValueError('Hybrid candidate limit is invalid')
    if type(value['rrf_k']) is not int or not 1<=value['rrf_k']<=1000:
        raise ValueError('Hybrid RRF constant is invalid')
    weights=[value['vector_weight'],value['bm25_weight']]
    if any(type(w) not in (int,float) or not math.isfinite(w) or not 0<=w<=100 for w in weights) or not sum(weights)>0:
        raise ValueError('Hybrid channel weights are invalid')
    rerank=value['rerank']
    if rerank is not None:
        if not isinstance(rerank,dict) or set(rerank)!={'protocol','endpoint','model','api_key_env'} or rerank['protocol']!='dashscope':
            raise ValueError('Rerank configuration fields are invalid')
        ref=rerank['api_key_env']
        if ref is not None and (not isinstance(ref,str) or not re.fullmatch(r'KNOWLEDGE_RERANK_[A-Z0-9_]{1,100}',ref)):
            raise ValueError('Rerank credential reference is invalid')
        from knowledge_platform.retrieval.rerank import DashScopeReranker
        DashScopeReranker(endpoint=rerank['endpoint'],model=rerank['model'])
    return value


def ranking_client(config):
    if 'retrieval' not in config:return None
    rerank=validate_retrieval(config['retrieval'])['rerank']
    if rerank is None:return None
    ref=rerank['api_key_env']
    if ref is not None and ref not in os.environ:
        raise ValueError('Explicit rerank credential environment is missing')
    from knowledge_platform.retrieval.rerank import DashScopeReranker
    return DashScopeReranker(endpoint=rerank['endpoint'],model=rerank['model'],api_key=os.environ[ref] if ref else '')
