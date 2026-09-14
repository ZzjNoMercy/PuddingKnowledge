import asyncio,copy,hashlib,json,sqlite3,struct,zlib
from pathlib import Path
import httpx,pytest
from fastapi import FastAPI
from knowledge_contracts import Principal
from knowledge_platform.catalog import SqliteCatalogQueryRepository
from knowledge_platform.local.app import _build_app
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.document_resources import DocumentResourceService
from knowledge_platform.transport.fastapi_document_resources_router import create_document_resources_router
from test_forward_document_dependencies import dependency_fixture,run


def png_bytes():
    def chunk(kind,data):return struct.pack('!I',len(data))+kind+data+struct.pack('!I',zlib.crc32(kind+data)&0xffffffff)
    return b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR',struct.pack('!IIBBBBB',16,16,8,2,0,0,0))+chunk(b'IDAT',zlib.compress((b'\0'+b'\xff\x40\x40'*16)*16))+chunk(b'IEND',b'')


def resource_fixture(tmp_path):
    args=dependency_fixture(tmp_path);image=png_bytes();(args[1]/'images/pic#x.png').write_bytes(image)
    with sqlite3.connect(args[0]) as db:
        metadata=json.loads(db.execute('SELECT doc_metadata FROM knowledge_documents').fetchone()[0]);metadata['assets'][0].update(sha256=hashlib.sha256(image).hexdigest(),size_bytes=len(image))
        db.execute('UPDATE knowledge_documents SET doc_metadata=?',(json.dumps(metadata),))
    run(args);state=tmp_path/'state'
    with open_persistent_workspace(state,document_migration=args[2]) as payload:pass
    return args,state,payload,image


def who(*spaces):return Principal('reader',scopes=('knowledge.read',*(f'knowledge.space:{space}' for space in spaces)))


def request(app,path):
    async def call():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:return await client.get(path)
    return asyncio.run(call())


def test_composed_http_lists_and_reads_verified_png(tmp_path):
    args,state,payload,image=resource_fixture(tmp_path);identity=next(iter(payload['document_bindings']))
    app=_build_app(SqliteCatalogQueryRepository(payload['catalog']),payload['file_bindings'],who('space_kb-1'),document_bindings=payload['document_bindings'],document_resources=payload['document_resources'])
    response=request(app,f'/v1/assets/{identity}/resources');assert response.status_code==200
    rows=response.json()['data']['resources'];item=next(row for row in rows if row['mime_type']=='image/png')
    assert item['name']=='pic#x.png' and '/private/' not in response.text
    binary=request(app,item['url']);assert binary.content==image and binary.headers['content-type']=='image/png'
    assert binary.headers['x-content-type-options']=='nosniff' and binary.headers['cache-control']=='no-store' and 'sandbox' in binary.headers['content-security-policy']
    assert request(app,f'/v1/assets/{identity}/resources/not-a-path').status_code==404
    (state/'resources/images/pic#x.png').write_bytes(b'changed')
    assert request(app,item['url']).status_code==409


@pytest.mark.parametrize('principal',[Principal('no-scope'),who('another-space'),Principal('tenant',scopes=('knowledge.admin',),tenant_id='other')])
def test_resource_list_requires_read_and_space_scope(tmp_path,principal):
    args,state,payload,_=resource_fixture(tmp_path);identity=next(iter(payload['document_bindings']))
    service=DocumentResourceService(SqliteCatalogQueryRepository(payload['catalog']),payload['document_resources']);app=FastAPI();app.include_router(create_document_resources_router(service,principal_provider=lambda:principal))
    assert request(app,f'/v1/assets/{identity}/resources').status_code==403


def test_dependency_link_does_not_bypass_other_document_space(tmp_path):
    args,state,payload,image=resource_fixture(tmp_path);binding=copy.deepcopy(payload['document_resources']);identity=next(iter(binding['bindings']))
    private='private-document';binding['bindings'][private]='docs/linked.md'
    binding['facts']['assets'][private]={'id':private,'space_id':'private-space','source_uri':f'knowledge://spaces/private-space/assets/{private}','content_digest':'sha256:'+binding['tree']['files']['docs/linked.md']['sha256']}
    class Repo:
        catalog_revision='sha256:'+'a'*64
        def get_asset(self,*,asset_id):return binding['facts']['assets'].get(asset_id)
    principal=[who('space_kb-1','private-space')];service=DocumentResourceService(Repo(),binding);app=FastAPI();app.include_router(create_document_resources_router(service,principal_provider=lambda:principal[0]))
    rows=request(app,f'/v1/assets/{identity}/resources').json()['data']['resources'];private_rows=[r for r in rows if r['name'] in ('linked.md','data.csv')];assert len(private_rows)==2
    principal[0]=who('space_kb-1')
    visible=request(app,f'/v1/assets/{identity}/resources').json()['data']['resources'];assert [r['name'] for r in visible]==['pic#x.png']
    for row in private_rows:assert request(app,row['url']).status_code==403


def test_symlink_swap_after_start_is_rejected(tmp_path):
    args,state,payload,image=resource_fixture(tmp_path);identity=next(iter(payload['document_bindings']))
    service=DocumentResourceService(SqliteCatalogQueryRepository(payload['catalog']),payload['document_resources']);app=FastAPI();app.include_router(create_document_resources_router(service,principal_provider=lambda:who('space_kb-1')))
    row=next(r for r in request(app,f'/v1/assets/{identity}/resources').json()['data']['resources'] if r['mime_type']=='image/png')
    path=state/'resources/images/pic#x.png';path.unlink();path.symlink_to(args[1]/'images/pic#x.png')
    assert request(app,row['url']).status_code==409


def test_non_image_bytes_are_download_only(tmp_path):
    args,state,payload,_=resource_fixture(tmp_path);identity=next(iter(payload['document_bindings']))
    service=DocumentResourceService(SqliteCatalogQueryRepository(payload['catalog']),payload['document_resources']);app=FastAPI();app.include_router(create_document_resources_router(service,principal_provider=lambda:who('space_kb-1')))
    row=next(r for r in request(app,f'/v1/assets/{identity}/resources').json()['data']['resources'] if r['name']=='linked.md')
    response=request(app,row['url']);assert response.status_code==200 and response.headers['content-type']=='application/octet-stream' and response.headers['content-disposition'].startswith('attachment;')
