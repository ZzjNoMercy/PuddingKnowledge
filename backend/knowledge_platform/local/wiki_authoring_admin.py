"""Explicit, Space-scoped administration of an owned Wiki authoring store."""
from pathlib import Path
from dataclasses import asdict
from ..distribution import wiki_archive as archive
from ..wiki.patch import WikiPatch, PageChange, digest, plan_patch, validate_patch_shape


def exact(value, required, optional=()):
    if not isinstance(value, dict) or not set(required) <= value.keys() or value.keys() - set(required) - set(optional):
        raise ValueError('Invalid authoring request fields')


def strings(value):
    if not isinstance(value, list) or len(value)>100 or any(not isinstance(v,str) or len(v)>1024 for v in value) or len(set(value))!=len(value):
        raise ValueError('Invalid authoring selection')
    return value


def parse_patch(value):
    exact(value, ('expected_revision','changes','selected_raw','index','log_entry'))
    changes=value['changes']
    if not isinstance(changes,list) or not 1<=len(changes)<=100:raise ValueError('Invalid patch changes')
    for change in changes:exact(change,('slug','markdown','expected_digest'),('replacement',))
    patch=WikiPatch(value['expected_revision'],tuple(PageChange(**c) for c in changes),tuple(strings(value['selected_raw'])),value['index'],value['log_entry'])
    validate_patch_shape(patch)
    return patch


class WikiAuthoringAdmin:
    def __init__(self, store, *, evidence_root, model=None):
        self.store=store
        self.evidence_root=Path(evidence_root)
        from .wiki_authoring_generation import WikiAuthoringGeneration
        self.generation=WikiAuthoringGeneration(self,model)

    def authorize(self, principal, space_id):
        scopes=set(principal.scopes)
        if principal.tenant_id is not None or not {'knowledge.admin',f'knowledge.space:{space_id}'}<=scopes or space_id!=self.store.space_id:
            raise PermissionError('Authoring admin and exact Space scope required')

    def context(self, principal, request):
        exact(request,('space_id',),('slugs','selected_raw'))
        self.authorize(principal,request['space_id'])
        slugs=strings(request.get('slugs',[]));selected=strings(request.get('selected_raw',[]))
        revision,pages,index,log=self.store.read()
        if any(s not in pages for s in slugs) or any(s not in self.store.raw for s in selected):raise ValueError('Unknown authoring selection')
        if sum(len(pages[s].encode()) for s in slugs)>16*1024*1024:raise ValueError('Selected page budget exceeded')
        raw=[];total=0
        # Membership in the admitted immutable manifest is the only path authority.
        if selected:
            with archive._lock(self.evidence_root,shared=True):
                manifest=archive._verify(self.evidence_root)
                for snapshot in selected:
                    data,fact=archive._read(self.evidence_root/'archive'/'raw'/snapshot,private=True,limit=8*1024*1024)
                    total+=len(data)
                    if total>16*1024*1024 or fact!=manifest['files'].get('raw/'+snapshot):raise ValueError('Raw evidence budget or commitment mismatch')
                    text=data.decode('utf-8')
                    if digest(text)!=self.store.raw[snapshot]:raise ValueError('Raw evidence changed')
                    raw.append({'snapshot_path':snapshot,'sha256':self.store.raw[snapshot],'content':text})
                archive._verify(self.evidence_root)
        return {'space_id':self.store.space_id,'revision':revision,'schema_bundle_hash':self.store.bundle.bundle_hash,
                'schema_closure_sha256':self.store.bundle.closure_sha256,'contract':asdict(self.store.bundle.lint_contract()),
                'index':index,'log_digest':digest(log),'inventory':[{'slug':s,'digest':digest(t)} for s,t in sorted(pages.items())],
                'pages':{s:pages[s] for s in slugs},'raw':raw}

    def preview(self, principal, request):
        exact(request,('space_id','patch'))
        self.authorize(principal,request['space_id'])
        patch=parse_patch(request['patch'])
        revision,pages,index,log=self.store.read()
        plan=plan_patch(bundle=self.store.bundle,pages=pages,index=index,log=log,raw_hashes=self.store.raw,raw_manifest_sha256=self.store.raw_manifest,patch=patch)
        return {'previous_revision':revision,'revision':plan.revision,'request_digest':plan.request_digest,
                'changed_slugs':[c.slug for c in patch.changes],'retired_slugs':[r[0] for r in plan.retired]}

    def apply(self, principal, request):
        exact(request,('space_id','operation_id','patch'))
        self.authorize(principal,request['space_id'])
        patch=parse_patch(request['patch'])
        revision=self.store.apply(patch,operation_id=request['operation_id'])
        return {'operation_id':request['operation_id'],'revision':revision}

    def generate(self, principal, request):
        return self.generation.generate(principal,request)

    def proposal(self, principal, request):
        return self.generation.proposal(principal,request)

    def abandon(self, principal, request):
        return self.generation.abandon(principal,request)
