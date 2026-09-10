"""Export an actual typed Catalog table, import it elsewhere, and query it."""
import hashlib
import json
import zipfile

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeStructuredAsset, KnowledgeDataset, KnowledgeCollectionBinding
from test_knowledge_platform_package_runtime import Runtime


@pytest.mark.parametrize('extension', ['csv', 'tsv', 'xlsx'])
def test_native_table_export_import_mcp_and_restart(tmp_path, extension):
    source = Runtime(tmp_path / 'source'); target = None
    table = source.root / ('sales.' + extension)
    sheet = None
    if extension in {'csv', 'tsv'}:
        table.write_text('region,amount\nNorth,12\nSouth,18\n'.replace(',', '\t' if extension == 'tsv' else ','))
    else:
        from openpyxl import Workbook
        book = Workbook(); book.active.title = 'Wrong'; book.active.append(['wrong']); book.active.append([999])
        selected = book.create_sheet('Sales'); selected.append(['region', 'amount']); selected.append(['North', 12]); selected.append(['South', 18])
        book.save(table); sheet = 'Sales'
    digest = 'sha256:' + hashlib.sha256(table.read_bytes()).hexdigest()
    engine = create_engine(f'sqlite:///{source.seed}')
    with Session(engine) as session, session.begin():
        session.add(KnowledgeStructuredAsset(id='sales', space_id='space_kb_default', source_key='sales',
            source_type='local_file', file_name=table.name, sheet_name=sheet, size_bytes=table.stat().st_size,
            source_uri='knowledge://spaces/space_kb_default/structured-assets/sales/source', content_digest=digest,
            profile_status='ready', reference_status='ready', capabilities=['table_query'], columns_json=['region','amount'],
            row_count=2, column_count=2))
        session.add(KnowledgeDataset(id='sales_collection', space_id='space_kb_default', name='Sales', version='1',
            kind='table', asset_ids=['sales'], capabilities=['table_query'], freshness={'status':'fresh'}))
        session.add(KnowledgeCollectionBinding(space_id='space_kb_default', collection_id='sales_collection',
            collection_version='1', capability='table_query', binding_json={'asset_id':'sales'}))
    engine.dispose()
    config = source.root / 'structured.json'
    config.write_text(json.dumps({'version':1,'space_id':'space_kb_default','assets':{'sales':str(table)}}))
    source.extra_args = ['--structured-config', str(config)]
    try:
        source.start()
        query = {'query':'sum amount','space_id':'space_kb_default','asset_id':'sales','limit':5}
        result = source.call('/v1/table/query', query)
        assert result['status'] == 'ok' and result['evidence'], result
        exported = source.call('/v1/packages:export', {'output_ref':'output','package_id':'sales','version':'1',
            'collections':[{'id':'sales_collection','version':'1'}]})
        assert exported['status'] == 'ok', exported
        target = Runtime(tmp_path / 'target', imports=[{'id':'restore','path':str(source.archive),
            'digest':'sha256:'+hashlib.sha256(source.archive.read_bytes()).hexdigest()}])
        target.start()
        imported = target.call('/v1/packages:import', {'package_ref':'restore','idempotency_key':'tables-once'})
        assert imported['status'] == 'ok', imported
        source.stop(); table.unlink()
        for turn in range(2):
            result = target.call('/v1/table/query', query)
            assert result['status'] == 'ok' and result['evidence'], result
            assert result['data']['tables'][0]['columns'] == ['region','amount'], result
            routed = target.call('/v1/knowledge/query', {'query':'sum amount','space_id':'space_kb_default',
                'collection_id':'sales_collection','capability_hint':'table_query','limit':5})
            assert routed['status'] == 'ok' and routed['evidence'], routed
            mcp = target.call('/mcp', {'jsonrpc':'2.0','id':1,'method':'tools/call',
                'params':{'name':'table_query','arguments':query}})
            assert not mcp.get('error'), mcp
            tool = mcp['result']
            assert not tool.get('isError'), tool
            assert tool['structuredContent']['status'] == 'ok', tool
            assert tool['structuredContent']['evidence'], tool
            rows = result['data']['tables'][0]['preview_rows']
            assert [row['region'] for row in rows] == ['North', 'South'], result
            assert sum(float(row['amount']) for row in rows) == 30, result
            if turn == 0: target.stop(); target.start()
        replay = target.call('/v1/packages:import', {'package_ref':'restore','idempotency_key':'tables-once'})
        assert replay['status']=='ok' and replay['data']['idempotent'], replay
        exported_again = target.call('/v1/packages:export', {'output_ref':'output','package_id':'sales-copy','version':'1',
            'collections':[{'id':'sales_collection','version':'1'}]})
        assert exported_again['status'] == 'ok', exported_again
        with zipfile.ZipFile(target.archive) as archive:
            assets = json.loads(archive.read('assets/index.json'))
            assert assets['assets'][0].get('sheet_name') == sheet, assets
    finally:
        source.stop()
        if target is not None: target.stop()
