"""Real embedding HTTP + independent process vector indexing and recovery."""
import hashlib
import json
import sqlite3
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from concurrent.futures import ThreadPoolExecutor

from test_knowledge_platform_package_runtime import Runtime


class Embeddings:
    def __init__(self):
        owner=self;self.calls=[];self.fail=False;self.block=False
        self.entered=threading.Event();self.release=threading.Event()
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args):pass
            def do_POST(self):
                value=json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.calls.append(value)
                if owner.block:
                    owner.entered.set();owner.release.wait(10)
                body=({'error':'fixture failure'} if owner.fail else {'data':[
                    {'index':i,'embedding':[1.0,0.0] if ('automobile' in text or text=='vehicle') else [0.0,1.0]}
                    for i,text in enumerate(value['input'])]})
                encoded=json.dumps(body).encode();self.send_response(500 if owner.fail else 200)
                self.send_header('Content-Type','application/json');self.send_header('Content-Length',str(len(encoded)));self.end_headers()
                try:self.wfile.write(encoded)
                except (BrokenPipeError,ConnectionResetError):pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def close(self):
        self.release.set();self.server.shutdown();self.server.server_close();self.thread.join()


def test_vector_rebuild_query_restart_failure_and_killed_rebuild(tmp_path):
    embedding=Embeddings();runtime=Runtime(tmp_path/'runtime')
    runtime.source.write_text('An automobile has four wheels.')
    animal=runtime.root/'animal.md';animal.write_text('Cats are small mammals.')
    (runtime.root/'files.json').write_text(json.dumps({'version':1,'collection_id':'portable_files','parsers':[{'id':'native'}],
        'bindings':[{'id':'input','path':str(runtime.source),'space_id':'space_kb_default'},
                    {'id':'animal','path':str(animal),'space_id':'space_kb_default'}]}))
    config={'version':1,'provider_id':'knowledge_local_vector','space_ids':['space_kb_default'],
        'embedding':{'endpoint':f'http://127.0.0.1:{embedding.server.server_port}/embeddings','model':'fixture-vector',
            'dimension':2,'api_key_env':None},'batch_size':16,'max_chars':1200}
    config_path=runtime.root/'index.json';config_path.write_text(json.dumps(config));runtime.extra_args=['--index-config',str(config_path)]
    try:
        runtime.start()
        for aid,path,binding in [('car',runtime.source,'input'),('animal',animal,'animal')]:
            result=runtime.call('/v1/assets:upload',{'asset_id':aid,'space_id':'space_kb_default','title':aid,
                'filename':path.name,'mime_type':'text/markdown','binding_id':binding,
                'content_digest':'sha256:'+hashlib.sha256(path.read_bytes()).hexdigest(),'idempotency_key':aid})
            assert result['status']=='ok',result
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
            version=db.execute("SELECT version FROM knowledge_datasets WHERE id='portable_files'").fetchone()[0]
        request={'space_id':'space_kb_default','collection_id':'portable_files','collection_version':version,
            'capability':'document_rag_query','provider_id':'knowledge_local_vector','idempotency_key':'index-once'}
        built=runtime.call('/v1/indexes:rebuild',request)
        assert built['status']=='ok' and built['data']['index']['active'],built
        calls=len(embedding.calls)
        replay=runtime.call('/v1/indexes:rebuild',request)
        assert replay['status']=='ok' and replay['data']['index']['idempotent'],replay
        assert len(embedding.calls)==calls
        query={'query':'vehicle','space_id':'space_kb_default','collection_id':'portable_files','capability_hint':'document_rag_query','limit':1}
        def check():
            found=runtime.call('/v1/knowledge/query',query)
            assert found['status']=='ok' and found['evidence'],found
            assert 'automobile' in found['evidence'][0]['quote'],found
            mcp=runtime.call('/mcp',{'jsonrpc':'2.0','id':1,'method':'tools/call','params':{'name':'knowledge_query','arguments':query}})
            assert not mcp.get('error') and not mcp['result'].get('isError'),mcp
            assert mcp['result']['structuredContent']['status']=='ok',mcp
        check()
        runtime.stop();runtime.start();check()
        embedding.fail=True
        failed=runtime.call('/v1/indexes:rebuild',{**request,'idempotency_key':'bad-embedding'})
        assert failed['status']=='error',failed
        embedding.fail=False;check()
        embedding.block=True
        with ThreadPoolExecutor(max_workers=1) as pool:
            pending=pool.submit(runtime.call,'/v1/indexes:rebuild',{**request,'idempotency_key':'crash-retry'})
            assert embedding.entered.wait(5)
            runtime.proc.kill();runtime.proc.wait(timeout=5);runtime.log.close();runtime.proc=None
            embedding.block=False;embedding.release.set()
            try:pending.result(timeout=5)
            except OSError:pass
        runtime.start();check()
        retried=runtime.call('/v1/indexes:rebuild',{**request,'idempotency_key':'crash-retry'})
        assert retried['status']=='ok',retried
        runtime.source.write_text('An automobile now has updated specifications.')
        updated=runtime.call('/v1/assets:upload',{'asset_id':'car','space_id':'space_kb_default','title':'car',
            'filename':runtime.source.name,'mime_type':'text/markdown','binding_id':'input',
            'content_digest':'sha256:'+hashlib.sha256(runtime.source.read_bytes()).hexdigest(),'idempotency_key':'car-update'})
        assert updated['status']=='ok',updated
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
            new_version=db.execute("SELECT version FROM knowledge_datasets WHERE id='portable_files'").fetchone()[0]
        retried=runtime.call('/v1/indexes:rebuild',{**request,'collection_version':new_version,'idempotency_key':'after-update'})
        assert retried['status']=='ok',retried
        check()
        direct=runtime.call('/v1/document-rag/query',{'query':'vehicle','space_id':'space_kb_default','limit':1})
        assert direct['status']=='ok' and direct['evidence'],direct
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
            db.execute("UPDATE knowledge_local_vector_chunks SET text='forged' WHERE generation=?",(retried['data']['index']['generation'],))
        rejected=runtime.call('/v1/knowledge/query',query)
        assert rejected['status']=='error',rejected
    finally:runtime.stop();embedding.close()


