"""Real HTTP ranking providers and independent runtime, optional real Milvus."""
import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from test_knowledge_platform_package_runtime import Runtime
from test_knowledge_platform_milvus_runtime import milvus


class Models:
    def __init__(self):
        self.mode='ok';self.fail_embedding=False;self.calls=[];self.revoke=None;owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append((self.path,value))
                if self.path=='/embeddings':
                    body={'data':[{'index':i,'embedding':[1.,0.]} for i,_ in enumerate(value['input'])]}
                else:
                    docs=value['input']['documents']
                    ordered=sorted(range(len(docs)),key=lambda i:('General' not in (docs[i].get('text','') if isinstance(docs[i],dict) else docs[i]),i))
                    rows=[{'index':i,'relevance_score':.99-j*.2,'document':{'text':'FORGED REMOTE CONTENT'}} for j,i in enumerate(ordered[:value['parameters']['top_n']])]
                    if owner.mode=='bad_id':rows[0]['index']=len(docs)
                    if owner.mode=='duplicate' and len(rows)>1:rows[1]['index']=rows[0]['index']
                    if owner.mode=='short':rows=[]
                    if owner.revoke:owner.revoke()
                    body={'output':{'results':rows}}
                encoded=json.dumps(body).encode();self.send_response(503 if (owner.mode=='failure' and self.path=='/rerank') or (owner.fail_embedding and self.path=='/embeddings') else 200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers();self.wfile.write(encoded)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):
        self.server.shutdown();self.server.server_close();self.thread.join()


@pytest.mark.parametrize('provider',['local','milvus'])
def test_hybrid_fusion_rerank_restart_and_untrusted_results(tmp_path,request,provider):
    endpoint=request.getfixturevalue('milvus')[0] if provider=='milvus' else None
    models=Models();runtime=Runtime(tmp_path/'runtime')
    runtime.source.write_text('General archive note.')
    target=runtime.root/'target.md';target.write_text('ZXQ42 calibration note.')
    (runtime.root/'files.json').write_text(json.dumps({'version':1,'collection_id':'portable_files','parsers':[{'id':'native'}],
        'bindings':[{'id':'input','path':str(runtime.source),'space_id':'space_kb_default'},
            {'id':'target','path':str(target),'space_id':'space_kb_default'}]}))
    config={'version':1,'provider_id':'knowledge_'+('milvus' if endpoint else 'local')+'_vector','space_ids':['space_kb_default'],
        'embedding':{'endpoint':f'http://127.0.0.1:{models.server.server_port}/embeddings','model':'fixture','dimension':2,'api_key_env':None},
        'batch_size':16,'max_chars':1200,
        'retrieval':{'candidate_limit':2,'vector_weight':1.,'bm25_weight':2.,'rrf_k':60,'rerank':None}}
    if endpoint:config['milvus']={'endpoint':endpoint,'api_key_env':None}
    config_path=runtime.root/'index.json';config_path.write_text(json.dumps(config));runtime.extra_args=['--index-config',str(config_path)]
    try:
        runtime.start()
        for aid,path,binding in [('a_noise',runtime.source,'input'),('b_target',target,'target')]:
            uploaded=runtime.call('/v1/assets:upload',{'asset_id':aid,'space_id':'space_kb_default','title':aid,
                'filename':path.name,'mime_type':'text/markdown','binding_id':binding,
                'content_digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest(),'idempotency_key':aid})
            assert uploaded['status']=='ok',uploaded
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
            version=db.execute("SELECT version FROM knowledge_datasets WHERE id='portable_files'").fetchone()[0]
        rebuild={'space_id':'space_kb_default','collection_id':'portable_files','collection_version':version,
            'capability':'document_rag_query','provider_id':config['provider_id'],'idempotency_key':'hybrid-once'}
        indexed=runtime.call('/v1/indexes:rebuild',rebuild);assert indexed['status']=='ok',indexed
        query={'query':'ZXQ42','space_id':'space_kb_default','collection_id':'portable_files','limit':1}
        fused=runtime.call('/v1/knowledge/query',query)
        assert fused['status']=='ok' and 'ZXQ42' in fused['evidence'][0]['quote'],fused
        # Query-only ranking configuration changes must not need a new index.
        runtime.stop();config['retrieval']['rerank']={'protocol':'dashscope',
            'endpoint':f'http://127.0.0.1:{models.server.server_port}/rerank','model':'qwen3-vl-rerank','api_key_env':None}
        config_path.write_text(json.dumps(config));runtime.start()
        replay=runtime.call('/v1/indexes:rebuild',rebuild)
        assert replay['status']=='ok' and replay['data']['index']['idempotent'],replay
        reranked=runtime.call('/v1/knowledge/query',query)
        assert reranked['status']=='ok' and 'General' in reranked['evidence'][0]['quote'],reranked
        assert 'FORGED' not in json.dumps(reranked)
        payload=[body for path,body in models.calls if path=='/rerank'][-1]
        assert payload['parameters']['return_documents'] is False
        assert all(isinstance(doc,dict) and set(doc)=={'text'} for doc in payload['input']['documents'])
        mcp=runtime.call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'knowledge_query','arguments':query}})
        assert mcp['result']['structuredContent']['status']=='ok',mcp
        direct=runtime.call('/v1/document-rag/query',{'query':'ZXQ42','space_id':'space_kb_default','limit':1})
        assert direct['status']=='ok' and 'General' in direct['evidence'][0]['quote'],direct
        for mode in ('bad_id','duplicate','short','failure'):
            models.mode=mode
            rejected=runtime.call('/v1/knowledge/query',{**query,'limit':2})
            assert rejected['status']=='error',rejected
            assert runtime.call('/v1/document-rag/query',{'query':'ZXQ42','space_id':'space_kb_default','limit':2})['status']=='error'
        models.mode='ok'
        runtime.stop()
        saved_rerank=config['retrieval']['rerank']
        config['retrieval'].update(vector_weight=0,rerank=None)
        config_path.write_text(json.dumps(config));models.fail_embedding=True;runtime.start()
        lexical_only=runtime.call('/v1/knowledge/query',query)
        assert lexical_only['status']=='ok' and 'ZXQ42' in lexical_only['evidence'][0]['quote'],lexical_only
        runtime.stop();models.fail_embedding=False
        config['retrieval'].update(vector_weight=1,rerank=saved_rerank)
        config_path.write_text(json.dumps(config));runtime.start()
        assert runtime.call('/v1/knowledge/query',query)['status']=='ok'
        def revoke_publication():
            with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
                db.execute("UPDATE knowledge_ingestion_jobs SET status='failed' WHERE kind='file_import'")
        models.revoke=revoke_publication
        assert runtime.call('/v1/knowledge/query',query)['status']=='error'
    finally:
        runtime.stop();models.close()
