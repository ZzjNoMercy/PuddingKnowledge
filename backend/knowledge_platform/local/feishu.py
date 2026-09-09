"""Explicit local Feishu configuration and Platform-owned credential composition."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeCredential, KnowledgeSourceItem, KnowledgeSpace, utcnow
from knowledge_platform.connector_sync.feishu_api import FeishuApiClient
from knowledge_platform.connector_sync.feishu_source import FeishuSource, FeishuSelection
from knowledge_platform.local.feishu_sync import FeishuSyncService, FeishuSyncError
from knowledge_platform.local.vault import LocalCredentialStore
from knowledge_platform.wiki.ports import RawSnapshot

SPACE='space_kb_default'
_ID=re.compile(r'^[A-Za-z0-9][A-Za-z0-9_-]{0,119}$')
_ENV=re.compile(r'^[A-Za-z_][A-Za-z0-9_]{0,159}$')


def load_feishu_config(path: Path):
    raw=json.loads(path.read_text())
    if not isinstance(raw,dict) or set(raw)!={'version','sources'} or type(raw['version']) is not int or raw['version']!=1:
        raise ValueError('Invalid Feishu configuration')
    if not isinstance(raw['sources'],list) or not 1<=len(raw['sources'])<=100:
        raise ValueError('Invalid Feishu sources')
    seen=set()
    for item in raw['sources']:
        if not isinstance(item,dict) or not {'id','name','selection','app_id','app_secret_env'}<=set(item) or set(item)-{'id','name','selection','app_id','app_secret_env','endpoint','auth_type','oauth_redirect_uris','oauth_scopes'}:
            raise ValueError('Invalid Feishu source fields')
        if not isinstance(item['id'],str) or not _ID.fullmatch(item['id']) or item['id'] in seen:
            raise ValueError('Invalid Feishu source identity')
        seen.add(item['id'])
        if not isinstance(item['name'],str) or not 1<=len(item['name'])<=240:
            raise ValueError('Invalid Feishu source name')
        if not isinstance(item['app_id'],str) or not _ID.fullmatch(item['app_id']):
            raise ValueError('Invalid Feishu app identity')
        if not isinstance(item['app_secret_env'],str) or not _ENV.fullmatch(item['app_secret_env']):
            raise ValueError('Invalid Feishu secret environment reference')
        auth_type=item.get('auth_type','tenant')
        if auth_type not in {'tenant','user'}:raise ValueError('Invalid Feishu auth type')
        if auth_type=='user':
            from knowledge_platform.connector_sync.feishu_oauth import _redirect_uri, _scopes
            redirects=item.get('oauth_redirect_uris')
            scopes=item.get('oauth_scopes')
            if not isinstance(redirects,list) or not 1<=len(redirects)<=10 or any(not isinstance(x,str) or _redirect_uri(x)!=x for x in redirects):
                raise ValueError('User OAuth needs exact configured redirect URIs')
            if not isinstance(scopes,list) or not 1<=len(scopes)<=100 or tuple(scopes)!=_scopes(scopes) or 'offline_access' not in scopes:
                raise ValueError('User OAuth needs explicit scopes including offline_access')
        elif set(item)&{'oauth_redirect_uris','oauth_scopes'}:
            raise ValueError('Tenant source cannot configure user OAuth')
        FeishuSelection(**item['selection'])
        # Validates the operator-selected origin without issuing any request.
        FeishuApiClient('validation-only',endpoint=item.get('endpoint'))
    return raw


class LocalFeishuService:
    def __init__(self, config, catalog: Path, state_root: Path):
        self.sync_service=FeishuSyncService(catalog,state_root)
        self.engine=self.sync_service.engine
        self.objects=self.sync_service.objects
        self.vault=LocalCredentialStore(state_root,owner_user_id='feishu')
        self.config={item['id']:item for item in config['sources']}
        self.brokers={}
        for item in config['sources']:
            selection=asdict(FeishuSelection(**item['selection']))
            auth_type=item.get('auth_type','tenant')
            secret=os.environ.get(item['app_secret_env'])
            with Session(self.engine) as session,session.begin():
                if session.get(KnowledgeSpace,SPACE) is None: raise FeishuSyncError('Feishu Space is unavailable')
                connector=session.get(KnowledgeConnector,item['id'])
                if connector is not None:
                    if connector.auth_type!=auth_type:raise FeishuSyncError('Auth type change requires explicit migration')
                    if auth_type=='user' and any(connector.config_json.get(k)!=item.get(k) for k in ('oauth_redirect_uris','oauth_scopes')):
                        raise FeishuSyncError('OAuth policy change requires explicit migration')
                    if connector.space_id!=SPACE or connector.connector_key!='feishu' or connector.config_json.get('selection')!=selection:
                        raise FeishuSyncError('Existing Feishu source requires explicit migration')
                    if connector.config_json.get('app_id')!=item['app_id'] or connector.config_json.get('endpoint')!=item.get('endpoint'):
                        raise FeishuSyncError('Existing Feishu app binding differs')
                if secret:
                    # Content-specific key: credential rotation cannot overwrite a token
                    # source still referenced by a concurrent request.
                    key='feishu_app_'+hashlib.sha256((item['app_id']+'\0'+secret).encode()).hexdigest()[:48]
                    reference=self.vault.put(key,json.dumps({'app_id':item['app_id'],'app_secret':secret}))
                elif connector is not None:
                    reference=connector.credential_ref
                    if not self.vault.get(reference): raise FeishuSyncError('Feishu Vault credential is unavailable')
                else:
                    raise FeishuSyncError('Feishu app secret environment reference is unset')
                credential_id='feishu_credential_'+hashlib.sha256(reference.encode()).hexdigest()[:48]
                if session.get(KnowledgeCredential,credential_id) is None:
                    session.add(KnowledgeCredential(id=credential_id,owner_principal_id='knowledge-local',provider='feishu',kind='tenant_app',
                        external_key=credential_id,display_name=item['name'],api_base_url=item.get('endpoint') or 'https://open.feishu.cn',
                        credential_ref=reference,status='pending_validation'))
                if connector is None:
                    connector=KnowledgeConnector(id=item['id'],space_id=SPACE,connector_key='feishu',name=item['name'],status='ready' if auth_type=='tenant' else 'pending_authorization',auth_type=auth_type)
                    session.add(connector)
                previous_config=dict(connector.config_json or {})
                if auth_type=='user' and connector.credential_ref and connector.credential_ref!=reference:
                    connector.status='needs_reauth'
                    previous_config['authorization_generation']=int(previous_config.get('authorization_generation',0))+1
                connector.credential_ref=reference
                connector.config_json={'selection':selection,'app_id':item['app_id'],'endpoint':item.get('endpoint'),'credential_id':credential_id}
                if auth_type=='user':
                    connector.config_json={**previous_config,**connector.config_json,'auth_type':'user','connector_id':item['id'],
                        'oauth_principal':'knowledge-local','oauth_redirect_uris':item['oauth_redirect_uris'],'oauth_scopes':item['oauth_scopes']}
                connector.updated_at=utcnow()
        from knowledge_platform.local.feishu_oauth import LocalFeishuOAuth
        self.oauth=LocalFeishuOAuth(self.engine,self.vault,state_root)

    async def _bound_source(self, reference, binding):
        from knowledge_platform.connector_sync.feishu_auth import TenantTokenBroker, FeishuAppConfig, ReauthenticatingFeishuApi
        endpoint=binding.get('endpoint')
        if binding.get('auth_type')=='user':
            from knowledge_platform.local.feishu_oauth import UserAuthorizedFeishuApi
            return FeishuSource(UserAuthorizedFeishuApi(self.oauth,reference,binding,space_id=SPACE))
        key=(reference,endpoint)
        if key not in self.brokers:
            self.brokers[key]=TenantTokenBroker(self.vault,endpoint=endpoint)
        app=FeishuAppConfig(app_id=binding['app_id'],app_secret_ref=reference,endpoint=endpoint or 'https://open.feishu.cn')
        return FeishuSource(ReauthenticatingFeishuApi(self.brokers[key],app,lambda token:FeishuApiClient(token,endpoint=endpoint)))

    async def sync(self, connector_id, *, idempotency_key, mode='incremental'):
        if connector_id not in self.config: raise FeishuSyncError('Feishu source is not configured')
        # The worker claims first and hands its immutable binding to the factory.
        # A successful replay needs neither a token exchange nor remote access.
        return await self.sync_service.sync(connector_id=connector_id,space_id=SPACE,idempotency_key=idempotency_key,mode=mode,source=self._bound_source)

    async def discover(self, connector_id):
        if connector_id not in self.config: raise FeishuSyncError('Feishu source is not configured')
        with Session(self.engine) as session:
            connector=session.get(KnowledgeConnector,connector_id)
            selection,fingerprint=self.sync_service._binding(connector,SPACE)
            reference=connector.credential_ref
            binding=json.loads(json.dumps(connector.config_json))
        source=await self._bound_source(reference,binding)
        entries=await source.discover(selection)
        with Session(self.engine) as session:
            if self.sync_service._binding(session.get(KnowledgeConnector,connector_id),SPACE)[1]!=fingerprint:
                raise FeishuSyncError('Feishu binding changed during discovery')
        return [asdict(item) for item in entries]

    def _asset(self, session, asset_id):
        asset=session.get(KnowledgeAsset,asset_id)
        if asset is None or asset.source_type!='feishu' or asset.space_id!=SPACE:
            raise LookupError('Feishu Asset is unavailable')
        item=session.get(KnowledgeSourceItem,asset.metadata_json.get('source_item_id'))
        if item is None or item.space_id!=SPACE or item.status!='ready' or item.connector_id not in self.config:
            raise LookupError('Feishu source is not readable')
        connector=session.get(KnowledgeConnector,item.connector_id)
        if connector is None or connector.status not in {'ready','active'}:
            raise LookupError('Feishu source is disabled')
        expected = item.asset_id if asset.kind=='document' else item.metadata_json.get('raw_asset_id') if asset.kind=='raw_snapshot' else None
        if expected!=asset.id:
            raise LookupError('Feishu Asset is not the current source binding')
        selection=asdict(FeishuSelection(**connector.config_json['selection']))
        selection_hash=hashlib.sha256(json.dumps(selection,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        if item.metadata_json.get('selection_fingerprint')!=selection_hash:
            raise LookupError('Feishu source selection changed')
        return asset

    def read_published(self, resource_uri):
        with Session(self.engine) as session:
            asset=self._asset(session,resource_uri.rsplit('/',1)[-1])
            if asset.source_uri!=resource_uri: raise LookupError('Feishu URI is invalid')
            return self.objects.read(asset.content_digest)

    async def get(self, *, snapshot_id, source_revision):
        with Session(self.engine) as session:
            asset=self._asset(session,snapshot_id)
            if asset.kind!='document' or asset.revision!=source_revision:
                raise LookupError('Feishu snapshot revision is invalid')
            return RawSnapshot(asset.id,asset.revision,asset.source_uri,self.objects.read(asset.content_digest).decode(),asset.content_digest)


class FeishuBlobReader:
    def __init__(self, repository, service, fallback):
        self.repository,self.service,self.fallback=repository,service,fallback
    async def read(self, request):
        from knowledge_contracts import BlobReadResult
        from knowledge_contracts.artifacts import MAX_BLOB_READ_BYTES
        asset=self.repository.get_asset(asset_id=request.resource_uri.rsplit('/',1)[-1])
        if not asset or asset.get('source_type')!='feishu': return await self.fallback.read(request)
        body=self.service.read_published(request.resource_uri)
        end=min(len(body),request.end if request.end is not None else request.start+MAX_BLOB_READ_BYTES)
        selected=body[request.start:end]
        digest=lambda value:'sha256:'+hashlib.sha256(value).hexdigest()
        return BlobReadResult(resource_uri=request.resource_uri,content=selected,content_digest=digest(selected),start=request.start,
            end=request.start+len(selected),asset_digest=digest(body))


class FeishuAndConfiguredSnapshots:
    def __init__(self, feishu, configured):
        self.feishu,self.configured=feishu,configured
    async def get(self, *, snapshot_id, source_revision):
        with Session(self.feishu.engine) as session:
            asset=session.get(KnowledgeAsset,snapshot_id)
            owned=asset is not None and asset.source_type=='feishu'
        return await (self.feishu if owned else self.configured).get(snapshot_id=snapshot_id,source_revision=source_revision)
