"""Schema-bound single-draft publication with transactional Wiki projection.

The existing compiler emits one page per request. This adapter preserves that
API while validating the complete projected workspace and committing its page,
index coverage and append-only publication log together. Multi-page patches and
updates/retirements need a separate compiler contract.
"""
from __future__ import annotations
import hashlib
import json
import re
import sqlite3
from pathlib import Path
from ..distribution import wiki_archive as archive
from ..wiki.lint import lint_workspace, _frontmatter
from ..wiki.ports import WikiValidationResult

TABLE='knowledge_local_wiki_schema_pages'
LOG='knowledge_local_wiki_schema_log'


class SchemaPublication:
    def __init__(self, owned, catalog_path):
        self.bundle=owned['schema_bundle'];self.space_id=owned.get('schema_space_id',owned.get('space_id'))
        self.database=Path(catalog_path);self.evidence=Path(owned['evidence_root'])
        self.pages={};self.raw={};self.snapshot_paths={}
        total_page_bytes=0
        with archive._lock(self.evidence,shared=True):
            manifest=archive._verify(self.evidence)
            self.archive_manifest=manifest
            for relative,fact in manifest['files'].items():
                if relative.startswith('wiki/') and relative.endswith('.md'):
                    data,actual=archive._read(self.evidence/'archive'/relative,private=True,limit=8*1024*1024)
                    total_page_bytes+=len(data)
                    if total_page_bytes>64*1024*1024:raise ValueError('Wiki publication workspace exceeds budget')
                    if actual!=fact:raise ValueError('Archived Wiki bytes changed')
                    if relative not in {'wiki/index.md','wiki/log.md'}:self.pages[relative[5:-3]]=data.decode('utf-8')
            self.index=self._text('wiki/index.md')
            self.log=self._text('wiki/log.md')
            raw_manifest=self._text('raw/manifest.jsonl')
            self.raw_manifest_sha256=hashlib.sha256(raw_manifest.encode()).hexdigest()
            for line in raw_manifest.splitlines():
                if line.strip():
                    value=archive._json(line.encode());self.raw[value['snapshot_path']]=value['sha256']
            for asset_id,path in owned['raw_bindings'].items():
                relative=Path(path).relative_to(self.evidence/'archive'/'raw').as_posix()
                if relative not in self.raw:raise ValueError('Unregistered compilation Raw')
                self.snapshot_paths[asset_id]=relative
        if len(self.pages)>5000 or sum(len(p.encode()) for p in self.pages.values())>64*1024*1024:
            raise ValueError('Wiki publication workspace exceeds budget')

    def _text(self,relative):
        raw,fact=archive._read(self.evidence/'archive'/relative,private=True,limit=8*1024*1024)
        if self.archive_manifest['files'].get(relative)!=fact:raise ValueError('Archived Wiki evidence changed')
        return raw.decode('utf-8')

    def initialize(self,connection):
        connection.execute(f'''CREATE TABLE IF NOT EXISTS {TABLE} (
            space_id TEXT NOT NULL, slug TEXT NOT NULL, resource_uri TEXT NOT NULL,
            markdown BLOB NOT NULL, receipt_id TEXT NOT NULL, bundle_hash TEXT NOT NULL,
            closure_sha256 TEXT NOT NULL, PRIMARY KEY(space_id,slug), UNIQUE(resource_uri))''')
        connection.execute(f'''CREATE TABLE IF NOT EXISTS {LOG} (
            space_id TEXT NOT NULL, resource_uri TEXT NOT NULL PRIMARY KEY,
            source_snapshot_id TEXT NOT NULL, source_digest TEXT NOT NULL,
            slug TEXT NOT NULL, receipt_id TEXT NOT NULL)''')

    def _source(self,snapshot):
        if 'sha256:'+hashlib.sha256(snapshot.content.encode('utf-8')).hexdigest()!=snapshot.content_digest:
            raise ValueError('Raw content does not match revision digest')
        path=self.snapshot_paths.get(snapshot.snapshot_id)
        if path is None or snapshot.content_digest!='sha256:'+self.raw[path]:
            raise ValueError('Schema compiler requires an owned registered Raw revision')
        if snapshot.source_revision!=snapshot.content_digest or snapshot.source_uri!=f'knowledge://spaces/{self.space_id}/assets/{snapshot.snapshot_id}':
            raise ValueError('Schema compilation source identity mismatch')
        return path

    def receipt(self,draft,snapshot):
        source=self._source(snapshot)
        if (draft.source_snapshot_id,draft.source_revision)!=(snapshot.snapshot_id,snapshot.source_revision):raise ValueError('Draft source identity mismatch')
        if not re.fullmatch(r'wiki/[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)+\.md',draft.path):raise ValueError('Draft needs a typed Wiki path')
        if len(draft.markdown.encode())>8*1024*1024:raise ValueError('Draft exceeds budget')
        front,_=_frontmatter(draft.markdown,source=draft.path)
        if not isinstance(front.get('sources'),list) or source not in front['sources']:
            raise ValueError('Draft must cite the selected exact Raw snapshot path')
        facts=[self.bundle.bundle_hash,self.bundle.closure_sha256,snapshot.source_uri,snapshot.content_digest,draft.path,draft.title,draft.markdown]
        return 'receipt_'+hashlib.sha256(json.dumps(facts,ensure_ascii=False,separators=(',',':')).encode()).hexdigest()[:48]

    async def validate(self,draft,*,snapshot):
        try:return WikiValidationResult(valid=True,receipt_id=self.receipt(draft,snapshot))
        except ValueError:return WikiValidationResult(valid=False,errors=('schema draft shape or source attribution invalid',))

    def _current(self,connection):
        rows=connection.execute(f'SELECT slug,length(markdown),typeof(markdown) FROM {TABLE} WHERE space_id=? LIMIT 5001',(self.space_id,)).fetchall()
        if len(rows)>5000 or any(r[2]!='blob' or r[1]>8*1024*1024 for r in rows) or sum(r[1] for r in rows)>64*1024*1024:raise ValueError('Published Wiki projection exceeds budget')
        assets=connection.execute("SELECT source_uri FROM knowledge_assets WHERE space_id=? AND source_type='local_wiki_compilation'",(self.space_id,)).fetchall()
        projected=connection.execute(f'SELECT resource_uri FROM {TABLE} WHERE space_id=? LIMIT 5001',(self.space_id,)).fetchall()
        if {r[0] for r in assets}!={r[0] for r in projected}:raise ValueError('Compilation Assets lack schema publication evidence')
        pages=dict(self.pages)
        for slug,raw,uri,receipt,bundle,closure in connection.execute(f'SELECT slug,markdown,resource_uri,receipt_id,bundle_hash,closure_sha256 FROM {TABLE} WHERE space_id=?',(self.space_id,)):
            if slug in pages or bundle!=self.bundle.bundle_hash or closure!=self.bundle.closure_sha256:raise ValueError('Schema projection identity conflict')
            asset=connection.execute('SELECT content_digest,metadata_json FROM knowledge_assets WHERE source_uri=? AND space_id=? AND kind=? AND source_type=?',(uri,self.space_id,'wiki_page','local_wiki_compilation')).fetchall()
            logs=connection.execute(f'SELECT receipt_id,source_snapshot_id,source_digest FROM {LOG} WHERE resource_uri=? AND space_id=? AND slug=?',(uri,self.space_id,slug)).fetchall()
            if len(asset)!=1 or asset[0][0]!='sha256:'+hashlib.sha256(raw).hexdigest() or json.loads(asset[0][1]).get('receipt_id')!=receipt or len(logs)!=1 or logs[0][0]!=receipt:raise ValueError('Schema projection publication evidence mismatch')
            attempts=connection.execute("SELECT markdown,receipt_id,snapshot_id,content_digest FROM knowledge_local_wiki_compilations WHERE space_id=? AND resource_uri=? AND status='succeeded' LIMIT 1001",(self.space_id,uri)).fetchall()
            if not attempts or len(attempts)>1000 or any(tuple(a)!=(raw,receipt,logs[0][1],logs[0][2]) for a in attempts):
                raise ValueError('Schema projection disagrees with committed compilation')
            source_path=self.snapshot_paths.get(logs[0][1])
            if source_path is None or logs[0][2]!='sha256:'+self.raw[source_path]:
                raise ValueError('Schema log source evidence mismatch')
            pages[slug]=raw.decode('utf-8')
        self._lint(pages)
        return pages

    def _lint(self,pages):
        added=sorted(set(pages)-set(self.pages))
        index=self.index+'\n'+''.join(f'- [[{s}]]\n' for s in added)
        report=lint_workspace(contract=self.bundle.lint_contract(),pages=pages,index=index,log_present=True,raw_hashes=self.raw,raw_manifest_sha256=self.raw_manifest_sha256)
        if not report['ok']:raise ValueError('Schema workspace lint rejected publication: '+','.join(sorted({e['code'] for e in report['errors']})))

    async def build_context(self,snapshot):
        source=self._source(snapshot)
        with sqlite3.connect(self.database) as connection:
            connection.execute("BEGIN")
            pages=self._current(connection)
        return json.dumps({'source_text':snapshot.content[:96000],'snapshot_path':source,
            'schema_version':self.bundle.bundle_version,'allowed_page_types':self.bundle.allowed_page_types,
            'required_frontmatter':self.bundle.required_frontmatter,'page_prefixes':dict(self.bundle.page_prefixes),
            'existing_slugs':sorted(pages)},ensure_ascii=False)

    def publish(self,connection,draft,*,snapshot,resource_uri):
        expected=self.receipt(draft.draft,snapshot)
        if expected!=draft.receipt_id:raise ValueError('Schema validation receipt mismatch')
        pages=self._current(connection);slug=draft.draft.path[5:-3]
        existing=connection.execute(f'SELECT resource_uri,markdown,receipt_id FROM {TABLE} WHERE space_id=? AND slug=?',(self.space_id,slug)).fetchone()
        if existing is not None:
            if tuple(existing)!=(resource_uri,draft.draft.markdown.encode(),expected):raise ValueError('Wiki slug already published')
        elif slug in pages:raise ValueError('Wiki slug already exists in archive')
        pages[slug]=draft.draft.markdown
        self._lint(pages)
        if existing is None:
            connection.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?,?,?,?)',(self.space_id,slug,resource_uri,draft.draft.markdown.encode(),expected,self.bundle.bundle_hash,self.bundle.closure_sha256))
            connection.execute(f'INSERT INTO {LOG} VALUES (?,?,?,?,?,?)',(self.space_id,resource_uri,snapshot.snapshot_id,snapshot.content_digest,slug,expected))
