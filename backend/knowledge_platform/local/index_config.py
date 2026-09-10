"""Explicit host configuration for local persistent vector indexes."""
from __future__ import annotations
import json
import os
import re
from pathlib import Path

from knowledge_platform.retrieval.embedding import OpenAICompatibleEmbeddingClient

_ID = re.compile(r'^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$')
_ENV = re.compile(r'^KNOWLEDGE_EMBEDDING_[A-Z0-9_]{1,100}$')


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
    if (not isinstance(value,dict) or set(value)!={'version','provider_id','space_ids','embedding','batch_size','max_chars'}
            or type(value['version']) is not int or value['version']!=1 or value['provider_id']!='knowledge_local_vector'):
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
    return value


def embedding_client(config):
    embedding=config['embedding'];ref=embedding['api_key_env']
    if ref is not None and ref not in os.environ:
        raise ValueError('Explicit embedding credential environment is missing')
    return OpenAICompatibleEmbeddingClient(endpoint=embedding['endpoint'],model=embedding['model'],
        dimension=embedding['dimension'],batch_size=config['batch_size'],api_key=os.environ[ref] if ref else '',timeout=30)
