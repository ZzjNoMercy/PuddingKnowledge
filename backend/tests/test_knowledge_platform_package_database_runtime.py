"""Package evidence rebound to an owned temporary PostgreSQL, across restart."""
import hashlib
import json
import sqlite3
import shutil
import zipfile

from knowledge_platform.package.builder import KnowledgePackageBuilder, export_package_zip
from knowledge_platform.local.package_database import source_input_digest
from test_knowledge_platform_package_runtime import Runtime
import test_knowledge_platform_local_database_process as pg


def database_package(root):
    source={'id':'sales_evidence','space_id':'space_kb_default','dataset_id':'database_sales','dialect':'postgresql',
        'ddl':[{'id':'ddl','content':'CREATE TABLE sales(amount integer);'}],
        'documentation':[{'id':'doc','content':'Sales amount in units.'}],
        'sql_examples':[{'id':'sum','question':'sales total','sql':'SELECT SUM(amount) AS total FROM sales'},
                        {'id':'write','question':'delete sales','sql':'DELETE FROM sales'}],
        'entities':[{'id':'amount','canonical_name':'amount','entity_type':'measure','table_column':'sales.amount','aliases':['sales amount']}]}
    result=KnowledgePackageBuilder().build(output_dir=root/'package',package_id='sales',version='1',
        spaces=[{'id':'space_kb_default','name':'Default','description':''}],
        collections=[{'id':'portable_database','space_id':'space_kb_default','name':'Sales','version':'1','kind':'database',
            'asset_ids':[],'semantic_asset_ids':[],'database_source_ids':['sales_evidence'],
            'capabilities':['database_nl2sql','database_schema','database_execute_readonly']}],
        assets=[],asset_files={},capabilities=['database_nl2sql','database_schema','database_execute_readonly'],
        catalog_revision='sha256:'+'0'*64,database_sources=[source])
    archive=root/'database.zip';export_package_zip(root/'package',archive)
    normalized=json.loads((root/'package/database/index.json').read_text())['sources'][0]
    return archive,result.package_revision,source_input_digest(normalized)


def test_package_database_rebind_restart_execute_and_reexport(tmp_path,monkeypatch):
    def replay(root,env,pgport,port,launcher):
        archive,revision,input_digest=database_package(root)
        runtime=Runtime(root/'runtime',imports=[{'id':'database','path':str(archive),
            'digest':'sha256:'+hashlib.sha256(archive.read_bytes()).hexdigest()}])
        try:
            runtime.start()
            imported=runtime.call('/v1/packages:import',{'package_ref':'database','idempotency_key':'database-once'})
            assert imported['status']=='ok',imported
            with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
                assert db.execute("SELECT COUNT(*) FROM knowledge_collection_bindings WHERE collection_id='portable_database'").fetchone()[0]==0
            runtime.stop()
            shutil.rmtree(root/'package')
            config=runtime.root/'database.json'
            config.write_text(json.dumps({'format':'knowledge-local-database/v2','collection_id':'portable_database','collection_version':'1',
                'source':{'dataset_id':'database_sales','host':'127.0.0.1','port':pgport,'database':'postgres',
                    'username':'knowledge_reader','allowed_tables':['sales'],'password_env':None},
                'vanna':{'package_source_id':'sales_evidence','package_revision':revision,'input_digest':input_digest}}))
            runtime.extra_args=['--database-config',str(config)]
            for turn in range(2):
                runtime.start()
                schema=runtime.call('/v1/database/schema?space_id=space_kb_default&dataset_id=database_sales')
                assert schema['status']=='ok',schema
                for transport in ['rest','mcp']:
                    scope={'space_id':'space_kb_default','dataset_id':'database_sales'}
                    def call(name,path,body):
                        if transport=='rest':return runtime.call(path,body)
                        mcp=runtime.call('/mcp',{'jsonrpc':'2.0','id':name,'method':'tools/call',
                            'params':{'name':name,'arguments':body}})
                        assert not mcp.get('error') and not mcp['result'].get('isError'),mcp
                        return mcp['result']['structuredContent']
                    plan=call('database_nl2sql','/v1/database/nl2sql',{**scope,'question':'sales total'})
                    assert plan['status']=='ok',plan
                    pid=plan['data']['query_plan']['query_plan_id']
                    result=call('database_execute_readonly',f'/v1/database/query-plans/{pid}:execute',
                        {'query_plan_id':pid,'space_id':'space_kb_default','expected_sql_hash':plan['data']['sql_hash'],'page_size':10})
                    assert result['status']=='ok' and result['data']['rows']==[{'total':30}],result
                for question,space in [('delete sales','space_kb_default'),('sales total','wrong_space')]:
                    denied=runtime.call('/v1/database/nl2sql',{'question':question,'space_id':space,'dataset_id':'database_sales'})
                    assert denied['status']=='error',denied
                replayed=runtime.call('/v1/packages:import',{'package_ref':'database','idempotency_key':'database-once'})
                assert replayed['status']=='ok' and replayed['data']['idempotent'],replayed
                runtime.stop()
            runtime.start()
            exported=runtime.call('/v1/packages:export',{'output_ref':'output','package_id':'sales-copy','version':'1',
                'collections':[{'id':'portable_database','version':'1'}]})
            assert exported['status']=='ok',exported
            with zipfile.ZipFile(runtime.archive) as z:
                sources=json.loads(z.read('database/index.json'))['sources']
                collections=json.loads(z.read('collections/index.json'))['collections']
                assert sources[0]['id']=='sales_evidence' and len(sources)==1
                assert collections[0]['database_source_ids']==['sales_evidence']
            old_plan=runtime.call('/v1/database/nl2sql',{'question':'sales total','space_id':'space_kb_default','dataset_id':'database_sales'})
            assert old_plan['status']=='ok',old_plan
            with sqlite3.connect(runtime.root/'state/catalog.sqlite3') as db:
                db.execute("UPDATE knowledge_package_database_sources SET content_digest=? WHERE id='sales_evidence'",('sha256:'+'1'*64,))
            denied=runtime.call('/v1/database/nl2sql',{'question':'sales total','space_id':'space_kb_default','dataset_id':'database_sales'})
            assert denied['status']=='error',denied
            pid=old_plan['data']['query_plan']['query_plan_id']
            stale=runtime.call(f'/v1/database/query-plans/{pid}:execute', {'space_id':'space_kb_default','expected_sql_hash':old_plan['data']['sql_hash'],'page_size':10})
            assert stale['status']=='error',stale
        finally:runtime.stop()
    monkeypatch.setattr(pg,'_replay',replay)
    pg.test_independent_database_and_wiki_runtime(tmp_path,False)
