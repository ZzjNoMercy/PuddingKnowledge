import hashlib,json,sqlite3
from pathlib import Path
import pytest
from knowledge_platform.distribution import wiki_archive as archive
from knowledge_platform.distribution.wiki_schema_evidence import capture_schema_evidence,CUSTOM,BRAIN
from knowledge_platform.local.workspace import open_persistent_workspace
from knowledge_platform.local.wiki import build_wiki_services
from knowledge_platform.wiki.compiler import WikiCompilationRequest
from knowledge_platform.wiki.ports import WikiDraft


def prepare(tmp_path):
    fixture=json.loads((Path(__file__).parent/'fixtures/wiki-schema-legacy.json').read_text())
    brain=tmp_path/'brain';brain.mkdir()
    for rel,key in [(CUSTOM,'custom_yaml'),(BRAIN,'brain_yaml'),('AGENTS.md','agents_markdown')]:
        p=brain/rel;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(fixture[key])
    (brain/'wiki').mkdir();(brain/'wiki/index.md').write_text('# Index\n');(brain/'wiki/log.md').write_text('# Log\n')
    (brain/'raw').mkdir();raw=b'The source describes a concept.';digest=hashlib.sha256(raw).hexdigest()
    (brain/'raw/source.md').write_bytes(raw)
    (brain/'raw/manifest.jsonl').write_text(json.dumps({'snapshot_path':'source.md','sha256':digest,'size_bytes':len(raw)})+'\n')
    candidate=tmp_path/'archive';archive.prepare_wiki_archive(brain,candidate)
    files={}
    for name,text in fixture['catalog_yaml'].items():
        path=tmp_path/(name+'.yaml');path.write_text(text);files[name]=path
    evidence=tmp_path/'schema.json';evidence.write_bytes(capture_schema_evidence(candidate,files,expected_bundle_hash=fixture['expected_bundle_hash']));evidence.chmod(0o600)
    state=tmp_path/'state'
    with open_persistent_workspace(state,wiki_archive=candidate,schema_evidence=evidence) as owned:pass
    snapshot_id=next(iter(owned['raw_bindings']));space=owned['space_id']
    request=WikiCompilationRequest(snapshot_id,'sha256:'+digest,f'knowledge://spaces/{space}/assets/{snapshot_id}','sha256:'+digest,'compile-one')
    config={'version':2,'space_id':space,'assets':{snapshot_id:str(owned['raw_bindings'][snapshot_id])},'model':{'endpoint':'http://127.0.0.1:1','model':'fixture'}}
    return owned,state,config,request


def markdown(body='A source-supported concept.'):
    return '---\ntitle: Concept\ntype: concept\nsources: [source.md]\ncreated: 2026-09-13\nupdated: 2026-09-13\nschema_version: 0.1.0\n---\n# Concept\n\n'+body


class Model:
    def __init__(self,request,text):self.request=request;self.text=text;self.calls=0
    async def generate(self,*,context,snapshot):
        self.calls+=1
        assert json.loads(context)['snapshot_path']=='source.md'
        return WikiDraft(path='wiki/concepts/new.md',title='Concept',markdown=self.text,source_snapshot_id=snapshot.snapshot_id,source_revision=snapshot.source_revision)


@pytest.mark.asyncio
async def test_schema_publication_atomic_success_restart_and_replay(tmp_path):
    owned,state,config,request=prepare(tmp_path)
    services=build_wiki_services(config,owned['catalog'],state/'processing',schema_workspace=owned)
    model=Model(request,markdown());services.wiki_compilation._model=model
    result=await services.wiki_compilation.compile(request)
    assert services.read_published(result.resource_uri)==markdown().encode()
    with sqlite3.connect(owned['catalog']) as db:
        assert db.execute('SELECT count(*) FROM knowledge_local_wiki_schema_pages').fetchone()[0]==1
        assert db.execute('SELECT count(*) FROM knowledge_local_wiki_schema_log').fetchone()[0]==1
    with open_persistent_workspace(state) as restarted:pass
    second=build_wiki_services(config,restarted['catalog'],state/'processing',schema_workspace=restarted)
    second.wiki_compilation._model=model
    assert (await second.wiki_compilation.compile(request)).resource_uri==result.resource_uri
    assert model.calls==1


@pytest.mark.asyncio
@pytest.mark.parametrize('text',[markdown('[[concepts/missing]]'),markdown().replace('type: concept','type: absent'),markdown().replace('sources: [source.md]','sources: [other.md]')])
async def test_invalid_schema_draft_leaves_no_publication(tmp_path,text):
    owned,state,config,request=prepare(tmp_path)
    services=build_wiki_services(config,owned['catalog'],state/'processing',schema_workspace=owned)
    services.wiki_compilation._model=Model(request,text)
    with pytest.raises((ValueError,RuntimeError)):await services.wiki_compilation.compile(request)
    with sqlite3.connect(owned['catalog']) as db:
        for table in ('knowledge_local_wiki_schema_pages','knowledge_local_wiki_schema_log'):
            assert db.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM knowledge_assets WHERE source_type='local_wiki_compilation'").fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM knowledge_local_wiki_compilations WHERE status='succeeded'").fetchone()[0]==0


@pytest.mark.asyncio
async def test_later_publication_failure_rolls_back_schema_projection(tmp_path,monkeypatch):
    owned,state,config,request=prepare(tmp_path)
    services=build_wiki_services(config,owned['catalog'],state/'processing',schema_workspace=owned)
    worker=services.wiki_compilation;worker._model=Model(request,markdown())
    def fail(*args):raise ValueError('collection ownership conflict')
    monkeypatch.setattr(worker._publisher,'_publish_collection',fail)
    with pytest.raises(ValueError,match='collection ownership'):await worker.compile(request)
    with sqlite3.connect(owned['catalog']) as db:
        assert db.execute('SELECT count(*) FROM knowledge_local_wiki_schema_pages').fetchone()[0]==0
        assert db.execute('SELECT count(*) FROM knowledge_local_wiki_schema_log').fetchone()[0]==0
        assert db.execute("SELECT count(*) FROM knowledge_assets WHERE source_type='local_wiki_compilation'").fetchone()[0]==0


def test_receipt_binds_source_content_and_title(tmp_path):
    from dataclasses import replace
    from knowledge_platform.local.wiki_schema_publication import SchemaPublication
    from knowledge_platform.wiki.ports import RawSnapshot
    owned,state,config,request=prepare(tmp_path);validator=SchemaPublication(owned,owned['catalog'])
    snapshot=RawSnapshot(request.snapshot_id,request.source_revision,request.source_uri,'The source describes a concept.',request.content_digest)
    draft=WikiDraft('wiki/concepts/new.md','Concept',markdown(),request.snapshot_id,request.source_revision)
    receipt=validator.receipt(draft,snapshot)
    assert validator.receipt(replace(draft,title='Changed title'),snapshot)!=receipt
    with pytest.raises(ValueError,match='Raw content'):validator.receipt(draft,replace(snapshot,content='Forged content'))


@pytest.mark.asyncio
async def test_committed_payload_tamper_cannot_replay_success(tmp_path):
    owned,state,config,request=prepare(tmp_path)
    services=build_wiki_services(config,owned['catalog'],state/'processing',schema_workspace=owned)
    worker=services.wiki_compilation;worker._model=Model(request,markdown())
    await worker.compile(request)
    with sqlite3.connect(owned['catalog']) as db:
        db.execute("UPDATE knowledge_local_wiki_compilations SET markdown=X'00' WHERE status='succeeded'")
    with pytest.raises(ValueError,match='committed compilation'):await worker.compile(request)