def test_package_replay_retains_ingestion_ownership_after_index(tmp_path):
    from test_knowledge_platform_local_package_import import _package
    archive=_package(tmp_path,space='space_kb_default',body=b'An automobile has four wheels.')
    embedding=Embeddings();runtime=Runtime(tmp_path/'runtime',imports=[{'id':'restore','path':str(archive),
        'digest':'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()}])
    config=runtime.root/'index.json';config.write_text(json.dumps({'version':1,'provider_id':'knowledge_local_vector',
        'space_ids':['space_kb_default'],'embedding':{'endpoint':f'http://127.0.0.1:{embedding.server.server_port}/embeddings',
            'model':'fixture-vector','dimension':2,'api_key_env':None},'batch_size':16,'max_chars':1200}))
    runtime.extra_args=['--index-config',str(config)]
    try:
        runtime.start()
        body={'package_ref':'restore','idempotency_key':'restore'}
        imported=runtime.call('/v1/packages:import',body);assert imported['status']=='ok',imported
        request={'space_id':'space_kb_default','collection_id':'collection_1','collection_version':'1',
            'capability':'wiki_query','provider_id':'knowledge_local_vector','idempotency_key':'package-index'}
        indexed=runtime.call('/v1/indexes:rebuild',request);assert indexed['status']=='ok',indexed
        replay=runtime.call('/v1/packages:import',body);assert replay['status']=='ok' and replay['data']['idempotent'],replay
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
            raw=db.execute("SELECT binding_json FROM knowledge_collection_bindings WHERE collection_id='collection_1' AND capability='wiki_query'").fetchone()[0]
            assert json.loads(raw)=={'provider_id':'knowledge_package'}
        runtime.stop();runtime.start()
        indexed_again=runtime.call('/v1/indexes:rebuild',request)
        assert indexed_again['status']=='ok' and indexed_again['data']['index']['idempotent'],indexed_again
        query={'query':'vehicle','space_id':'space_kb_default','collection_id':'collection_1','capability_hint':'wiki_query','limit':1}
        found=runtime.call('/v1/knowledge/query',query);assert found['status']=='ok' and found['evidence'],found
        # Publication revocation can occur while Catalog Asset metadata remains.
        with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:db.execute("UPDATE knowledge_package_imports SET status='revoked'")
        denied=runtime.call('/v1/knowledge/query',query);assert denied['status']=='error',denied
    finally:runtime.stop();embedding.close()
