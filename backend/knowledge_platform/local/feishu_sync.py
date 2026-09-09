"""Owned Feishu sync jobs: short fenced Catalog writes around remote I/O."""
from __future__ import annotations

from contextlib import contextmanager
from dataclasses import asdict
import fcntl
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

from sqlalchemy import create_engine, select, text, update
from sqlalchemy.orm import Session
from sqlalchemy.pool import NullPool

from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeSourceItem, KnowledgeSyncRun, utcnow
from knowledge_platform.connector_sync.feishu_source import FeishuSelection, FeishuSource
from knowledge_platform.local.objects import LocalObjectStore


class FeishuSyncError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False)


class FeishuSyncService:
    """One local writer per connector, with database ownership checks on every write.

    The OS lock releases on process death; abandoned running jobs may be retried
    with their original fingerprint. No SQLite transaction spans network calls.
    """
    def __init__(self, catalog: Path, state_root: Path):
        self.engine = create_engine('sqlite:///' + str(catalog), poolclass=NullPool)
        self.objects = LocalObjectStore(state_root / 'objects')
        with self.engine.begin() as connection:
            connection.exec_driver_sql('CREATE TABLE IF NOT EXISTS knowledge_local_object_store (id INTEGER PRIMARY KEY CHECK(id=1), store_id TEXT NOT NULL)')
            connection.execute(text('INSERT OR IGNORE INTO knowledge_local_object_store VALUES (1,:store)'), {'store':self.objects.identity})
            if connection.execute(text('SELECT store_id FROM knowledge_local_object_store WHERE id=1')).scalar_one() != self.objects.identity:
                raise FeishuSyncError('Catalog belongs to another object store')
        locks = state_root / 'feishu-locks'
        if locks.is_symlink(): raise FeishuSyncError('Invalid sync lock root')
        locks.mkdir(mode=0o700, exist_ok=True)
        self._locks_fd = os.open(locks, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def close(self):
        fd = getattr(self, '_locks_fd', None)
        if fd is not None:
            os.close(fd); self._locks_fd = None
        self.objects.close()
        self.engine.dispose()

    @contextmanager
    def _lock(self, identity):
        fd = os.open(_hash(identity)+'.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=self._locks_fd)
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode): raise FeishuSyncError('Invalid sync lock')
            try: fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError: raise FeishuSyncError('Connector sync is already running') from None
            yield
        finally:
            os.close(fd)

    @staticmethod
    def _binding(connector, space_id):
        if connector is None or connector.space_id != space_id or connector.connector_key != 'feishu' or connector.status not in {'ready','active'}:
            raise FeishuSyncError('Feishu connector is unavailable in this Space')
        selection = FeishuSelection(**connector.config_json['selection'])
        # Credentials/config changes invalidate an in-flight publisher too.
        fingerprint = _hash(_json({'selection':asdict(selection), 'credential_ref':connector.credential_ref,
                                  'config':connector.config_json, 'space_id':space_id}))
        return selection, fingerprint

    def _fence(self, session, run_id, owner, connector_id, space_id, fingerprint):
        row = session.execute(update(KnowledgeSyncRun).where(KnowledgeSyncRun.id==run_id,
            KnowledgeSyncRun.connector_id==connector_id, KnowledgeSyncRun.lease_owner==owner,
            KnowledgeSyncRun.status=='running').values(heartbeat_at=utcnow(),updated_at=utcnow()))
        if row.rowcount != 1: raise FeishuSyncError('Sync ownership was lost')
        connector = session.get(KnowledgeConnector, connector_id)
        if self._binding(connector, space_id)[1] != fingerprint:
            raise FeishuSyncError('Connector configuration changed during sync')
        return connector

    async def sync(self, *, connector_id: str, space_id: str, idempotency_key: str, source: FeishuSource, mode='incremental'):
        if mode not in {'incremental','full'} or not isinstance(idempotency_key,str) or not idempotency_key.strip() or len(idempotency_key)>512:
            raise FeishuSyncError('Sync request is invalid')
        if not all(isinstance(x,str) and 0<len(x)<=160 for x in (connector_id,space_id)):
            raise FeishuSyncError('Sync identity is invalid')
        run_id = 'feishu_sync_' + _hash(space_id+'\0'+idempotency_key)[:48]
        with self._lock('job:'+run_id), self._lock('connector:'+connector_id):
            with Session(self.engine) as session, session.begin():
                connector = session.get(KnowledgeConnector,connector_id)
                selection, fingerprint = self._binding(connector,space_id)
                credential_ref = connector.credential_ref
                binding_config = json.loads(_json(connector.config_json))
                selection_fingerprint = _hash(_json(asdict(selection)))
                for existing in session.scalars(select(KnowledgeSourceItem).where(KnowledgeSourceItem.connector_id==connector_id)):
                    if existing.metadata_json.get('selection_fingerprint') != selection_fingerprint:
                        raise FeishuSyncError('Selection change requires explicit source migration')
                request_fingerprint = _hash(_json([connector_id,space_id,fingerprint,mode]))
                run = session.get(KnowledgeSyncRun,run_id)
                if run is not None:
                    if run.connector_id!=connector_id or run.cursor_json.get('request_fingerprint')!=request_fingerprint:
                        raise FeishuSyncError('Sync idempotency key was reused with different input')
                    if run.status=='succeeded': return {'run_id':run.id,'status':run.status,**run.stats_json}
                    if run.status not in {'running','failed','queued'}: raise FeishuSyncError('Sync cannot be retried')
                else:
                    run=KnowledgeSyncRun(id=run_id,connector_id=connector_id,mode=mode,
                        cursor_json={'request_fingerprint':request_fingerprint})
                    session.add(run)
                owner=uuid.uuid4().hex
                run.cursor_json={'request_fingerprint':request_fingerprint,'discovery_complete':False}
                run.status='running';run.current_step='discovering';run.lease_owner=owner
                run.attempt=(run.attempt or 0)+1;run.started_at=utcnow();run.finished_at=None
                run.error_json={};run.stats_json={};run.updated_at=utcnow()
                session.flush()
            stats={'discovered':0,'changed':0,'unchanged':0,'linked':0,'unsupported':0,'deleted':0}
            try:
                if callable(source):
                    source = await source(credential_ref, binding_config)
                entries=await source.discover(selection)
                stats['discovered']=len(entries)
                with Session(self.engine) as session, session.begin():
                    self._fence(session,run_id,owner,connector_id,space_id,fingerprint)
                    run=session.get(KnowledgeSyncRun,run_id)
                    run.cursor_json={**run.cursor_json,'discovery_complete':True,'discovered':len(entries)}
                for entry in entries:
                    item_id='feishu_item_'+_hash(connector_id+'\0'+entry.external_id)[:48]
                    with Session(self.engine) as session:
                        old=session.get(KnowledgeSourceItem,item_id)
                        if old and (old.connector_id,old.space_id,old.external_id)!=(connector_id,space_id,entry.external_id):
                            raise FeishuSyncError('Source identity collision')
                        previous_revision=old.revision if (old and old.status=='ready' and old.asset_id and mode=='incremental'
                            and old.external_type==entry.kind and old.metadata_json.get('object_token')==entry.object_token) else None
                        previous_asset=old.asset_id if old else None
                        if previous_revision:
                            asset=session.get(KnowledgeAsset,previous_asset)
                            if not asset or asset.space_id!=space_id or asset.content_digest!=old.content_digest:
                                raise FeishuSyncError('Source Asset binding is inconsistent')
                            # A revision fast path is only valid when owned bytes still exist.
                            self.objects.read(asset.content_digest)
                    document=None; raw_digest=None; content_digest=None
                    if entry.kind=='docx':
                        document=await source.document(entry,previous_revision=previous_revision)
                        if document is not None:
                            raw_digest=self.objects.put(document.raw)
                            content_digest=self.objects.put(document.markdown)
                    with Session(self.engine) as session, session.begin():
                        self._fence(session,run_id,owner,connector_id,space_id,fingerprint)
                        item=session.get(KnowledgeSourceItem,item_id)
                        if item is None:
                            item=KnowledgeSourceItem(id=item_id,space_id=space_id,connector_id=connector_id,external_id=entry.external_id)
                            session.add(item)
                        elif (item.asset_id or None)!=(previous_asset or None):
                            raise FeishuSyncError('Source Asset changed during fetch')
                        item.external_parent_id=entry.parent_id;item.external_type=entry.kind
                        item.title=entry.title;item.path_json=list(entry.path)
                        item.last_seen_sync_run_id=run_id;item.updated_at=utcnow()
                        metadata={'object_token':entry.object_token,'selection_fingerprint':selection_fingerprint}
                        if entry.kind=='docx':
                            item.status='ready'
                            if document is not None:
                                for kind,body_digest,mime in [('raw_snapshot',raw_digest,'application/json'),('document',content_digest,'text/markdown')]:
                                    identity='feishu_'+kind+'_'+_hash(item_id+'\0'+document.revision+'\0'+body_digest)[:48]
                                    uri=f'knowledge://spaces/{space_id}/assets/{identity}'
                                    asset=session.get(KnowledgeAsset,identity)
                                    if asset is None:
                                        asset=KnowledgeAsset(id=identity,space_id=space_id,kind=kind,title=document.title,
                                            mime_type=mime,source_type='feishu',source_uri=uri,revision=body_digest,
                                            content_digest=body_digest,metadata_json={'source_item_id':item_id,'remote_revision':document.revision})
                                        session.add(asset)
                                    elif (asset.space_id,asset.content_digest,asset.source_uri,asset.source_type)!=(space_id,body_digest,uri,'feishu'):
                                        raise FeishuSyncError('Feishu Asset identity collision')
                                    if kind=='document': item.asset_id=identity
                                    else: metadata['raw_asset_id']=identity
                                item.revision=document.revision;item.content_digest=content_digest;item.title=document.title
                                metadata['warnings']=list(document.warnings)
                                metadata['attachments']=list(document.attachments)
                                stats['changed']+=1
                            else:
                                metadata={**(item.metadata_json or {}),**metadata}
                                stats['unchanged']+=1
                        else:
                            item.status='linked' if entry.kind in {'bitable','bitable_table'} else 'unsupported'
                            # An external identity may change type; do not retain stale readable content.
                            item.asset_id=None;item.content_digest=None;item.revision=None
                            if item.status=='linked': metadata.update(storage_mode='live',row_storage=False);stats['linked']+=1
                            else: stats['unsupported']+=1
                        item.metadata_json=metadata
                        run=session.get(KnowledgeSyncRun,run_id);run.stats_json=dict(stats);run.current_step='synchronizing'
                        run.cursor_json={**run.cursor_json,'last_external_id':entry.external_id}
                        run.progress=min(99,int(100*(stats['changed']+stats['unchanged']+stats['linked']+stats['unsupported'])/max(1,len(entries))))
                with Session(self.engine) as session, session.begin():
                    connector=self._fence(session,run_id,owner,connector_id,space_id,fingerprint)
                    seen={entry.external_id for entry in entries}
                    for item in session.scalars(select(KnowledgeSourceItem).where(KnowledgeSourceItem.connector_id==connector_id)):
                        if item.space_id!=space_id: raise FeishuSyncError('Connector source Space is inconsistent')
                        if mode=='full' and item.external_id not in seen and item.status!='deleted':
                            item.status='deleted';item.updated_at=utcnow();stats['deleted']+=1
                    run=session.get(KnowledgeSyncRun,run_id);run.status='succeeded';run.current_step='completed'
                    run.stats_json=dict(stats);run.progress=100;run.finished_at=utcnow();run.lease_owner=None
                    run.cursor_json={**run.cursor_json,'deletion_reconciled':mode=='full'}
                    connector.last_sync_run_id=run_id;connector.last_synced_at=utcnow();connector.updated_at=utcnow()
                return {'run_id':run_id,'status':'succeeded',**stats}
            except BaseException:
                try:
                    with Session(self.engine) as session, session.begin():
                        session.execute(update(KnowledgeSyncRun).where(KnowledgeSyncRun.id==run_id,
                            KnowledgeSyncRun.lease_owner==owner,KnowledgeSyncRun.status=='running').values(
                            status='failed',current_step='failed',error_json={'code':'feishu_sync_failed'},finished_at=utcnow(),lease_owner=None))
                except Exception:
                    pass
                raise
