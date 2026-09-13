"""Owned transactional Wiki authoring state, separate from the single-draft API.

The ledger is an internal application port. Catalog/read/query projection and
multi-page model orchestration are subsequent integrations, not implied here.
"""
from __future__ import annotations
import sqlite3
from pathlib import Path
from ..wiki.patch import WikiPatch, plan_patch, workspace_revision, canonical, digest, validate_patch_shape
from ..wiki.lint import lint_workspace


class WikiAuthoringStore:
    def __init__(self, *, database: Path, space_id: str, bundle, raw_hashes, raw_manifest_sha256, catalog_projection=False):
        self.catalog_projection=catalog_projection
        self.database=database;self.space_id=space_id;self.bundle=bundle
        self.raw=dict(raw_hashes);self.raw_manifest=raw_manifest_sha256

    @classmethod
    def from_owned_workspace(cls, owned):
        from .wiki_schema_publication import SchemaPublication
        source=SchemaPublication(owned,owned['catalog'])
        store=cls(database=Path(owned['catalog']),space_id=source.space_id,bundle=source.bundle,
                  raw_hashes=source.raw,raw_manifest_sha256=source.raw_manifest_sha256,catalog_projection=True)
        with store._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            existing=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='knowledge_wiki_authoring_state'").fetchone()
            if existing and db.execute('SELECT 1 FROM knowledge_wiki_authoring_state WHERE space_id=?',(store.space_id,)).fetchone():
                if 'catalog_projected' not in {row[1] for row in db.execute('PRAGMA table_info(knowledge_wiki_authoring_state)')}:
                    db.execute('ALTER TABLE knowledge_wiki_authoring_state ADD COLUMN catalog_projected INTEGER NOT NULL DEFAULT 0')
                revision,pages,index,log=store._read(db,allow_unprojected=True)
                if not store.catalog_projection:
                    from .wiki_authoring_projection import project
                    project(db,store.space_id,store.bundle,pages,revision,initial=True)
                    db.execute('UPDATE knowledge_wiki_authoring_state SET catalog_projected=1 WHERE space_id=?',(store.space_id,))
                    store.catalog_projection=True
                return store
            source.initialize(db)
            pages=source._current(db)
            index=source.index+'\n'+''.join(f'- [[{s}]]\n' for s in sorted(set(pages)-set(source.pages)))
            store._initialize(db,pages=pages,index=index,log=source.log)
        return store

    def _connect(self):
        # Caller binds an owned database; never discover a path from model data.
        from .wiki import _safe_database
        connection=sqlite3.connect(_safe_database(self.database),timeout=5)
        return connection

    def initialize(self, *, pages, index, log):
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            return self._initialize(db,pages=pages,index=index,log=log)

    def _initialize(self, db, *, pages, index, log):
        report=lint_workspace(contract=self.bundle.lint_contract(),pages=pages,index=index,log_present=True,raw_hashes=self.raw,raw_manifest_sha256=self.raw_manifest)
        if not report['ok']:raise ValueError('Initial Wiki authoring state fails lint')
        revision=workspace_revision(self.bundle,pages,index,log,self.raw,self.raw_manifest)
        db.execute('CREATE TABLE IF NOT EXISTS knowledge_wiki_authoring_state (space_id TEXT PRIMARY KEY, revision TEXT NOT NULL, index_text TEXT NOT NULL, log_text TEXT NOT NULL, catalog_projected INTEGER NOT NULL DEFAULT 0)')
        db.execute('CREATE TABLE IF NOT EXISTS knowledge_wiki_authoring_pages (space_id TEXT NOT NULL, slug TEXT NOT NULL, markdown TEXT NOT NULL, PRIMARY KEY(space_id,slug))')
        db.execute('CREATE TABLE IF NOT EXISTS knowledge_wiki_authoring_commits (space_id TEXT NOT NULL, operation_id TEXT NOT NULL, request_digest TEXT NOT NULL, previous_revision TEXT NOT NULL, revision TEXT NOT NULL, retired_json TEXT NOT NULL, patch_json TEXT NOT NULL, before_json TEXT NOT NULL, receipt_digest TEXT NOT NULL, PRIMARY KEY(space_id,operation_id))')
        if db.execute('SELECT 1 FROM knowledge_wiki_authoring_state WHERE space_id=?',(self.space_id,)).fetchone():raise ValueError('Wiki authoring workspace already initialized')
        db.execute('INSERT INTO knowledge_wiki_authoring_state VALUES (?,?,?,?,?)',(self.space_id,revision,index,log,int(self.catalog_projection)))
        db.executemany('INSERT INTO knowledge_wiki_authoring_pages VALUES (?,?,?)',[(self.space_id,s,t) for s,t in pages.items()])
        if self.catalog_projection:
            from .wiki_authoring_projection import project
            project(db,self.space_id,self.bundle,pages,revision,initial=True)
        return revision

    def _read(self,db,*,allow_unprojected=False):
        sizes=db.execute('SELECT length(CAST(index_text AS BLOB)),length(CAST(log_text AS BLOB)),typeof(index_text),typeof(log_text) FROM knowledge_wiki_authoring_state WHERE space_id=?',(self.space_id,)).fetchone()
        if sizes is not None and (sizes[2:]!=('text','text') or max(sizes[:2])>8*1024*1024):raise ValueError('Wiki authoring text budget exceeded')
        row=db.execute('SELECT revision,index_text,log_text FROM knowledge_wiki_authoring_state WHERE space_id=?',(self.space_id,)).fetchone()
        if row is None:raise ValueError('Wiki authoring workspace unavailable')
        sizes=db.execute('SELECT length(CAST(markdown AS BLOB)) FROM knowledge_wiki_authoring_pages WHERE space_id=? LIMIT 5001',(self.space_id,)).fetchall()
        if len(sizes)>5000 or any(r[0]>8*1024*1024 for r in sizes) or sum(r[0] for r in sizes)>64*1024*1024:raise ValueError('Wiki authoring page budget exceeded')
        pages=dict(db.execute('SELECT slug,markdown FROM knowledge_wiki_authoring_pages WHERE space_id=?',(self.space_id,)))
        if workspace_revision(self.bundle,pages,row[1],row[2],self.raw,self.raw_manifest)!=row[0]:raise ValueError('Wiki authoring state commitment mismatch')
        columns={item[1] for item in db.execute('PRAGMA table_info(knowledge_wiki_authoring_state)')}
        flag=db.execute('SELECT catalog_projected FROM knowledge_wiki_authoring_state WHERE space_id=?',(self.space_id,)).fetchone()[0] if 'catalog_projected' in columns else 0
        if type(flag) is not int or flag not in (0,1):raise ValueError('Invalid authoring projection mode')
        if self.catalog_projection and not flag and not allow_unprojected:raise ValueError("Authoring projection cannot be disabled")
        self.catalog_projection=bool(flag)
        if self.catalog_projection:
            from .wiki_authoring_projection import verify_projection
            verify_projection(db,self.space_id,self.bundle,pages,row[0])
        return row[0],pages,row[1],row[2]

    def read(self):
        with self._connect() as db:
            db.execute('BEGIN')
            return self._read(db)

    def apply(self,patch: WikiPatch, *, operation_id: str, _after_page=None):
        # _after_page is a test-only crash injection hook, never an event port.
        validate_patch_shape(patch)
        import re
        if not isinstance(operation_id,str) or not re.fullmatch(r'[a-zA-Z0-9._-]{1,160}',operation_id):raise ValueError('Invalid authoring operation identity')
        with self._connect() as db:
            db.execute('BEGIN IMMEDIATE')
            revision,pages,index,log=self._read(db)
            # Full immutable request bytes support exact retry after crash/restart.
            request=canonical({'revision':patch.expected_revision,'changes':[{'slug':c.slug,'markdown':c.markdown,'expected_digest':c.expected_digest,'replacement':c.replacement} for c in patch.changes],'selected_raw':patch.selected_raw,'index':patch.index,'log_entry':patch.log_entry})
            previous=db.execute('SELECT request_digest,revision,patch_json,previous_revision,retired_json,before_json,receipt_digest FROM knowledge_wiki_authoring_commits WHERE space_id=? AND operation_id=?',(self.space_id,operation_id)).fetchone()
            if previous is not None:
                receipt=digest(canonical([operation_id,previous[0],previous[3],previous[1],previous[4],previous[5],previous[2]]))
                if previous[6]!=receipt:raise ValueError('Authoring commit receipt mismatch')
                if previous[0]!=digest(request) or previous[2]!=request:raise ValueError('Authoring operation identity reused')
                return previous[1]
            planned=plan_patch(bundle=self.bundle,pages=pages,index=index,log=log,raw_hashes=self.raw,raw_manifest_sha256=self.raw_manifest,patch=patch)
            for change in patch.changes:
                if change.markdown is None:db.execute('DELETE FROM knowledge_wiki_authoring_pages WHERE space_id=? AND slug=?',(self.space_id,change.slug))
                else:db.execute('INSERT INTO knowledge_wiki_authoring_pages VALUES (?,?,?) ON CONFLICT(space_id,slug) DO UPDATE SET markdown=excluded.markdown',(self.space_id,change.slug,change.markdown))
                if _after_page is not None:_after_page(change.slug)
            db.execute('UPDATE knowledge_wiki_authoring_state SET revision=?,index_text=?,log_text=? WHERE space_id=?',(planned.revision,planned.index,planned.log,self.space_id))
            if self.catalog_projection:
                from .wiki_authoring_projection import project
                project(db,self.space_id,self.bundle,dict(planned.pages),planned.revision)
            retired=canonical(planned.retired)
            before=canonical([(c.slug,pages.get(c.slug)) for c in patch.changes])
            receipt=digest(canonical([operation_id,planned.request_digest,revision,planned.revision,retired,before,request]))
            db.execute('INSERT INTO knowledge_wiki_authoring_commits VALUES (?,?,?,?,?,?,?,?,?)',(self.space_id,operation_id,planned.request_digest,revision,planned.revision,retired,request,before,receipt))
            return planned.revision

    def read_published(self, resource_uri):
        from .wiki_authoring_projection import asset_id
        revision,pages,index,log=self.read()
        for slug,markdown in pages.items():
            if resource_uri==f"knowledge://spaces/{self.space_id}/assets/{asset_id(self.space_id,slug)}":
                return markdown.encode('utf-8')
        raise LookupError('Current Wiki resource unavailable')
