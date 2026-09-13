import json
import pytest
from knowledge_platform.local.wiki_patch_model import HttpWikiPatchModel


def envelope(content='{}',finish='stop',**message):
    return json.dumps({'choices':[{'finish_reason':finish,'message':{'content':content,**message}}]}).encode()


def gateway(monkeypatch,response):
    calls=[]
    class Response:
        def __enter__(self):return self
        def __exit__(self,*args):pass
        def read(self,n):return response[:n]
    class Opener:
        def open(self,request,timeout):calls.append(request);return Response()
    def build(*handlers):
        assert len(handlers)==2 and handlers[0].proxies=={}
        assert handlers[1].redirect_request(None,None,None,None,None,None) is None
        return Opener()
    monkeypatch.setattr('urllib.request.build_opener',build)
    return HttpWikiPatchModel({'endpoint':'http://127.0.0.1:1','model':'fixture'}),calls


@pytest.mark.parametrize('raw',[envelope(finish='length'),envelope(tool_calls=[{}]),envelope(function_call={'name':'tool'}),envelope(refusal='no'),envelope(content=''),envelope(content='{"changes":[],"changes":[]}'),b'{"choices":[],"choices":[]}',envelope(content='{"x":NaN}')])
def test_gateway_rejects_non_normal_or_ambiguous_results(monkeypatch,raw):
    model,calls=gateway(monkeypatch,raw)
    with pytest.raises(ValueError):model.generate({},'instruction')
    assert len(calls)==1


def test_gateway_budgets_and_credential_preflight(monkeypatch):
    model,calls=gateway(monkeypatch,envelope())
    monkeypatch.setattr('knowledge_platform.local.wiki_patch_model.MAX_CONTEXT',16)
    with pytest.raises(ValueError):model.generate({'raw':'too long'},'instruction')
    assert calls==[]
    monkeypatch.setattr('knowledge_platform.local.wiki_patch_model.MAX_CONTEXT',4096)
    model.config['api_key_env']='WIKI_MISSING_TEST_KEY';monkeypatch.delenv('WIKI_MISSING_TEST_KEY',raising=False)
    with pytest.raises(ValueError):model.generate({},'instruction')
    assert calls==[]
    del model.config['api_key_env'];monkeypatch.setattr('knowledge_platform.local.wiki_patch_model.MAX_RESPONSE',8)
    with pytest.raises(ValueError):model.generate({},'instruction')


def test_gateway_sends_exact_context_without_ambient_proxy_or_redirect(monkeypatch):
    model,calls=gateway(monkeypatch,envelope('{"changes":[],"index":"","log_entry":"note"}'))
    context={'revision':'abc','raw':[{'content':'source data'}]}
    assert model.generate(context,'compile')['log_entry']=='note'
    request=json.loads(calls[0].data)
    assert json.loads(request['messages'][1]['content'])=={'context':context,'instruction':'compile'}
    assert request['stream'] is False
