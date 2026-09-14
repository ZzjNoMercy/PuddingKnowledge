"""Read only verified migrated resources reachable from an authorized document."""
import copy
import hashlib
from pathlib import Path
from ..distribution import wiki_archive as files
from ..retrieval.services import _authorized_for_space


class DocumentResourceError(ValueError):
    def __init__(self,status):self.status=status;super().__init__('Document resource unavailable')


def _mime(data):
    if data.startswith(b'\x89PNG\r\n\x1a\n'):return 'image/png'
    if data.startswith(b'\xff\xd8\xff'):return 'image/jpeg'
    if data.startswith((b'GIF87a',b'GIF89a')):return 'image/gif'
    if data[:4]==b'RIFF' and data[8:12]==b'WEBP':return 'image/webp'
    return 'application/octet-stream'


class DocumentResourceService:
    def __init__(self,catalog,binding):
        self.catalog=catalog;self.binding=copy.deepcopy(binding);binding=self.binding
        self.root=Path(binding['root']);self.facts=binding['tree']['files'];self.assets=binding['facts']['assets']
        edges={}
        for edge in binding['tree']['graph']['edges']:edges.setdefault(edge['source'],set()).add(edge['target'])
        self.members={};self.owners={};self.names={};count=0
        for identity,primary in binding['bindings'].items():
            pending=[primary];seen=set()
            while pending:
                name=pending.pop()
                if name in seen:continue
                if name not in self.facts:raise ValueError('Resource graph is incomplete')
                seen.add(name);count+=1
                if count>100000:raise ValueError('Document resource membership budget exceeded')
                pending.extend(edges.get(name,()))
            self.members[identity]=seen-{primary}
            for name in seen:self.owners.setdefault(name,set()).add(identity)
        for name in self.owners:
            resource_id=hashlib.sha256(('document-resource/v1\0'+name).encode()).hexdigest()
            if resource_id in self.names:raise ValueError('Resource identity collision')
            self.names[resource_id]=name

    def _authorize(self,principal,identity):
        baseline=self.assets.get(identity)
        current=self.catalog.get_asset(asset_id=identity)
        if not baseline or not current:raise DocumentResourceError(404)
        if not _authorized_for_space(principal,current.get('space_id'),operation='read'):raise DocumentResourceError(403)
        if current.get('space_id')!=baseline['space_id'] or current.get('content_digest')!=baseline['content_digest'] or current.get('source_uri')!=baseline['source_uri']:
            raise DocumentResourceError(409)
        return current

    def _access(self,principal,identity,name):
        self._authorize(principal,identity)
        if name not in self.members.get(identity,set()):raise DocumentResourceError(404)
        for owner in self.owners[name]:self._authorize(principal,owner)

    def _read(self,name):
        try:data,fact=files._read(self.root/name,private=True)
        except (ValueError,OSError):raise DocumentResourceError(409) from None
        if fact!=self.facts[name]:raise DocumentResourceError(409)
        return data

    def list(self,principal,identity):
        revision=self.catalog.catalog_revision;self._authorize(principal,identity);result=[]
        for resource_id,name in sorted(self.names.items()):
            if name not in self.members.get(identity,set()):continue
            try:self._access(principal,identity,name)
            except DocumentResourceError:continue
            data=self._read(name)
            result.append({'id':resource_id,'name':Path(name).name,'mime_type':_mime(data),'size_bytes':len(data),
                           'url':f'/v1/assets/{identity}/resources/{resource_id}'})
        if self.catalog.catalog_revision!=revision:raise DocumentResourceError(409)
        return result

    def read(self,principal,identity,resource_id):
        revision=self.catalog.catalog_revision;name=self.names.get(resource_id)
        if name is None:raise DocumentResourceError(404)
        self._access(principal,identity,name);data=self._read(name)
        if self.catalog.catalog_revision!=revision:raise DocumentResourceError(409)
        return data,_mime(data)
