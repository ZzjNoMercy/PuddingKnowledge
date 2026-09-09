"""Platform-owned one-shot PKCE sessions and fenced rotating user grants."""
from __future__ import annotations

import asyncio
import base64
from contextlib import asynccontextmanager
from datetime import timedelta, timezone
import fcntl
import hashlib
import json
import os
import secrets
import stat
import time

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeConnector, KnowledgeCredentialGrant, KnowledgeOAuthSession, utcnow


class OAuthStateError(ValueError):
    pass


def _hash(value):
    return hashlib.sha256(value.encode()).hexdigest()


def _utc(value):
    return value.replace(tzinfo=timezone.utc) if value and value.tzinfo is None else value


def _binding(connector):
    if connector is None or connector.connector_key!='feishu' or connector.auth_type!='user' or connector.status not in {'ready','active','needs_reauth','pending_authorization'}:
        raise OAuthStateError('User-authorized Feishu source is unavailable')
    return _hash(json.dumps({'ref':connector.credential_ref,'config':connector.config_json,'space':connector.space_id,'auth':connector.auth_type},sort_keys=True))


class LocalFeishuOAuth:
    def __init__(self, engine, vault, state_root, *, client_factory=None):
        self.engine,self.vault=engine,vault
        from knowledge_platform.connector_sync.feishu_oauth import OAuthClient
        self.client_factory=client_factory or (lambda endpoint:OAuthClient(endpoint=endpoint))
        locks=state_root/'oauth-locks'
        if locks.is_symlink(): raise OAuthStateError('Invalid OAuth lock directory')
        locks.mkdir(mode=0o700,exist_ok=True)
        self._fd=os.open(locks,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        self._locks={}

    def close(self):
        fd=getattr(self,'_fd',None)
        if fd is not None:
            os.close(fd);self._fd=None

    def __del__(self):
        self.close()

    @asynccontextmanager
    async def _lock(self, connector_id):
        lock=self._locks.setdefault(connector_id,asyncio.Lock())
        async with lock:
            fd=os.open(_hash(connector_id)+'.lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW|os.O_NONBLOCK,0o600,dir_fd=self._fd)
            try:
                if not stat.S_ISREG(os.fstat(fd).st_mode):raise OAuthStateError('Invalid OAuth lock')
                deadline=time.monotonic()+30
                while True:
                    try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB);break
                    except BlockingIOError:
                        if time.monotonic()>deadline:raise OAuthStateError('OAuth operation is busy') from None
                        await asyncio.sleep(.05)
                yield
            finally:os.close(fd)

    def _credentials(self, reference, app_id):
        raw=json.loads(self.vault.get(reference))
        if not isinstance(raw,dict) or raw.get('app_id')!=app_id or not isinstance(raw.get('app_secret'),str) or not raw['app_secret']:
            raise OAuthStateError('OAuth application credential does not match')
        return raw['app_secret']

    @staticmethod
    def _principal(connector, principal_id):
        if connector.config_json.get('oauth_principal')!=principal_id:
            raise OAuthStateError('OAuth source belongs to another principal')

    async def start(self, connector_id, *, principal_id, redirect_uri):
        async with self._lock(connector_id):
            with Session(self.engine) as session,session.begin():
                connector=session.get(KnowledgeConnector,connector_id)
                fingerprint=_binding(connector);self._principal(connector,principal_id)
                if redirect_uri not in connector.config_json['oauth_redirect_uris']:
                    raise OAuthStateError('OAuth redirect is not configured')
                scopes=connector.config_json['oauth_scopes']
                app_id=connector.config_json['app_id']
                state=secrets.token_urlsafe(32);verifier=secrets.token_urlsafe(64)
                identity='oauth_'+secrets.token_hex(24)
                payload={'verifier':verifier,'redirect_uri':redirect_uri,'binding':fingerprint,
                         'app_ref':connector.credential_ref,'app_id':app_id,'endpoint':connector.config_json.get('endpoint')}
                reference=self.vault.put(identity,json.dumps(payload))
                # A new flow invalidates prior pending flows for this exact binding.
                session.execute(update(KnowledgeOAuthSession).where(KnowledgeOAuthSession.connector_id==connector_id,
                    KnowledgeOAuthSession.principal_id==principal_id,KnowledgeOAuthSession.status=='pending').values(status='superseded'))
                flow=KnowledgeOAuthSession(id=identity,state_hash=_hash(state),credential_id=connector.config_json['credential_id'],
                    connector_id=connector_id,principal_id=principal_id,redirect_uri_digest=_hash(redirect_uri),
                    verifier_credential_ref=reference,requested_scopes=list(scopes),expires_at=utcnow()+timedelta(minutes=10))
                session.add(flow)
                client=self.client_factory(payload['endpoint'])
                challenge=base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
                url=client.authorization_url(app_id=app_id,redirect_uri=redirect_uri,scopes=scopes,state=state,challenge=challenge)
                session.flush()
                return {'flow_id':identity,'authorization_url':url,'expires_at':flow.expires_at.isoformat()}

    async def complete(self, *, state, code, principal_id):
        if not isinstance(state,str) or not 20<=len(state)<=256 or not isinstance(code,str) or not 1<=len(code)<=4096:
            raise OAuthStateError('OAuth callback is invalid')
        with Session(self.engine) as session:
            flow=session.scalar(select(KnowledgeOAuthSession).where(KnowledgeOAuthSession.state_hash==_hash(state)))
            if flow is None or flow.principal_id!=principal_id:raise OAuthStateError('OAuth state is unavailable')
            connector_id=flow.connector_id;flow_id=flow.id
        async with self._lock(connector_id):
            with Session(self.engine) as session,session.begin():
                flow=session.get(KnowledgeOAuthSession,flow_id)
                connector=session.get(KnowledgeConnector,connector_id)
                fingerprint=_binding(connector);self._principal(connector,principal_id)
                if flow.status!='pending':raise OAuthStateError('OAuth state was already used')
                expired=_utc(flow.expires_at)<=utcnow()
                if expired:
                    flow.status='expired'
                else:
                    stored=json.loads(self.vault.get(flow.verifier_credential_ref))
                    if stored['binding']!=fingerprint or _hash(stored['redirect_uri'])!=flow.redirect_uri_digest:
                        raise OAuthStateError('OAuth binding changed')
                    claimed=session.execute(update(KnowledgeOAuthSession).where(KnowledgeOAuthSession.id==flow_id,
                        KnowledgeOAuthSession.status=='pending').values(status='exchanging',consumed_at=utcnow()))
                    if claimed.rowcount!=1:raise OAuthStateError('OAuth state was already claimed')
                    requested=list(flow.requested_scopes);credential_id=flow.credential_id
                    verifier_ref=flow.verifier_credential_ref
            if expired:raise OAuthStateError('OAuth state has expired')
            try:
                client=self.client_factory(stored['endpoint'])
                secret=self._credentials(stored['app_ref'],stored['app_id'])
                tokens=await client.exchange(app_id=stored['app_id'],app_secret=secret,code=code,
                    redirect_uri=stored['redirect_uri'],verifier=stored['verifier'])
                if tokens.scopes is None or not set(requested)<=set(tokens.scopes):
                    raise OAuthStateError('OAuth grant lacks the requested scopes')
                user=await client.userinfo(access_token=tokens.access_token)
                if not isinstance(user.get('open_id'),str) or not user['open_id']:
                    raise OAuthStateError('OAuth user identity is unavailable')
                grant_id='feishu_grant_'+_hash(connector_id+'\0'+principal_id+'\0'+credential_id)[:48]
                token_ref=self.vault.put(grant_id+'-'+secrets.token_hex(12),json.dumps({'access_token':tokens.access_token,'refresh_token':tokens.refresh_token}))
                with Session(self.engine) as session,session.begin():
                    connector=session.get(KnowledgeConnector,connector_id)
                    if _binding(connector)!=fingerprint:raise OAuthStateError('OAuth binding changed during exchange')
                    done=session.execute(update(KnowledgeOAuthSession).where(KnowledgeOAuthSession.id==flow_id,
                        KnowledgeOAuthSession.status=='exchanging').values(status='consumed'))
                    if done.rowcount!=1:raise OAuthStateError('OAuth callback ownership was lost')
                    grant=session.get(KnowledgeCredentialGrant,grant_id)
                    if grant is None:
                        grant=KnowledgeCredentialGrant(id=grant_id,credential_id=credential_id,connector_id=connector_id,principal_id=principal_id)
                        session.add(grant)
                    grant.token_credential_ref=token_ref;grant.token_version=(grant.token_version or 0)+1
                    grant.open_id=user['open_id'];grant.union_id=str(user.get('union_id') or '');grant.tenant_key=str(user.get('tenant_key') or '')
                    grant.access_expires_at=utcnow()+timedelta(seconds=tokens.expires_in)
                    grant.refresh_expires_at=utcnow()+timedelta(seconds=tokens.refresh_expires_in)
                    grant.granted_scopes=list(tokens.scopes);grant.status='active';grant.updated_at=utcnow()
                    connector.config_json={**connector.config_json,'user_grant_id':grant_id,
                        'authorization_generation':int(connector.config_json.get('authorization_generation',0))+1}
                    connector.status='ready';connector.updated_at=utcnow()
                # Only the verifier is disposable; superseded token refs remain
                # encrypted until a retention-aware Vault garbage collector runs.
                try:self.vault.delete(verifier_ref)
                except Exception:pass
                return {'grant_id':grant_id,'status':'active','scopes':list(tokens.scopes)}
            except BaseException:
                with Session(self.engine) as session,session.begin():
                    session.execute(update(KnowledgeOAuthSession).where(KnowledgeOAuthSession.id==flow_id,
                        KnowledgeOAuthSession.status=='exchanging').values(status='failed'))
                raise

    async def get_token(self, connector_id, *, principal_id, expected_binding, force_refresh=False, rejected_token=None):
        async with self._lock(connector_id):
            needs_reauth=False
            with Session(self.engine) as session,session.begin():
                connector=session.get(KnowledgeConnector,connector_id)
                if _binding(connector)!=expected_binding or connector.status not in {'ready','active'}:raise OAuthStateError('User credential binding changed')
                self._principal(connector,principal_id)
                grant_id=connector.config_json.get('user_grant_id')
                grant=session.get(KnowledgeCredentialGrant,grant_id) if grant_id else None
                if grant is None or grant.connector_id!=connector_id or grant.principal_id!=principal_id or grant.credential_id!=connector.config_json['credential_id']:
                    raise OAuthStateError('User grant is unavailable')
                if grant.status!='active' or grant.refresh_expires_at is None or _utc(grant.refresh_expires_at)<=utcnow():
                    grant.status='needs_reauth' if grant.status=='refreshing' else grant.status
                    connector.status='needs_reauth';needs_reauth=True
                else:
                    old=json.loads(self.vault.get(grant.token_credential_ref))
                    if (not force_refresh or rejected_token is not None and old['access_token']!=rejected_token) and grant.access_expires_at is not None and _utc(grant.access_expires_at)>utcnow()+timedelta(seconds=120):
                        return old['access_token']
                    grant_id=grant.id;version=grant.token_version;old_scopes=list(grant.granted_scopes)
                    grant.status='refreshing';grant.updated_at=utcnow()
                    binding=json.loads(json.dumps(connector.config_json));reference=connector.credential_ref
            if needs_reauth:raise OAuthStateError('User grant requires reauthorization')
            try:
                client=self.client_factory(binding.get('endpoint'))
                tokens=await client.refresh(app_id=binding['app_id'],app_secret=self._credentials(reference,binding['app_id']),refresh_token=old['refresh_token'])
                scopes=old_scopes if tokens.scopes is None else list(tokens.scopes)
                if not set(binding['oauth_scopes'])<=set(scopes):raise OAuthStateError('Refreshed grant lacks required scopes')
                token_ref=self.vault.put(grant_id+'-'+secrets.token_hex(12),json.dumps({'access_token':tokens.access_token,'refresh_token':tokens.refresh_token}))
                with Session(self.engine) as session,session.begin():
                    connector=session.get(KnowledgeConnector,connector_id)
                    if _binding(connector)!=expected_binding:raise OAuthStateError('User binding changed during refresh')
                    changed=session.execute(update(KnowledgeCredentialGrant).where(KnowledgeCredentialGrant.id==grant_id,
                        KnowledgeCredentialGrant.status=='refreshing',KnowledgeCredentialGrant.token_version==version).values(
                        status='active',token_credential_ref=token_ref,token_version=version+1,granted_scopes=scopes,
                        access_expires_at=utcnow()+timedelta(seconds=tokens.expires_in),
                        refresh_expires_at=utcnow()+timedelta(seconds=tokens.refresh_expires_in),updated_at=utcnow()))
                    if changed.rowcount!=1:raise OAuthStateError('User refresh ownership was lost')
                return tokens.access_token
            except BaseException:
                with Session(self.engine) as session,session.begin():
                    changed=session.execute(update(KnowledgeCredentialGrant).where(KnowledgeCredentialGrant.id==grant_id,
                        KnowledgeCredentialGrant.status=='refreshing',KnowledgeCredentialGrant.token_version==version).values(status='needs_reauth',updated_at=utcnow()))
                    connector=session.get(KnowledgeConnector,connector_id)
                    if changed.rowcount==1 and connector and connector.config_json.get('user_grant_id')==grant_id:
                        connector.status='needs_reauth';connector.updated_at=utcnow()
                raise

    def revoke(self, connector_id, *, principal_id):
        # Revocation never waits for an in-flight provider request. Updating the
        # generation fences every old callback, refresh and sync publisher.
        with Session(self.engine) as session,session.begin():
            connector=session.get(KnowledgeConnector,connector_id)
            _binding(connector);self._principal(connector,principal_id)
            grant_id=connector.config_json.get('user_grant_id')
            grant=session.get(KnowledgeCredentialGrant,grant_id) if grant_id else None
            if grant is not None:
                if grant.connector_id!=connector_id or grant.principal_id!=principal_id or grant.credential_id!=connector.config_json['credential_id']:
                    raise OAuthStateError('Grant ownership does not match source')
                grant.status='revoked';grant.updated_at=utcnow();grant.token_version+=1
            session.execute(update(KnowledgeOAuthSession).where(KnowledgeOAuthSession.connector_id==connector_id,
                KnowledgeOAuthSession.status.in_(['pending','exchanging'])).values(status='revoked'))
            connector.config_json={**connector.config_json,'authorization_generation':int(connector.config_json.get('authorization_generation',0))+1}
            connector.status='needs_reauth';connector.updated_at=utcnow()
        return {'status':'revoked','provider_revocation':False}


class UserAuthorizedFeishuApi:
    def __init__(self, oauth, reference, binding, *, space_id):
        from types import SimpleNamespace
        self.oauth,self.binding=oauth,binding
        self.expected=_binding(SimpleNamespace(connector_key='feishu',auth_type='user',status='ready',
            credential_ref=reference,config_json=binding,space_id=space_id))

    def __getattr__(self, name):
        async def invoke(*args,**kwargs):
            from knowledge_platform.connector_sync.feishu_api import FeishuApiClient, FeishuApiError
            from knowledge_platform.connector_sync.feishu_auth import ReauthenticatingFeishuApi
            request={'connector_id':self.binding['connector_id'],'principal_id':self.binding['oauth_principal'],'expected_binding':self.expected}
            token=await self.oauth.get_token(**request)
            client=FeishuApiClient(token,endpoint=self.binding.get('endpoint'))
            try:return await getattr(client,name)(*args,**kwargs)
            except FeishuApiError as error:
                if not ReauthenticatingFeishuApi._is_invalid_token(error):raise
            token=await self.oauth.get_token(**request,force_refresh=True,rejected_token=token)
            return await getattr(FeishuApiClient(token,endpoint=self.binding.get('endpoint')),name)(*args,**kwargs)
        return invoke
