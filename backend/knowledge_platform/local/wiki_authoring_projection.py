"""Current Wiki Catalog projection, committed in the authoring transaction."""
from __future__ import annotations
import hashlib,json
from datetime import datetime,timezone
from ..wiki.lint import _frontmatter
from ..wiki.patch import digest,canonical

SOURCE='local_wiki_authoring'
DESCRIPTION='local_wiki_authoring/v1'


def asset_id(space,slug):return 'wiki_active_'+hashlib.sha256((space+':'+slug).encode()).hexdigest()[:32]
def collection_id(space):return 'collection_active_wiki_'+hashlib.sha256(space.encode()).hexdigest()[:32]


def facts(space,bundle,pages,revision):
    result={}
    for slug,markdown in pages.items():
        front,_=_frontmatter(markdown,source=slug);identifier=asset_id(space,slug)
        result[identifier]=(space,'wiki_page',str(front['title']),SOURCE,f'knowledge://spaces/{space}/assets/{identifier}',
            'sha256:'+digest(markdown),'sha256:'+digest(markdown),canonical({'slug':slug,'schema_bundle_hash':bundle.bundle_hash,'authoring_revision':revision}), '{}')
    return result


def verify_projection(db,space,bundle,pages,revision):
    expected=facts(space,bundle,pages,revision)
    rows=db.execute('SELECT id,space_id,kind,title,source_type,source_uri,revision,content_digest,metadata_json,permissions_json FROM knowledge_assets WHERE space_id=? AND source_type=? LIMIT 5001',(space,SOURCE)).fetchall()
    if {row[0]:tuple(row[1:]) for row in rows}!=expected:raise ValueError('Active Wiki Catalog does not match authoring state')
    row=db.execute('SELECT space_id,version,kind,description,asset_ids,semantic_asset_ids,capabilities,permissions_json,manifest_digest FROM knowledge_datasets WHERE id=?',(collection_id(space),)).fetchall()
    target=(space,revision,'wiki',DESCRIPTION,canonical(sorted(expected)),'[]','["wiki_query"]','{}','sha256:'+revision)
    if len(row)!=1 or tuple(row[0])!=target:raise ValueError('Active Wiki Collection does not match authoring state')


def project(db,space,bundle,pages,revision,*,initial=False):
    expected=facts(space,bundle,pages,revision);cid=collection_id(space)
    now=datetime.now(timezone.utc).isoformat()
    if initial and (db.execute('SELECT 1 FROM knowledge_assets WHERE space_id=? AND source_type=?',(space,SOURCE)).fetchone() or db.execute('SELECT 1 FROM knowledge_datasets WHERE id=?',(cid,)).fetchone()):raise ValueError('Active Wiki projection already owned')
    for identifier in expected:
        existing=db.execute('SELECT space_id,source_type FROM knowledge_assets WHERE id=?',(identifier,)).fetchone()
        if existing is not None and tuple(existing)!=(space,SOURCE):raise ValueError('Active Wiki Asset identity collision')
    # Previous projection is verified by the caller before mutation. Historical
    # archive/compilation Assets remain immutable under their original URIs.
    for (identifier,) in db.execute('SELECT id FROM knowledge_assets WHERE space_id=? AND source_type=?',(space,SOURCE)).fetchall():
        if identifier not in expected:db.execute('DELETE FROM knowledge_assets WHERE id=?',(identifier,))
    for identifier,values in expected.items():
        db.execute('''INSERT INTO knowledge_assets
            (id,space_id,kind,title,source_type,source_uri,revision,content_digest,metadata_json,permissions_json,description,mime_type,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,'','text/markdown',?,?)
            ON CONFLICT(id) DO UPDATE SET title=excluded.title,revision=excluded.revision,content_digest=excluded.content_digest,metadata_json=excluded.metadata_json,updated_at=excluded.updated_at''',(identifier,*values,now,now))
    db.execute('DELETE FROM knowledge_datasets WHERE id=?',(cid,))
    db.execute('''INSERT INTO knowledge_datasets
        (id,space_id,name,version,kind,description,asset_ids,semantic_asset_ids,capabilities,freshness,permissions_json,manifest_digest,created_at,updated_at)
        VALUES (?,?, 'Current Wiki',?,'wiki',?,?,'[]','["wiki_query"]',?,'{}',?,?,?)''',
        (cid,space,revision,DESCRIPTION,canonical(sorted(expected)),canonical({'mode':'current_wiki_authoring','state':'ready'}),'sha256:'+revision,now,now))
    verify_projection(db,space,bundle,pages,revision)


class AuthoringReaderServices:
    def __init__(self, authoring, previous=None):
        self.authoring,self.previous=authoring,previous
        self.source_types=(SOURCE,'local_wiki_compilation') if previous is not None else (SOURCE,)

    def search_snapshot(self,space_id):
        if space_id!=self.authoring.space_id:return ()
        with self.authoring._connect() as db:
            db.execute('BEGIN')
            revision,pages,index,log=self.authoring._read(db)
            rows=db.execute('SELECT id,title,source_uri,content_digest,kind,source_type FROM knowledge_assets WHERE space_id=? AND source_type=? ORDER BY id',(space_id,SOURCE)).fetchall()
            bodies={f'knowledge://spaces/{space_id}/assets/{asset_id(space_id,slug)}':body.encode() for slug,body in pages.items()}
            result=[(dict(zip(('id','title','source_uri','content_digest','kind','source_type'),row)),bodies[row[2]]) for row in rows]
            if self.previous is not None:
                old=db.execute("SELECT id,title,source_uri,content_digest,kind,source_type FROM knowledge_assets WHERE space_id=? AND source_type='local_wiki_compilation' ORDER BY id LIMIT 5001",(space_id,)).fetchall()
                if len(old)>5000:raise ValueError('Historical Wiki result budget exceeded')
                total=0
                for row in old:
                    size=db.execute("SELECT length(markdown) FROM knowledge_local_wiki_compilations WHERE space_id=? AND resource_uri=? AND status='succeeded' LIMIT 1",(space_id,row[2])).fetchone()
                    if size is None or size[0]>8*1024*1024:raise ValueError('Historical Wiki bytes unavailable')
                    total+=size[0]
                    if total>64*1024*1024:raise ValueError('Historical Wiki result budget exceeded')
                    data=db.execute("SELECT markdown FROM knowledge_local_wiki_compilations WHERE space_id=? AND resource_uri=? AND status='succeeded' LIMIT 1",(space_id,row[2])).fetchone()[0]
                    result.append((dict(zip(('id','title','source_uri','content_digest','kind','source_type'),row)),bytes(data)))
            return tuple(result)

    def read_published(self,uri):
        try:return self.authoring.read_published(uri)
        except LookupError:
            if self.previous is None:raise
            return self.previous.read_published(uri)
