"""Registered Bitable schemas and bounded, non-persistent live record pages."""
from __future__ import annotations

from dataclasses import asdict
import json
import hashlib
import time

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge_platform.catalog.models import KnowledgeAsset, KnowledgeConnector, KnowledgeSourceItem, utcnow
from knowledge_platform.connector_sync.bitable_schema import normalize_schema, validate_relations
from knowledge_platform.connector_sync.feishu_source import token


class BitableError(ValueError):
    pass


def normalize_policy(value):
    if not isinstance(value,dict) or set(value)!={'tables','relations'}:
        raise BitableError('Bitable policy needs tables and relations')
    tables=value['tables'];relations=value['relations']
    if not isinstance(tables,list) or len(tables)>100 or not isinstance(relations,list) or len(relations)>500:
        raise BitableError('Bitable policy exceeds its bound')
    selected=[];seen=set()
    for table in tables:
        if not isinstance(table,dict) or set(table)!={'table_id','view_id'}:
            raise BitableError('Bitable table policy fields are invalid')
        table_id=token(table['table_id']);view_id=table['view_id']
        if view_id!='':token(view_id)
        if table_id in seen:raise BitableError('Duplicate Bitable table policy')
        seen.add(table_id);selected.append({'table_id':table_id,'view_id':view_id})
    validate_relations(relations,{}) # validates syntax even before any schemas exist
    allowed={'id','name','description','source_table_id','source_field_id','target_table_id','target_field_id','cardinality'}
    for relation in relations:
        if set(relation)-allowed:raise BitableError('Unknown relation fields')
        if relation['source_table_id'] not in seen or relation['target_table_id'] not in seen:
            raise BitableError('Relation table is outside the configured scope')
    return {'tables':sorted(selected,key=lambda x:x['table_id']),'relations':json.loads(json.dumps(relations))}


