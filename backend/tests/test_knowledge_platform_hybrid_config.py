import copy
import json

import pytest

from knowledge_platform.local.index_config import load_index_config,ranking_client,validate_retrieval


def ranking():
    return {'candidate_limit':20,'vector_weight':1.,'bm25_weight':1.,'rrf_k':60,'rerank':None}


@pytest.mark.parametrize('field,value',[('candidate_limit',True),('candidate_limit',51),('rrf_k',0),
    ('vector_weight',float('nan')),('bm25_weight',-1),('rerank',{}),('unexpected',True)])
def test_invalid_query_only_settings_rejected(field,value):
    value_dict=ranking();value_dict[field]=value
    with pytest.raises(ValueError):validate_retrieval(value_dict)


def test_zero_channels_missing_secret_and_explicit_no_auth(monkeypatch):
    config=ranking();config['vector_weight']=config['bm25_weight']=0
    with pytest.raises(ValueError):validate_retrieval(config)
    config=ranking();config['rerank']={'protocol':'dashscope','endpoint':'http://127.0.0.1:1/rerank','model':'gte-rerank-v2','api_key_env':None}
    assert ranking_client({'retrieval':config}) is not None
    config['rerank']['api_key_env']='KNOWLEDGE_RERANK_TEST_KEY'
    monkeypatch.delenv('KNOWLEDGE_RERANK_TEST_KEY',raising=False)
    with pytest.raises(ValueError,match='missing'):ranking_client({'retrieval':config})
    config['rerank']['api_key_env']='OPENAI_API_KEY'
    with pytest.raises(ValueError,match='reference'):validate_retrieval(config)


def test_optional_ranking_does_not_change_existing_file_config(tmp_path):
    from test_knowledge_platform_milvus_index_config import _config
    config=_config();path=tmp_path/'index.json'
    path.write_text(json.dumps(config));assert load_index_config(path)==config
    config['retrieval']=ranking();path.write_text(json.dumps(config));assert load_index_config(path)==config