class LocalBitableService:
    def __init__(self, owner):
        self.owner=owner
        self.engine=owner.engine

    def authorize(self, connector_id, principal_id):
        if not isinstance(principal_id,str) or not principal_id:raise BitableError('Bitable principal is required')
        snapshot=self._snapshot(connector_id)
        if snapshot['binding'].get('auth_type')=='user' and snapshot['binding'].get('oauth_principal')!=principal_id:
            raise BitableError('User OAuth source belongs to another principal')

    def _snapshot(self, connector_id, table_id=None):
        if connector_id not in self.owner.config:raise BitableError('Bitable source is not configured')
        with Session(self.engine) as session:
            connector=session.get(KnowledgeConnector,connector_id)
            selection,fingerprint=self.owner.sync_service._binding(connector,'space_kb_default')
            if selection.kind!='bitable':raise BitableError('Source is not a Bitable app')
            policy=normalize_policy(connector.config_json.get('bitable',{'tables':[],'relations':[]}))
            selected={x['table_id']:x['view_id'] for x in policy['tables']}
            if table_id is not None and table_id not in selected:raise BitableError('Table is outside the approved scope')
            binding=json.loads(json.dumps(connector.config_json))
            return {'selection':selection,'fingerprint':fingerprint,'reference':connector.credential_ref,'binding':binding,'policy':policy,'selected':selected}

    def _check(self, connector_id, fingerprint):
        if self._snapshot(connector_id)['fingerprint']!=fingerprint:
            raise BitableError('Bitable authorization or scope changed during request')

    async def _live_schema(self, connector_id, table_id):
        snapshot=self._snapshot(connector_id,table_id)
        source=await self.owner._bound_source(snapshot['reference'],snapshot['binding'])
        entries=await source.discover(snapshot['selection'])
        entry=next((entry for entry in entries if entry.external_id.rsplit(':',1)[-1]==table_id),None)
        if entry is None:raise BitableError('Registered table is no longer visible')
        schema=await source.schema(entry)
        self._check(connector_id,snapshot['fingerprint'])
        return snapshot,source,schema

    def _stored_schema(self, connector_id, table_id):
        snapshot=self._snapshot(connector_id,table_id)
        app=snapshot['selection'].root
        selection_hash=hashlib.sha256(json.dumps(asdict(snapshot['selection']),sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
        with Session(self.engine) as session:
            item=session.scalar(select(KnowledgeSourceItem).where(KnowledgeSourceItem.connector_id==connector_id,
                KnowledgeSourceItem.external_type=='bitable_table',KnowledgeSourceItem.status=='linked',
                KnowledgeSourceItem.external_id==f'bitable:{app}:{table_id}'))
            if item is None or item.space_id!='space_kb_default':
                raise BitableError('Table schema has not been synchronized')
            if item.metadata_json.get('selection_fingerprint')!=selection_hash:
                raise BitableError('Stored schema selection changed')
            asset=session.get(KnowledgeAsset,item.asset_id)
            if asset is None or asset.space_id!=item.space_id or asset.kind!='table_schema' or asset.revision!=item.revision or asset.source_type!='feishu' or asset.metadata_json.get('source_item_id')!=item.id or asset.content_digest!=item.content_digest or asset.source_uri!=f'knowledge://spaces/{item.space_id}/assets/{asset.id}':
                raise BitableError('Table schema Asset binding is invalid')
            body=self.owner.objects.read(asset.content_digest)
            document=json.loads(body)
            normalized=normalize_schema(document['app_token'],document['table_id'],document['table_name'],document['view_id'],document['fields'])
            if normalized.revision!=asset.revision or document['app_token']!=app or document['table_id']!=table_id or document['view_id']!=self._snapshot(connector_id,table_id)['selected'][table_id]:
                raise BitableError('Stored schema identity is invalid')
            return document,asset.id,asset.source_uri,asset.revision

    async def describe(self, connector_id, table_id):
        snapshot,source,schema=await self._live_schema(connector_id,table_id)
        try:
            _,asset_id,uri,revision=self._stored_schema(connector_id,table_id)
        except BitableError:
            asset_id=uri=revision=None
        self._check(connector_id,snapshot['fingerprint'])
        return {'source_id':connector_id,'table_id':table_id,'view_id':snapshot['selected'][table_id],
            'schema':schema.document,'schema_revision':schema.revision,'schema_asset_id':asset_id,'schema_resource_uri':uri,
            'sync_required':revision!=schema.revision,'live':True,'row_storage':False}

    async def query(self, connector_id, *, table_id, schema_revision, field_names, page_size=50, cursor='', principal_id):
        self.authorize(connector_id,principal_id)
        token(table_id)
        if type(page_size) is not int or not 1<=page_size<=100 or not isinstance(field_names,list) or len(field_names)>100 or any(not isinstance(x,str) or not x or len(x)>500 for x in field_names) or len(field_names)!=len(set(field_names)):
            raise BitableError('Invalid Bitable page request')
        if not isinstance(cursor,str) or len(cursor)>8192:raise BitableError('Invalid Bitable cursor')
        snapshot,source,schema=await self._live_schema(connector_id,table_id)
        _,asset_id,uri,stored_revision=self._stored_schema(connector_id,table_id)
        if schema_revision!=schema.revision or stored_revision!=schema.revision:
            raise BitableError('Bitable schema changed; synchronize before querying')
        available={field['field_name'] for field in schema.document['fields']}
        if len(available)!=len(schema.document['fields']):raise BitableError('Duplicate field names prevent exact projection')
        selected=sorted(field_names or available)
        if not set(selected)<=available or len(selected)>100:raise BitableError('Requested fields are outside the visible schema or page bound')
        identity={'source':connector_id,'table':table_id,'view':snapshot['selected'][table_id],'fields':selected,
                  'schema':schema_revision,'policy':snapshot['fingerprint'],'principal':principal_id,'page_size':page_size}
        identity=hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        provider_cursor=''
        if cursor:
            try:
                data=json.loads(self.owner.vault.vault.decrypt(cursor.encode(),context='bitable-cursor'))
                if data['binding']!=identity or type(data['expires']) is not int or data['expires']<int(time.time()):raise ValueError()
                provider_cursor=data['cursor']
                if not isinstance(provider_cursor,str) or not provider_cursor or len(provider_cursor)>2000:raise ValueError()
            except Exception:raise BitableError('Cursor is invalid, expired or belongs to another query') from None
        page=await source.api.list_bitable_records_page(app_token=snapshot['selection'].root,table_id=table_id,
            view_id=snapshot['selected'][table_id],field_names=selected,page_size=page_size,page_token=provider_cursor)
        if not isinstance(page.get('items'),list) or type(page.get('has_more')) is not bool or any(not isinstance(item,dict) for item in page['items']):raise BitableError('Invalid Bitable record response')
        if len(page['items'])>page_size:raise BitableError('Provider exceeded the requested page size')
        records=[];seen=set()
        for item in page['items']:
            identity_id=token(item.get('record_id'))
            if identity_id in seen or not isinstance(item.get('fields'),dict):raise BitableError('Invalid Bitable record page')
            seen.add(identity_id)
            records.append({'record_id':identity_id,'fields':{key:value for key,value in item['fields'].items() if key in selected}})
        # Records have no revision pin; re-observe metadata before returning them.
        _,_,after_schema=await self._live_schema(connector_id,table_id)
        if after_schema.revision!=schema.revision:raise BitableError('Bitable schema changed during query')
        self._check(connector_id,snapshot['fingerprint'])
        next_cursor=''
        if page['has_more']:
            next_token=page['page_token']
            if not isinstance(next_token,str) or not next_token or next_token==provider_cursor or len(next_token)>2000:
                raise BitableError('Provider continuation is invalid')
            payload=json.dumps({'binding':identity,'cursor':next_token,'expires':int(time.time())+900},sort_keys=True).encode()
            next_cursor=self.owner.vault.vault.encrypt(payload,context='bitable-cursor').decode()
        return {'source_id':connector_id,'table_id':table_id,'view_id':snapshot['selected'][table_id],
            'schema_revision':schema_revision,'schema_asset_id':asset_id,'schema_resource_uri':uri,
            'records':records,'has_more':page['has_more'],'next_cursor':next_cursor,
            'total':page.get('total') if type(page.get('total')) is int and page['total']>=0 else None,
            'live':True,'row_storage':False,'privacy':'Records are returned to the caller and are not stored by Platform.'}

    async def resolve(self, connector_id, url):
        from knowledge_platform.connector_sync.bitable_reference import parse_bitable_reference
        snapshot=self._snapshot(connector_id)
        reference=parse_bitable_reference(url)
        source=await self.owner._bound_source(snapshot['reference'],snapshot['binding'])
        app_token=reference.app_token
        if reference.node_token:
            node=await source.api.get_node(node_token=reference.node_token)
            if node.get('obj_type')!='bitable':raise BitableError('Wiki link is not a Bitable app')
            app_token=node.get('obj_token')
        if app_token!=snapshot['selection'].root:raise BitableError('Link is outside the registered Bitable app')
        tables=await source.api.list_bitable_tables(app_token=app_token)
        metadata=[{'table_id':token(x.get('table_id')),'name':str(x.get('name') or '')[:500]} for x in tables]
        if reference.table_id and reference.table_id not in {x['table_id'] for x in metadata}:raise BitableError('Linked table is not visible')
        self._check(connector_id,snapshot['fingerprint'])
        return {'tables':metadata,'table_id':reference.table_id,'view_id':reference.view_id,'policy_revision':snapshot['fingerprint'],'row_storage':False}

    def policy(self,connector_id):
        snapshot=self._snapshot(connector_id)
        return {**snapshot['policy'],'policy_revision':snapshot['fingerprint'],'row_storage':False}

    async def configure(self,connector_id,*,policy,expected_revision):
        policy=normalize_policy(policy)
        snapshot=self._snapshot(connector_id)
        if snapshot['fingerprint']!=expected_revision:raise BitableError('Bitable policy revision changed')
        source=await self.owner._bound_source(snapshot['reference'],snapshot['binding'])
        visible=await source.api.list_bitable_tables(app_token=snapshot['selection'].root)
        if {x['table_id'] for x in policy['tables']}-{x.get('table_id') for x in visible}:raise BitableError('Selected tables are not visible')
        previous={r['id']:r for r in snapshot['policy']['relations']}
        changed=[r for r in policy['relations'] if previous.get(r['id'])!=r]
        if changed:
            schemas={table:self._stored_schema(connector_id,table)[0] for table in {r[key] for r in changed for key in ('source_table_id','target_table_id')}}
            if not validate_relations(changed,schemas)['valid']:
                raise BitableError('New relation endpoints require synchronized schema fields')
        with Session(self.engine) as session,session.begin():
            session.connection().exec_driver_sql('BEGIN IMMEDIATE')
            connector=session.get(KnowledgeConnector,connector_id)
            if self.owner.sync_service._binding(connector,'space_kb_default')[1]!=expected_revision:
                raise BitableError('Bitable configuration changed during validation')
            connector.config_json={**connector.config_json,'bitable':policy,
                'bitable_policy_generation':int(connector.config_json.get('bitable_policy_generation',0))+1}
            connector.updated_at=utcnow()
            selected={x['table_id']:x['view_id'] for x in policy['tables']}
            for item in session.scalars(select(KnowledgeSourceItem).where(KnowledgeSourceItem.connector_id==connector_id,KnowledgeSourceItem.external_type=='bitable_table')):
                table_id=item.metadata_json.get('table_id')
                if table_id not in selected or item.metadata_json.get('view_id','')!=selected[table_id]:
                    item.status='deleted';item.updated_at=utcnow()
        return self.policy(connector_id)

    def relations(self,connector_id):
        snapshot=self._snapshot(connector_id);schemas={}
        for table in snapshot['selected']:
            try:schemas[table]=self._stored_schema(connector_id,table)[0]
            except BitableError:pass
        result=validate_relations(snapshot['policy']['relations'],schemas)
        self._check(connector_id,snapshot['fingerprint'])
        return {**result,'policy_revision':snapshot['fingerprint'],'row_storage':False}
